"""Sealed export packages: seal, release and claim."""

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


def put_registry(entries, generation):
    resp = client.put(
        "/api/sensitive-columns",
        json={"entries": entries, "expected_generation": generation},
    )
    assert resp.status_code == 200, resp.text
    return resp


# -------------------------------- sealing -----------------------------------


def test_seal_returns_only_package_id_and_creation_generation():
    gid = register()
    resp = seal(gid)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"id", "generation"}
    assert isinstance(body["id"], str) and body["id"]
    assert body["generation"] == 0


def test_seal_unknown_graph_returns_404():
    resp = client.post(
        "/api/graphs/does-not-exist/packages",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GRAPH_NOT_FOUND"


def test_seal_input_error_creates_no_package():
    gid = register()
    # Missing rows for the only source: same coverage error as the preview.
    resp = client.post(f"/api/graphs/{gid}/packages",
                       json={"rows": {}, "conditions": CONDITIONS})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_SOURCE_COVERAGE"
    assert get_store().list_package_rows() == []

    # Wrong row keys: same row-key error as the preview, still no package.
    resp = client.post(
        f"/api/graphs/{gid}/packages",
        json={"rows": {"users": [{"uid": 1}]}, "conditions": CONDITIONS},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_ROW_KEYS"
    assert get_store().list_package_rows() == []

    # Extra body field stays a schema error: 422, no package either.
    resp = client.post(
        f"/api/graphs/{gid}/packages",
        json={"rows": ROWS, "conditions": CONDITIONS, "nodes": []},
    )
    assert resp.status_code == 422
    assert get_store().list_package_rows() == []


def test_seal_blocked_creates_no_package():
    gid = register()
    put_registry(ENTRIES, 0)
    resp = seal(gid)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "PREVIEW_BLOCKED"
    assert "rows" not in error and "columns" not in error
    assert error["review"]["blocked"] is True
    assert get_store().list_package_rows() == []


# ------------------------------- releasing ----------------------------------


def test_release_returns_only_package_id_and_release_generation():
    gid = register()
    pid = seal_ok(gid)
    resp = release(pid, 0)
    assert resp.status_code == 200
    assert resp.json() == {"id": pid, "release_generation": 0}


def test_release_unknown_package_returns_404():
    resp = release("does-not-exist", 0)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PACKAGE_NOT_FOUND"


def test_release_stale_generation_returns_409_and_records_nothing():
    gid = register()
    pid = seal_ok(gid)
    # Move the registry to generation 1 (empty list again: graph stays safe).
    put_registry([], 0)
    resp = release(pid, 0)
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "GENERATION_CONFLICT"
    assert error["expected_generation"] == 0
    assert error["current_generation"] == 1
    # Nothing was recorded: the package is still unreleased.
    assert client.get(f"/api/packages/{pid}").status_code == 409
    # Releasing with the current generation works.
    resp = release(pid, 1)
    assert resp.status_code == 200
    assert resp.json() == {"id": pid, "release_generation": 1}


def test_release_blocked_graph_returns_400_and_records_nothing():
    gid = register()
    pid = seal_ok(gid)
    # Mark users.email: the graph is blocked under generation 1.
    put_registry(ENTRIES, 0)
    resp = release(pid, 1)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "RELEASE_BLOCKED"
    assert error["review"]["blocked"] is True
    mail = next(c for c in error["review"]["columns"] if c["name"] == "mail")
    assert mail["content_reasons"] == [{"node_id": "users", "column": "email"}]
    # No sample data leaks through the release error.
    assert "rows" not in error and "columns" not in error
    # The package was not released.
    assert client.get(f"/api/packages/{pid}").status_code == 409


def test_package_sealed_under_old_generation_releases_under_current():
    gid = register()
    pid = seal_ok(gid)  # created under generation 0
    # A harmless registry change (unknown source) moves to generation 1.
    put_registry([{"source": "ghost", "column": "x"}], 0)
    resp = release(pid, 1)
    assert resp.status_code == 200
    assert resp.json() == {"id": pid, "release_generation": 1}
    body = client.get(f"/api/packages/{pid}").json()
    assert body["generation"] == 0          # sealed under generation 0
    assert body["release_generation"] == 1  # released under generation 1
    assert body["rows"] == SEALED_ROWS


# -------------------------------- claiming ----------------------------------


def test_claim_returns_sealed_data_with_both_generations():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 200
    assert resp.json() == {
        "id": pid,
        "graph_id": gid,
        "generation": 0,
        "release_generation": 0,
        "columns": SEALED_COLUMNS,
        "rows": SEALED_ROWS,
    }


def test_claim_unreleased_package_returns_409_without_data():
    gid = register()
    pid = seal_ok(gid)
    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "PACKAGE_NOT_RELEASED"
    assert error["release_generation"] is None
    assert error["current_generation"] == 0
    assert "columns" not in error and "rows" not in error


def test_claim_unknown_package_returns_404():
    resp = client.get("/api/packages/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PACKAGE_NOT_FOUND"


def test_registry_change_invalidates_release_until_rerelease():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    assert client.get(f"/api/packages/{pid}").status_code == 200

    # Registry marks users.email: generation 1, the graph is now blocked.
    put_registry(ENTRIES, 0)
    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "PACKAGE_NOT_RELEASED"
    assert error["release_generation"] == 0
    assert error["current_generation"] == 1
    assert "columns" not in error and "rows" not in error

    # Releasing under the blocked registry is refused (400, nothing recorded).
    assert release(pid, 1).status_code == 400

    # Clearing the registry restores safety at generation 2 — but the old
    # release is still stale: the package must be released again.
    put_registry([], 1)
    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 409
    assert resp.json()["error"]["release_generation"] == 0

    # Re-release under the current generation, then the claim succeeds and
    # returns the very data sealed at creation time (never recomputed).
    resp = release(pid, 2)
    assert resp.status_code == 200
    assert resp.json() == {"id": pid, "release_generation": 2}
    body = client.get(f"/api/packages/{pid}").json()
    assert body["generation"] == 0
    assert body["release_generation"] == 2
    assert body["columns"] == SEALED_COLUMNS
    assert body["rows"] == SEALED_ROWS


def test_claim_never_recomputes_the_sealed_data():
    gid = register()
    pid = seal_ok(gid)
    release(pid, 0)
    # A live preview with different rows returns different data ...
    other = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": {"users": [
            {"uid": 9, "email": "z@x.example", "country": "CN"}]},
              "conditions": CONDITIONS},
    )
    assert other.status_code == 200
    assert other.json()["rows"] == [[9, "z@x.example", "ALL"]]
    # ... while the claim keeps serving the batch sealed at creation time.
    body = client.get(f"/api/packages/{pid}").json()
    assert body["rows"] == SEALED_ROWS
    assert body["columns"] == SEALED_COLUMNS


