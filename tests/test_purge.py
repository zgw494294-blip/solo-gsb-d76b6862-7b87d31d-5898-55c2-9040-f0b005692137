"""Purging sealed samples by source column: POST /api/packages/purge."""

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

# Clean graph: nothing sensitive in-graph, so the registry alone decides.
# Export dependencies: users/uid and users/email (copied content) plus
# users/country (filter reference -> row influence).
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
SEALED_COLUMNS = ["user_id", "mail", "bucket"]
SEALED_ROWS = [[1, "a@x.example", "ALL"]]
ENTRIES = [{"source": "users", "column": "email"}]

# A second, unrelated graph: a bare source export depending only on s2/x.
OTHER_GRAPH = {
    "nodes": [{"id": "s2", "type": "source",
               "columns": [{"name": "x", "sensitive": False}]}],
    "export": "s2",
}
OTHER_ROWS = {"s2": [{"x": 1}]}

# Constant-only export: no content dependency and no row influence at all.
CONSTANT_GRAPH = {
    "nodes": [
        {"id": "s", "type": "source",
         "columns": [{"name": "a"}, {"name": "b"}]},
        {"id": "e", "type": "projection", "input": "s",
         "columns": [{"name": "lit", "constant": 1}]},
    ],
    "export": "e",
}

# The sensitive column never reaches the export; the clean one does.
SENSITIVE_GRAPH = {
    "nodes": [
        {"id": "s", "type": "source",
         "columns": [{"name": "secret", "sensitive": True},
                     {"name": "open", "sensitive": False}]},
        {"id": "e", "type": "projection", "input": "s",
         "columns": [{"name": "o", "copy": "open"}]},
    ],
    "export": "e",
}

# Join keys shape the row set without being copied into any output.
JOIN_GRAPH = {
    "nodes": [
        {"id": "orders", "type": "source",
         "columns": [{"name": "uid"}, {"name": "amount"}]},
        {"id": "people", "type": "source",
         "columns": [{"name": "uid"}, {"name": "name"}]},
        {"id": "j", "type": "join",
         "left_input": "orders", "right_input": "people",
         "left_key": "uid", "right_key": "uid",
         "outputs": [
             {"alias": "amt", "side": "left", "column": "amount"},
             {"alias": "uname", "side": "right", "column": "name"},
         ]},
    ],
    "export": "j",
}
JOIN_ROWS = {
    "orders": [{"uid": 1, "amount": 9}],
    "people": [{"uid": 1, "name": "ada"}],
}


