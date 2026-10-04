"""Purging sealed export packages by source column."""

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import registry
from app.main import app
from app.storage import get_store, reset_store

client = TestClient(app)
# A storage fault must surface as a 500/INTERNAL response, not a test crash.
no_raise = TestClient(app, raise_server_exceptions=False)

# Clean graph: nothing sensitive in-graph, so dependency alone decides. The
# export copies users.uid and users.email (content), the filter puts
# users.country into row influence, and bucket is a constant.
GRAPH = {
    "nodes": [
        {
            "id": "users",
            "type": "source",
            "columns": [
                {"name": "uid", "sensitive": False},
                {"name": "email", "sensitive": False},
                {"name": "country", "sensitive": False},
            ],
        },
        {"id": "only_cn", "type": "filter", "input": "users",
         "references": ["country"]},
        {
            "id": "export_users",
            "type": "projection",
            "input": "only_cn",
            "columns": [
                {"name": "user_id", "copy": "uid"},
                {"name": "mail", "copy": "email"},
                {"name": "bucket", "constant": "ALL"},
            ],
        },
    ],
    "export": "export_users",
}
ROWS = {
    "users": [
        {"uid": 1, "email": "a@x.example", "country": "CN"},
        {"uid": 2, "email": "b@x.example", "country": "US"},
    ]
}
CONDITIONS = {"only_cn": {"country": "CN"}}
SEALED_ROWS = [[1, "a@x.example", "ALL"]]


def register(payload=GRAPH):
    resp = client.post("/api/graphs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def seal(gid, http=None):
    return (http or client).post(
        f"/api/graphs/{gid}/packages",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )


def seal_ok(gid):
    resp = seal(gid)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def release(pid, generation, http=None):
    return (http or client).post(
        f"/api/packages/{pid}/release",
        json={"expected_generation": generation},
    )


def purge(sources, http=None):
    return (http or client).post(
        "/api/packages/purge",
        json={"sources": [{"source": s, "column": c} for s, c in sources]},
    )


def put_registry(entries, generation):
    resp = client.put(
        "/api/sensitive-columns",
        json={"entries": entries, "expected_generation": generation},
    )
    assert resp.status_code == 200, resp.text
    return resp


def package_row(pid):
    return get_store().get_package_row(pid)


# --------------------------- dependency matching ----------------------------


def test_purge_matches_copied_content():
    gid = register()
    pid = seal_ok(gid)
    # users.email reaches the export as copied column content.
    resp = purge([("users", "email")])
    assert resp.status_code == 200
    assert resp.json() == {"purged": [pid]}


def test_purge_matches_filter_row_influence():
    gid = register()
    pid = seal_ok(gid)
    # users.country never reaches an export column, but the filter on it
    # shapes the exported rows: row-level influence counts as a dependency.
    resp = purge([("users", "country")])
    assert resp.status_code == 200
    assert resp.json() == {"purged": [pid]}


def test_purge_matches_join_key_row_influence():
    graph = {
        "nodes": [
            {"id": "left", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {"id": "right", "type": "source",
             "columns": [{"name": "k"}, {"name": "w"}]},
            {"id": "j", "type": "join",
             "left_input": "left", "right_input": "right",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "v", "side": "left", "column": "v"},
                 {"alias": "w", "side": "right", "column": "w"}]},
        ],
        "export": "j",
    }
    gid = register(graph)
    resp = client.post(
        f"/api/graphs/{gid}/packages",
        json={"rows": {"left": [{"k": 1, "v": "a"}],
                       "right": [{"k": 1, "w": "b"}]},
              "conditions": {}},
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]
    # The join keys never appear in the export's content, but they shape the
    # joined rows: purging either key's source column purges the package.
    assert purge([("right", "k")]).json() == {"purged": [pid]}


def test_purge_matches_any_listed_source():
    gid = register()
    pid = seal_ok(gid)
    resp = purge([("ghost", "x"), ("users", "uid")])
    assert resp.json() == {"purged": [pid]}


def test_purge_without_dependency_returns_empty_and_keeps_package():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    resp = purge([("ghost", "x")])
    assert resp.status_code == 200
    assert resp.json() == {"purged": []}
    # The package is untouched and still claimable.
    assert client.get(f"/api/packages/{pid}").status_code == 200