# ------------------------------ persistence ---------------------------------


def test_packages_and_releases_survive_store_reopen(tmp_path):
    db_path = str(tmp_path / "packages.db")
    reset_store(db_path)
    gid = register()
    released = seal_ok(gid)
    release(released, 0)
    unreleased = seal_ok(gid)

    # Simulate a restart: drop the process-wide store, reopen the same file.
    reset_store(db_path)
    body = client.get(f"/api/packages/{released}").json()
    assert body["graph_id"] == gid
    assert body["generation"] == 0
    assert body["release_generation"] == 0
    assert body["columns"] == SEALED_COLUMNS
    assert body["rows"] == SEALED_ROWS
    # The unreleased package survived too, still not claimable.
    resp = client.get(f"/api/packages/{unreleased}")
    assert resp.status_code == 409
    assert resp.json()["error"]["release_generation"] is None


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


def test_failed_seal_insert_leaves_no_package(faulty_conn):
    gid = register()
    faulty_conn.arm(lambda sql: sql.startswith("INSERT INTO packages"))
    resp = seal(gid, http=no_raise)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL"
    assert get_store().list_package_rows() == []

    # Fault cleared: the same seal succeeds.
    resp = seal(gid)
    assert resp.status_code == 201
    assert len(get_store().list_package_rows()) == 1


def test_failed_release_update_leaves_no_release_record(faulty_conn):
    gid = register()
    pid = seal_ok(gid)
    faulty_conn.arm(lambda sql: sql.startswith("UPDATE packages"))
    resp = release(pid, 0, http=no_raise)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL"
    # No release was recorded: the package stays unclaimable.
    assert client.get(f"/api/packages/{pid}").status_code == 409

    # Fault cleared: the same release succeeds.
    assert release(pid, 0).status_code == 200
    assert client.get(f"/api/packages/{pid}").status_code == 200


# ---------------------------- concurrency order -----------------------------


def test_release_and_replacement_take_effect_in_one_order(monkeypatch):
    """A release either fully precedes or fully follows a replacement.

    Hold the store lock inside the release's re-review so a registry
    replacement queues behind it: the in-flight release completes under
    generation 0, then the replacement commits generation 1 and the just
    recorded release is already stale for claiming.
    """
    gid = register()
    pid = seal_ok(gid)
    entered = threading.Event()
    proceed = threading.Event()
    import app.registry as reg_mod

    original_review = reg_mod.run_review

    def slow_review(request, extra_sensitive):
        entered.set()
        assert proceed.wait(timeout=5)
        return original_review(request, extra_sensitive)

    monkeypatch.setattr(reg_mod, "run_review", slow_review)

    outcomes = {}

    def do_release():
        outcomes["release"] = registry.release_package(pid, 0)

    worker = threading.Thread(target=do_release)
    worker.start()
    assert entered.wait(timeout=5)
    # The replacement cannot commit while the release holds the lock.
    pending = threading.Thread(
        target=lambda: outcomes.setdefault(
            "replace",
            registry.replace_registry([("users", "email")], 0),
        )
    )
    pending.start()
    assert "replace" not in outcomes
    proceed.set()
    worker.join(timeout=5)
    pending.join(timeout=5)

    # The release went through under generation 0 ...
    assert outcomes["release"] == {"id": pid, "release_generation": 0}
    # ... then the replacement committed generation 1 ...
    assert outcomes["replace"]["generation"] == 1
    # ... and the old release no longer matches the current generation.
    resp = client.get(f"/api/packages/{pid}")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "PACKAGE_NOT_RELEASED"
    assert error["release_generation"] == 0
    assert error["current_generation"] == 1
