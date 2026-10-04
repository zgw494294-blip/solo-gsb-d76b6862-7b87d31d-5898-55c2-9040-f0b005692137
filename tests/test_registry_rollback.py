"""Regression tests: a failed registry replacement must leave no trace.

The store connection runs in autocommit mode, so the replacement's writes
(new entries, the generation increment, every graph's new conclusion) are
wrapped in one explicit transaction. These tests inject a storage fault at
each stage — mid-write, after some graph conclusions were already updated,
and at COMMIT — and check that the old state is fully preserved: through the
HTTP API, after the database is closed and reopened, and that the very same
replacement (original ``expected_generation``) succeeds once the fault is
cleared.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

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
OTHER_GRAPH = {
    "nodes": [{"id": "s2", "type": "source",
               "columns": [{"name": "x", "sensitive": False}]}],
    "export": "s2",
}
ROWS = {
    "users": [
        {"uid": 1, "email": "a@x.example", "country": "CN"},
        {"uid": 2, "email": "b@x.example", "country": "US"},
    ]
}
CONDITIONS = {"only_cn": {"country": "CN"}}
ENTRIES = [{"source": "users", "column": "email"}]


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


def register(payload=GRAPH):
    resp = client.post("/api/graphs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def put_registry(entries, generation, http=None):
    return (http or client).put(
        "/api/sensitive-columns",
        json={"entries": entries, "expected_generation": generation},
    )


def assert_internal_error(resp):
    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["code"] == "INTERNAL"
    assert "injected storage fault" in error["message"]


def assert_initial_state(graph_ids):
    """Registry empty at generation 0; every listed graph safe at gen 0."""
    assert client.get("/api/sensitive-columns").json() == {
        "generation": 0, "entries": []}
    for gid in graph_ids:
        got = client.get(f"/api/graphs/{gid}").json()
        assert got["generation"] == 0
        assert got["review"]["blocked"] is False
    # Preview by id is gated on the same old (safe) conclusion.
    resp = client.post(
        f"/api/graphs/{graph_ids[0]}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 200
    assert resp.json()["generation"] == 0
    assert resp.json()["rows"] == [[1, "a@x.example", "ALL"]]


def reopen_database():
    """Simulate a restart: drop the process-wide store, reopen the file."""
    reset_store(get_store()._path)


# ------------------------- failure mid-transaction --------------------------


def test_failure_during_generation_write_rolls_back_everything(faulty_conn):
    gid = register()
    # The entries DELETE/INSERT succeed, then the generation UPDATE fails.
    faulty_conn.arm(lambda sql: sql.startswith("UPDATE meta"))

    assert_internal_error(put_registry(ENTRIES, 0, http=no_raise))

    # New entries must not survive next to the old generation.
    assert_initial_state([gid])
    reopen_database()
    assert_initial_state([gid])

    # Fault cleared: the same replacement with the original generation works.
    resp = put_registry(ENTRIES, 0)
    assert resp.status_code == 200
    assert resp.json()["generation"] == 1
    assert resp.json()["reviews"][gid]["blocked"] is True
    got = client.get(f"/api/graphs/{gid}").json()
    assert got["generation"] == 1
    assert got["review"]["blocked"] is True


def test_failure_during_entry_write_restores_previous_entries(faulty_conn):
    gid = register()
    # Establish a non-empty registry at generation 1 first.
    assert put_registry(ENTRIES, 0).status_code == 200
    assert client.get(f"/api/graphs/{gid}").json()["review"]["blocked"] is True

    # Clearing the registry: the DELETE of the old entries succeeds, then the
    # (empty here — use a non-empty replacement so the INSERT runs) fails.
    faulty_conn.arm(lambda sql: sql.startswith("INSERT INTO sensitive_entries"))
    new_entries = [{"source": "users", "column": "uid"}]
    assert_internal_error(put_registry(new_entries, 1, http=no_raise))

    # The previous entries, generation and blocked conclusion all survive.
    assert client.get("/api/sensitive-columns").json() == {
        "generation": 1, "entries": ENTRIES}
    got = client.get(f"/api/graphs/{gid}").json()
    assert got["generation"] == 1
    assert got["review"]["blocked"] is True
    # Preview by id stays gated on the same old (blocked) conclusion.
    resp = client.post(f"/api/graphs/{gid}/preview",
                       json={"rows": ROWS, "conditions": CONDITIONS})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_BLOCKED"

    reopen_database()
    assert client.get("/api/sensitive-columns").json() == {
        "generation": 1, "entries": ENTRIES}
    assert client.get(f"/api/graphs/{gid}").json()["review"]["blocked"] is True

    # Fault cleared: the original replacement succeeds at generation 2.
    resp = put_registry(new_entries, 1)
    assert resp.status_code == 200
    assert resp.json()["generation"] == 2
    assert client.get("/api/sensitive-columns").json() == {
        "generation": 2, "entries": new_entries}


def test_failure_after_partial_graph_updates_rolls_back_all(faulty_conn):
    g1 = register()
    g2 = register(OTHER_GRAPH)
    entries = ENTRIES + [{"source": "s2", "column": "x"}]

    # The first graph's conclusion UPDATE executes; the second one fails.
    graph_updates = []

    def fail_on_second_graph_update(sql):
        if sql.startswith("UPDATE graphs"):
            graph_updates.append(sql)
            return len(graph_updates) == 2
        return False

    faulty_conn.arm(fail_on_second_graph_update)
    assert_internal_error(put_registry(entries, 0, http=no_raise))
    # The fault really fired only after g1's conclusion had been written.
    assert len(graph_updates) == 2

    # g1's already-written conclusion update was rolled back too.
    assert_initial_state([g1, g2])
    reopen_database()
    assert_initial_state([g1, g2])

    resp = put_registry(entries, 0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation"] == 1
    assert body["reviews"][g1]["blocked"] is True
    assert body["reviews"][g2]["blocked"] is True


def test_commit_failure_rolls_back_everything(faulty_conn):
    gid = register()
    faulty_conn.arm(lambda sql: sql == "COMMIT")

    assert_internal_error(put_registry(ENTRIES, 0, http=no_raise))

    assert_initial_state([gid])
    reopen_database()
    assert_initial_state([gid])

    resp = put_registry(ENTRIES, 0)
    assert resp.status_code == 200
    assert resp.json()["generation"] == 1
    assert client.get(f"/api/graphs/{gid}").json()["review"]["blocked"] is True


def test_stale_generation_still_409_after_failed_attempt(faulty_conn):
    register()
    faulty_conn.arm(lambda sql: sql.startswith("UPDATE meta"))
    assert_internal_error(put_registry(ENTRIES, 0, http=no_raise))

    # The failed attempt did not burn the generation: a stale expectation is
    # still a 409 against generation 0 and changes nothing.
    resp = put_registry([], 7)
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "GENERATION_CONFLICT"
    assert error["expected_generation"] == 7
    assert error["current_generation"] == 0
    assert client.get("/api/sensitive-columns").json() == {
        "generation": 0, "entries": []}