def test_constant_only_export_depends_on_nothing():
    graph = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [{"name": "a"}]},
            {"id": "e", "type": "projection", "input": "s",
             "columns": [{"name": "lit", "constant": 1}]},
        ],
        "export": "e",
    }
    gid = register(graph)
    resp = client.post(f"/api/graphs/{gid}/packages",
                       json={"rows": {"s": [{"a": 1}]}, "conditions": {}})
    assert resp.status_code == 201
    pid = resp.json()["id"]
    # A constant cuts content provenance and no filter/join is involved:
    # the export depends on no source column at all.
    assert purge([("s", "a")]).json() == {"purged": []}
    assert package_row(pid)["purged"] == 0


def test_constant_column_keeps_filter_row_influence():
    # The constant clears content provenance, but the filter's row-level
    # influence survives the projection and still counts as a dependency.
    graph = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]},
            {"id": "f", "type": "filter", "input": "s", "references": ["a"]},
            {"id": "e", "type": "projection", "input": "f",
             "columns": [{"name": "lit", "constant": 1}]},
        ],
        "export": "e",
    }
    gid = register(graph)
    resp = client.post(
        f"/api/graphs/{gid}/packages",
        json={"rows": {"s": [{"a": 1, "b": 2}]},
              "conditions": {"f": {"a": 1}}},
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]
    # s.b is not referenced anywhere: no dependency.
    assert purge([("s", "b")]).json() == {"purged": []}
    # s.a drives the filter: row influence survives the constant column.
    assert purge([("s", "a")]).json() == {"purged": [pid]}


def test_purge_ignores_side_branches():
    graph = {
        "nodes": [
            {"id": "users", "type": "source",
             "columns": [{"name": "uid"}, {"name": "email"}]},
            {"id": "e", "type": "projection", "input": "users",
             "columns": [{"name": "user_id", "copy": "uid"}]},
            # A side branch reads users.email but never reaches the export.
            {"id": "audit", "type": "projection", "input": "users",
             "columns": [{"name": "mail", "copy": "email"}]},
        ],
        "export": "e",
    }
    gid = register(graph)
    resp = client.post(
        f"/api/graphs/{gid}/packages",
        json={"rows": {"users": [{"uid": 1, "email": "a@x.example"}]},
              "conditions": {}},
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]
    assert purge([("users", "email")]).json() == {"purged": []}
    assert purge([("users", "uid")]).json() == {"purged": [pid]}


def test_purge_ignores_sensitivity_marks():
    # users.email is marked sensitive in-graph, but the export neither copies
    # it nor filters on it: sensitivity alone is not a dependency.
    graph = {
        "nodes": [
            {"id": "users", "type": "source", "columns": [
                {"name": "uid"},
                {"name": "email", "sensitive": True}]},
            {"id": "e", "type": "projection", "input": "users",
             "columns": [{"name": "user_id", "copy": "uid"}]},
        ],
        "export": "e",
    }
    gid = register(graph)
    resp = client.post(
        f"/api/graphs/{gid}/packages",
        json={"rows": {"users": [{"uid": 1, "email": "a@x.example"}]},
              "conditions": {}},
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]
    assert purge([("users", "email")]).json() == {"purged": []}
    assert package_row(pid)["purged"] == 0


def test_purge_ignores_registry_entries():
    gid = register()
    pid = seal_ok(gid)
    # The registry marks users.email: the graph is now blocked, but the
    # registry changes no dependency — an unrelated source still matches
    # nothing, and a non-sensitive row-influence source still matches.
    put_registry([{"source": "users", "column": "email"}], 0)
    assert purge([("ghost", "x")]).json() == {"purged": []}
    assert purge([("users", "country")]).json() == {"purged": [pid]}


# ------------------------------ purge effects -------------------------------


def test_purge_deletes_rows_and_release_and_marks_purged():
    gid = register()
    released = seal_ok(gid)
    release(released, 0)
    unreleased = seal_ok(gid)

    resp = purge([("users", "email")])
    assert resp.status_code == 200
    assert resp.json() == {"purged": sorted([released, unreleased])}

    for pid in (released, unreleased):
        row = package_row(pid)
        assert row["purged"] == 1
        assert json.loads(row["rows"]) == []      # sealed rows are gone
        assert row["release_generation"] is None  # release record is gone


def test_purge_returns_ids_sorted():
    gid = register()
    pids = [seal_ok(gid) for _ in range(3)]
    resp = purge([("users", "email")])
    assert resp.json() == {"purged": sorted(pids)}