def register(payload=GRAPH):
    resp = client.post("/api/graphs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def seal(gid, rows=ROWS, conditions=CONDITIONS, http=None):
    return (http or client).post(
        f"/api/graphs/{gid}/packages",
        json={"rows": rows, "conditions": conditions},
    )


def seal_ok(gid, rows=ROWS, conditions=CONDITIONS):
    resp = seal(gid, rows, conditions)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def release(pid, generation, http=None):
    return (http or client).post(
        f"/api/packages/{pid}/release",
        json={"expected_generation": generation},
    )


def purge(*columns, http=None):
    """POST /api/packages/purge with (source, column) pairs."""
    return (http or client).post(
        "/api/packages/purge",
        json={"columns": [{"source": s, "column": c} for s, c in columns]},
    )


def put_registry(entries, generation):
    resp = client.put(
        "/api/sensitive-columns",
        json={"entries": entries, "expected_generation": generation},
    )
    assert resp.status_code == 200, resp.text
    return resp


# --------------------------- dependency decision ----------------------------


def test_purge_matches_copied_content_dependency():
    gid = register()
    pid = seal_ok(gid)
    resp = purge(("users", "email"))
    assert resp.status_code == 200
    assert resp.json() == {"purged": [pid]}


def test_purge_matches_filter_row_influence():
    # users/country never flows into any export column's content; it only
    # shapes the row set through the filter reference. That still counts.
    gid = register()
    pid = seal_ok(gid)
    assert purge(("users", "country")).json()["purged"] == [pid]


def test_purge_matches_join_key_row_influence():
    gid = register(JOIN_GRAPH)
    pid = seal_ok(gid, rows=JOIN_ROWS, conditions={})
    # No output copies a join key, yet the keys decide which rows survive.
    assert purge(("orders", "uid")).json()["purged"] == [pid]

    gid = register(JOIN_GRAPH)
    pid = seal_ok(gid, rows=JOIN_ROWS, conditions={})
    assert purge(("people", "uid")).json()["purged"] == [pid]


def test_constant_cuts_content_dependency():
    gid = register(CONSTANT_GRAPH)
    pid = seal_ok(gid, rows={"s": [{"a": 1, "b": 2}]}, conditions={})
    # The export exposes only a constant column and no filter/join left any
    # row influence: it depends on no source column at all.
    assert purge(("s", "a")).json()["purged"] == []
    assert purge(("s", "b")).json()["purged"] == []
    assert client.get(f"/api/packages/{pid}").status_code == 409  # not purged


def test_non_export_branch_does_not_count():
    graph = {
        "nodes": GRAPH["nodes"] + [
            {"id": "audit", "type": "source",
             "columns": [{"name": "secret", "sensitive": False}]},
        ],
        "export": "export_users",
    }
    gid = register(graph)
    # The closure's only source is users; audit data is never supplied.
    pid = seal_ok(gid)
    assert purge(("audit", "secret")).json()["purged"] == []
    assert client.get(f"/api/packages/{pid}").status_code == 409  # not purged


def test_sensitivity_marks_do_not_drive_the_decision():
    gid = register(SENSITIVE_GRAPH)
    pid = seal_ok(gid, rows={"s": [{"secret": "x", "open": 1}]}, conditions={})
    # The sensitive s/secret never reaches the export: listing it purges
    # nothing, even though it is marked sensitive in-graph ...
    assert purge(("s", "secret")).json()["purged"] == []
    assert client.get(f"/api/packages/{pid}").status_code == 409
    # ... while the non-sensitive s/open does reach it and purges the package.
    assert purge(("s", "open")).json()["purged"] == [pid]
    assert client.get(f"/api/packages/{pid}").status_code == 410


def test_purge_ignores_the_shared_registry_marks():
    gid = register()
    pid = seal_ok(gid)
    # The registry marks users/email; the dependency decision is unaffected
    # by marks in either direction, so an unrelated column still matches
    # nothing and a marked column matches exactly as before.
    put_registry(ENTRIES, 0)
    assert purge(("ghost", "email")).json()["purged"] == []
    assert purge(("users", "email")).json()["purged"] == [pid]


# ------------------------------ purge result --------------------------------


def test_purge_lists_every_matching_package_once_sorted():
    gid = register()
    other = register(OTHER_GRAPH)
    p1 = seal_ok(gid)
    p2 = seal_ok(gid)  # a second package of the same graph
    p3 = seal_ok(other, rows=OTHER_ROWS, conditions={})
    resp = purge(("users", "email"), ("s2", "x"))
    assert resp.status_code == 200
    # Both packages of the dependent graph are listed, each exactly once,
    # together with the other graph's package, sorted by id.
    assert resp.json()["purged"] == sorted([p1, p2, p3])
    for pid in (p1, p2, p3):
        assert client.get(f"/api/packages/{pid}").status_code == 410


def test_purged_packages_are_never_listed_again():
    gid = register()
    pid = seal_ok(gid)
    assert purge(("users", "email")).json()["purged"] == [pid]
    # A repeated purge of the same (or another matching) column finds no
    # live package to clear and lists nothing.
    assert purge(("users", "email")).json()["purged"] == []
    assert purge(("users", "uid"), ("users", "country")).json()["purged"] == []


def test_unmatched_columns_purge_nothing_and_change_nothing():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    resp = purge(("ghost", "x"), ("users", "no_such_column"))
    assert resp.status_code == 200
    assert resp.json() == {"purged": []}
    # The package is fully intact: rows, release record, no purged marker.
    row = get_store().get_package_row(pid)
    assert row["purged"] == 0
    assert json.loads(row["rows"]) == SEALED_ROWS
    assert row["release_generation"] == 0
    assert client.get(f"/api/packages/{pid}").status_code == 200


def test_purge_clears_rows_and_release_record_but_keeps_columns():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert purge(("users", "email")).json()["purged"] == [pid]
    row = get_store().get_package_row(pid)
    assert row["purged"] == 1
    assert json.loads(row["rows"]) == []
    assert row["release_generation"] is None
    assert json.loads(row["columns"]) == SEALED_COLUMNS
    assert row["graph_id"] == gid


def test_purge_leaves_graphs_registry_and_generation_untouched():
    gid = register()
    pid = seal_ok(gid)
    put_registry(ENTRIES, 0)  # generation 1: the graph is now blocked
    assert purge(("users", "email")).json()["purged"] == [pid]
    assert client.get("/api/sensitive-columns").json() == {
        "generation": 1, "entries": ENTRIES}
    got = client.get(f"/api/graphs/{gid}").json()
    assert got["generation"] == 1
    assert got["review"]["blocked"] is True


def test_packages_sealed_after_the_purge_are_unaffected():
    gid = register()
    old = seal_ok(gid)
    assert purge(("users", "email")).json()["purged"] == [old]
    # A package sealed only after the purge lives on normally.
    new = seal_ok(gid)
    release(new, 0)
    resp = client.get(f"/api/packages/{new}")
    assert resp.status_code == 200
    assert resp.json()["rows"] == SEALED_ROWS
    assert resp.json()["columns"] == SEALED_COLUMNS


# ------------------------- purged package is gone ---------------------------


def test_purged_package_release_and_claim_return_410_without_sample():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert purge(("users", "email")).json()["purged"] == [pid]

    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 410
    body = resp.json()
    assert body["error"]["code"] == "PACKAGE_PURGED"
    assert "columns" not in body and "rows" not in body

    resp = release(pid, 0)
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "PACKAGE_PURGED"
    assert "columns" not in resp.json() and "rows" not in resp.json()

    # An unreleased package purged before any release behaves the same.
    pid2 = seal_ok(gid)
    assert purge(("users", "uid")).json()["purged"] == [pid2]
    assert client.get(f"/api/packages/{pid2}").status_code == 410
    assert release(pid2, 0).status_code == 410


def test_registry_restore_and_rerelease_cannot_revive_a_purged_package():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    put_registry(ENTRIES, 0)   # generation 1: blocked
    put_registry([], 1)        # generation 2: safe again
    assert purge(("users", "email")).json()["purged"] == [pid]
    # The registry is safe and current, yet the purged package stays gone.
    assert client.get(f"/api/packages/{pid}").status_code == 410
    assert release(pid, 2).status_code == 410


def test_unknown_package_still_404_after_purges():
    gid = register()
    seal_ok(gid)
    assert purge(("users", "email")).status_code == 200
    resp = client.get("/api/packages/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PACKAGE_NOT_FOUND"
    resp = release("does-not-exist", 0)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PACKAGE_NOT_FOUND"


# ------------------------------ request schema ------------------------------


def test_purge_request_schema_violations_are_422():
    gid = register()
    pid = seal_ok(gid)
    # Empty list.
    assert client.post("/api/packages/purge", json={"columns": []}
                       ).status_code == 422
    # Missing columns field.
    assert client.post("/api/packages/purge", json={}).status_code == 422
    # Empty source / column names.
    assert client.post("/api/packages/purge",
                       json={"columns": [{"source": "", "column": "a"}]}
                       ).status_code == 422
    assert client.post("/api/packages/purge",
                       json={"columns": [{"source": "s", "column": ""}]}
                       ).status_code == 422
    # Duplicate (source, column) pair.
    assert client.post("/api/packages/purge",
                       json={"columns": [{"source": "s", "column": "a"},
                                         {"source": "s", "column": "a"}]}
                       ).status_code == 422
    # Unknown field.
    assert client.post("/api/packages/purge",
                       json={"columns": [{"source": "s", "column": "a"}],
                             "nope": 1}).status_code == 422
    # Nothing was purged by any of the rejected requests.
    assert client.get(f"/api/packages/{pid}").status_code == 409

    # The same column name on two different sources is not a duplicate.
    resp = client.post("/api/packages/purge",
                       json={"columns": [{"source": "s1", "column": "a"},
                                         {"source": "s2", "column": "a"}]})
    assert resp.status_code == 200
    assert resp.json() == {"purged": []}


# ------------------------------ persistence ---------------------------------


def test_purged_marker_survives_store_reopen(tmp_path):
    db_path = str(tmp_path / "purge.db")
    reset_store(db_path)
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert purge(("users", "email")).json()["purged"] == [pid]

    # Simulate a restart: drop the process-wide store, reopen the same file.
    reset_store(db_path)
    assert client.get(f"/api/packages/{pid}").status_code == 410
    assert release(pid, 0).status_code == 410
    row = get_store().get_package_row(pid)
    assert row["purged"] == 1
    assert json.loads(row["rows"]) == []


def test_database_from_before_the_purged_column_keeps_working(tmp_path):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('generation', '0');
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
        INSERT INTO packages VALUES
            ('p1', 0, 'g1', '["a"]', '[[1]]', 0, 0);
        """
    )
    conn.close()

    reset_store(db_path)
    # The old file gained the purged column; the old package is live.
    row = get_store().get_package_row("p1")
    assert row["purged"] == 0
    assert json.loads(row["rows"]) == [[1]]
    assert row["release_generation"] == 0


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


def test_failed_purge_leaves_every_package_unchanged(faulty_conn):
    gid = register()
    other = register(OTHER_GRAPH)
    p1 = seal_ok(gid)
    p2 = seal_ok(other, rows=OTHER_ROWS, conditions={})
    release(p1, 0)
    release(p2, 0)

    faulty_conn.arm(lambda sql: sql.startswith("UPDATE packages"))
    resp = purge(("users", "email"), ("s2", "x"), http=no_raise)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL"
    # The whole batch was rolled back: neither package was purged.
    assert client.get(f"/api/packages/{p1}").status_code == 200
    assert client.get(f"/api/packages/{p2}").status_code == 200
    assert all(row["purged"] == 0 for row in get_store().list_package_rows())

    # Fault cleared: the same purge succeeds and lists both, sorted.
    resp = purge(("users", "email"), ("s2", "x"))
    assert resp.status_code == 200
    assert resp.json()["purged"] == sorted([p1, p2])
    assert client.get(f"/api/packages/{p1}").status_code == 410
    assert client.get(f"/api/packages/{p2}").status_code == 410


def test_failed_purge_commit_rolls_back_everything(faulty_conn):
    gid = register()
    pid = seal_ok(gid)
    faulty_conn.arm(lambda sql: sql == "COMMIT")
    resp = purge(("users", "email"), http=no_raise)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL"
    # Nothing changed: the package can still be released and claimed.
    release(pid, 0)
    assert client.get(f"/api/packages/{pid}").status_code == 200


# ---------------------------- concurrency order -----------------------------


def test_purge_and_claim_take_effect_in_one_order(monkeypatch):
    """A claim either fully precedes or fully follows a purge.

    Hold the store lock inside the purge's dependency evaluation so a
    concurrent claim queues behind it: the purge commits first, and the
    claim that started while the purge was in flight observes the purged
    package instead of the sample.
    """
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    entered = threading.Event()
    proceed = threading.Event()
    import app.registry as reg_mod

    original_dependencies = reg_mod.export_dependencies

    def slow_dependencies(request):
        entered.set()
        assert proceed.wait(timeout=5)
        return original_dependencies(request)

    monkeypatch.setattr(reg_mod, "export_dependencies", slow_dependencies)

    outcomes = {}

    def do_purge():
        outcomes["purge"] = registry.purge_packages([("users", "email")])

    worker = threading.Thread(target=do_purge)
    worker.start()
    assert entered.wait(timeout=5)

    def do_claim():
        try:
            outcomes["claim"] = registry.claim_package(pid)
        except registry.PackagePurged:
            outcomes["claim"] = "purged"

    pending = threading.Thread(target=do_claim)
    pending.start()
    # The claim cannot be served while the purge holds the lock.
    assert "claim" not in outcomes
    proceed.set()
    worker.join(timeout=5)
    pending.join(timeout=5)

    # The purge went through first ...
    assert outcomes["purge"] == {"purged": [pid]}
    # ... and the queued claim observed the purged package, not the sample.
    assert outcomes["claim"] == "purged"
    assert client.get(f"/api/packages/{pid}").status_code == 410