def test_purge_leaves_graphs_registry_and_generation_untouched():
    gid = register()
    pid = seal_ok(gid)
    put_registry([{"source": "users", "column": "email"}], 0)

    # users.country reaches the export only through the filter's row
    # influence; the purge decision does not consult the (blocked) review.
    assert purge([("users", "country")]).json() == {"purged": [pid]}

    registry_body = client.get("/api/sensitive-columns").json()
    assert registry_body["generation"] == 1
    assert registry_body["entries"] == [{"source": "users", "column": "email"}]
    graph_body = client.get(f"/api/graphs/{gid}").json()
    assert graph_body["generation"] == 1
    assert graph_body["review"]["blocked"] is True


def test_packages_sealed_after_purge_are_unaffected():
    gid = register()
    old = seal_ok(gid)
    assert purge([("users", "email")]).json() == {"purged": [old]}

    # Sealing the same graph again creates a fresh, ordinary package.
    new = seal_ok(gid)
    assert package_row(new)["purged"] == 0
    assert release(new, 0).status_code == 200
    body = client.get(f"/api/packages/{new}").json()
    assert body["rows"] == SEALED_ROWS


def test_already_purged_packages_are_not_listed_again():
    gid = register()
    first = seal_ok(gid)
    assert purge([("users", "email")]).json() == {"purged": [first]}
    # Purging the same source again matches nothing new.
    assert purge([("users", "email")]).json() == {"purged": []}
    # A package sealed afterwards is matched normally.
    second = seal_ok(gid)
    assert purge([("users", "email")]).json() == {"purged": [second]}


# ------------------------- purged packages: 410 -----------------------------


def test_purged_package_release_and_claim_return_410_without_data():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert purge([("users", "email")]).json() == {"purged": [pid]}

    resp = release(pid, 0)
    assert resp.status_code == 410
    error = resp.json()["error"]
    assert error["code"] == "PACKAGE_PURGED"
    assert "columns" not in error and "rows" not in error

    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 410
    error = resp.json()["error"]
    assert error["code"] == "PACKAGE_PURGED"
    assert "columns" not in error and "rows" not in error


def test_purged_unreleased_package_claim_returns_410_not_409():
    gid = register()
    pid = seal_ok(gid)
    assert purge([("users", "email")]).json() == {"purged": [pid]}
    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "PACKAGE_PURGED"


def test_purge_is_permanent_across_registry_and_restart(tmp_path):
    db_path = str(tmp_path / "purge.db")
    reset_store(db_path)
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert purge([("users", "email")]).json() == {"purged": [pid]}

    # Registry changes (even back to a safe list) cannot bring it back.
    put_registry([{"source": "users", "column": "email"}], 0)
    put_registry([], 1)
    assert client.get(f"/api/packages/{pid}").status_code == 410
    assert release(pid, 2).status_code == 410

    # Nor can a restart: the purged flag is persisted in SQLite.
    reset_store(db_path)
    assert client.get(f"/api/packages/{pid}").status_code == 410
    assert release(pid, 2).status_code == 410
    # And an already purged package is never listed again.
    assert purge([("users", "email")]).json() == {"purged": []}


# ------------------------------ validation ----------------------------------


def test_purge_request_validation():
    gid = register()
    pid = seal_ok(gid)

    # Empty source list -> 422.
    resp = client.post("/api/packages/purge", json={"sources": []})
    assert resp.status_code == 422
    # Empty source/column strings -> 422.
    resp = client.post("/api/packages/purge",
                       json={"sources": [{"source": "", "column": "a"}]})
    assert resp.status_code == 422
    resp = client.post("/api/packages/purge",
                       json={"sources": [{"source": "s", "column": ""}]})
    assert resp.status_code == 422
    # Duplicate (source, column) pair -> 422.
    resp = client.post(
        "/api/packages/purge",
        json={"sources": [{"source": "s", "column": "a"},
                          {"source": "s", "column": "a"}]},
    )
    assert resp.status_code == 422
    # Unknown field -> 422.
    resp = client.post(
        "/api/packages/purge",
        json={"sources": [{"source": "s", "column": "a"}], "extra": 1},
    )
    assert resp.status_code == 422

    # None of the rejected requests touched the package.
    assert package_row(pid)["purged"] == 0
    assert json.loads(package_row(pid)["rows"]) == SEALED_ROWS


# ------------------------- failure leaves no trace --------------------------


class FaultyConnection:
    """Delegates to the real connection, failing an armed statement once."""

    def __init__(self, real):
        self._real = real
        self._predicate = None

    def arm(self, predicate):
        self._predicate = predicate

    def _check(self, sql):
        if self._predicate is not None and self._predicate(sql):
            self._predicate = None  # a transient fault fires exactly once
            raise sqlite3.OperationalError("injected storage fault")

    def execute(self, sql, parameters=()):
        self._check(sql)
        return self._real.execute(sql, parameters)

    def executemany(self, sql, seq):
        self._check(sql)
        return self._real.executemany(sql, seq)

    def commit(self):
        self._check("COMMIT")
        return self._real.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        # Mirror sqlite3.Connection: commit on success, rollback on error.
        if exc_type is None:
            self._real.commit()
        else:
            self._real.rollback()
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def faulty_conn():
    store = get_store()
    real = store._conn
    proxy = FaultyConnection(real)
    store._conn = proxy
    yield proxy
    store._conn = real


def test_failed_purge_leaves_the_whole_batch_untouched(faulty_conn):
    gid = register()
    first = seal_ok(gid)
    second = seal_ok(gid)
    release(first, 0)

    faulty_conn.arm(lambda sql: sql.startswith("UPDATE packages"))
    resp = purge([("users", "email")], http=no_raise)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL"

    # Nothing changed: rows, release records and flags are all intact.
    for pid in (first, second):
        row = package_row(pid)
        assert row["purged"] == 0
        assert json.loads(row["rows"]) == SEALED_ROWS
    assert package_row(first)["release_generation"] == 0
    assert client.get(f"/api/packages/{first}").status_code == 200

    # Fault cleared: the same purge succeeds as a whole.
    resp = purge([("users", "email")])
    assert resp.status_code == 200
    assert resp.json() == {"purged": sorted([first, second])}


# ------------------------------ persistence ---------------------------------


def test_purge_survives_store_reopen(tmp_path):
    db_path = str(tmp_path / "packages.db")
    reset_store(db_path)
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert purge([("users", "email")]).json() == {"purged": [pid]}

    # Simulate a restart: drop the process-wide store, reopen the same file.
    reset_store(db_path)
    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "PACKAGE_PURGED"
    row = package_row(pid)
    assert row["purged"] == 1
    assert json.loads(row["rows"]) == []
    assert row["release_generation"] is None


def test_store_reopens_pre_purge_database_and_migrates(tmp_path):
    db_path = str(tmp_path / "old.db")
    # Build a database with the pre-purge schema (no purged column).
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sensitive_entries (
            source_id TEXT NOT NULL, column_name TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            PRIMARY KEY (source_id, column_name));
        CREATE TABLE graphs (
            id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL,
            nodes TEXT NOT NULL, export TEXT NOT NULL,
            review TEXT NOT NULL, review_generation INTEGER NOT NULL);
        CREATE TABLE packages (
            id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL,
            graph_id TEXT NOT NULL, columns TEXT NOT NULL, rows TEXT NOT NULL,
            generation INTEGER NOT NULL, release_generation INTEGER);
        INSERT INTO meta (key, value) VALUES ('generation', '0');
        """
    )
    conn.close()

    reset_store(db_path)
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert client.get(f"/api/packages/{pid}").status_code == 200
    assert purge([("users", "email")]).json() == {"purged": [pid]}
    assert client.get(f"/api/packages/{pid}").status_code == 410


# ---------------------------- concurrency order -----------------------------


def test_purge_and_release_take_effect_in_one_order(monkeypatch):
    """A purge and a release take effect in one total order.

    Hold the store lock inside the purge's dependency computation so a
    release queues behind it: the purge completes first, and the queued
    release then finds the package already purged and refuses it.
    """
    gid = register()
    pid = seal_ok(gid)
    entered = threading.Event()
    proceed = threading.Event()
    import app.registry as reg_mod

    original = reg_mod.export_source_dependencies

    def slow_dependencies(request):
        entered.set()
        assert proceed.wait(timeout=5)
        return original(request)

    monkeypatch.setattr(
        reg_mod, "export_source_dependencies", slow_dependencies
    )

    outcomes = {}

    def do_purge():
        outcomes["purge"] = registry.purge_packages([("users", "email")])

    def do_release():
        try:
            outcomes["release"] = registry.release_package(pid, 0)
        except registry.PackagePurged:
            outcomes["release"] = "purged"

    worker = threading.Thread(target=do_purge)
    worker.start()
    assert entered.wait(timeout=5)
    # The release cannot start while the purge holds the lock.
    pending = threading.Thread(target=do_release)
    pending.start()
    assert "release" not in outcomes
    proceed.set()
    worker.join(timeout=5)
    pending.join(timeout=5)

    # The purge went through first; the queued release then found the
    # package already purged.
    assert outcomes["purge"] == {"purged": [pid]}
    assert outcomes["release"] == "purged"
