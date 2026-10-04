"""Registered graphs + shared sensitive-column registry behaviour."""

import threading

import pytest
from fastapi.testclient import TestClient

from app import registry
from app.engine import review
from app.main import app
from app.models import ReviewRequest
from app.storage import get_store, reset_store


client = TestClient(app)


# A clean graph: non-sensitive source -> filter(country) -> projection export.
# Same shape as the README example, but nothing is marked sensitive in-graph,
# so the registry is the only thing that can block it.
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


def register(payload=GRAPH):
    resp = client.post("/api/graphs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def put_registry(entries, generation):
    return client.put(
        "/api/sensitive-columns",
        json={"entries": entries, "expected_generation": generation},
    )


# ----------------------------- registration --------------------------------


def test_registry_starts_empty_at_generation_zero():
    resp = client.get("/api/sensitive-columns")
    assert resp.status_code == 200
    assert resp.json() == {"generation": 0, "entries": []}


def test_register_runs_whole_graph_validation_and_returns_id():
    body = register()
    assert set(body) == {"id", "nodes", "export", "generation", "review"}
    assert isinstance(body["id"], str) and body["id"]
    assert body["nodes"] == GRAPH["nodes"]
    assert body["export"] == "export_users"
    assert body["generation"] == 0
    assert body["review"]["blocked"] is False


def test_register_invalid_graph_is_rejected_without_storage_change():
    bad = {"nodes": [{"id": "p", "type": "projection", "input": "ghost",
                      "columns": [{"name": "a", "copy": "a"}]}],
           "export": "p"}
    resp = client.post("/api/graphs", json=bad)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REFERENCE"
    # Nothing was stored: graph count stays zero.
    assert get_store().list_graph_rows() == []
    # Schema problems stay 422.
    resp = client.post("/api/graphs", json={"nodes": GRAPH["nodes"],
                                            "export": "export_users",
                                            "nope": 1})
    assert resp.status_code == 422


def test_register_stores_original_graph_verbatim():
    body = register()
    got = client.get(f"/api/graphs/{body['id']}")
    assert got.status_code == 200
    stored = got.json()
    assert stored["id"] == body["id"]
    assert stored["nodes"] == GRAPH["nodes"]
    assert stored["export"] == "export_users"
    assert stored["generation"] == 0
    assert stored["review"]["blocked"] is False


def test_read_unknown_graph_returns_404():
    resp = client.get("/api/graphs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GRAPH_NOT_FOUND"


# --------------------------- OR sensitivity --------------------------------


def test_registry_entries_block_registered_graph_content():
    gid = register()["id"]
    resp = put_registry([{"source": "users", "column": "email"}], 0)
    assert resp.status_code == 200
    assert resp.json()["generation"] == 1

    # The stored graph is untouched; only its review conclusion changed.
    got = client.get(f"/api/graphs/{gid}").json()
    assert got["nodes"] == GRAPH["nodes"]
    assert got["generation"] == 1
    mail = next(c for c in got["review"]["columns"] if c["name"] == "mail")
    assert mail["blocked"] is True
    assert mail["content_reasons"] == [{"node_id": "users", "column": "email"}]
    user_id = next(c for c in got["review"]["columns"] if c["name"] == "user_id")
    assert user_id["blocked"] is False


def test_registry_marks_row_influence_via_filter_reference():
    gid = register()["id"]
    put_registry([{"source": "users", "column": "country"}], 0)
    got = client.get(f"/api/graphs/{gid}").json()
    # The filter references the registry-marked column; every export column
    # inherits the row-level origin.
    for column in got["review"]["columns"]:
        assert column["row_reasons"] == [{"node_id": "users", "column": "country"}]
        assert column["blocked"] is True


def test_in_graph_mark_and_registry_are_ored():
    nodes = [dict(node) if isinstance(node, dict) else node
             for node in GRAPH["nodes"]]
    # email marked in-graph too; registry additionally marks uid.
    nodes[0] = {
        "id": "users", "type": "source",
        "columns": [
            {"name": "uid", "sensitive": False},
            {"name": "email", "sensitive": True},
            {"name": "country", "sensitive": False},
        ],
    }
    gid = register({"nodes": nodes, "export": "export_users"})["id"]
    put_registry([{"source": "users", "column": "uid"}], 0)
    got = client.get(f"/api/graphs/{gid}").json()
    by_name = {c["name"]: c for c in got["review"]["columns"]}
    assert by_name["mail"]["content_reasons"] == [
        {"node_id": "users", "column": "email"}]
    assert by_name["user_id"]["content_reasons"] == [
        {"node_id": "users", "column": "uid"}]


def test_registry_entry_may_name_unknown_source_or_column():
    register()
    # Unregistered source, and a column the source does not declare: inert.
    resp = put_registry(
        [{"source": "ghost", "column": "x"},
         {"source": "users", "column": "ghost_col"}], 0)
    assert resp.status_code == 200
    listing = client.get("/api/sensitive-columns").json()
    assert {(e["source"], e["column"]) for e in listing["entries"]} == {
        ("ghost", "x"), ("users", "ghost_col")}
    # A graph registered afterwards stays safe under those inert entries.
    resp = client.post("/api/graphs", json=GRAPH)
    assert resp.json()["review"]["blocked"] is False


def test_duplicate_registry_entry_is_a_schema_error_422():
    resp = put_registry(
        [{"source": "users", "column": "email"},
         {"source": "users", "column": "email"}], 0)
    assert resp.status_code == 422
    # Nothing replaced.
    assert client.get("/api/sensitive-columns").json()["generation"] == 0


# ------------------------- replacement semantics ---------------------------


def test_replacement_increments_and_rereviews_all_graphs():
    g1 = register()["id"]
    # Second graph with a different source id.
    other = {
        "nodes": [{"id": "s2", "type": "source",
                   "columns": [{"name": "x", "sensitive": False}]}],
        "export": "s2",
    }
    g2 = client.post("/api/graphs", json=other).json()["id"]

    resp = put_registry([{"source": "users", "column": "email"},
                         {"source": "s2", "column": "x"}], 0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation"] == 1
    assert body["reviews"][g1]["blocked"] is True
    assert body["reviews"][g2]["blocked"] is True

    # Clearing with the right expected generation restores both at gen 2.
    resp = put_registry([], 1)
    assert resp.status_code == 200
    assert resp.json()["generation"] == 2
    assert client.get(f"/api/graphs/{g1}").json()["review"]["blocked"] is False
    assert client.get(f"/api/graphs/{g2}").json()["review"]["blocked"] is False


def test_stale_generation_returns_409_and_changes_nothing():
    gid = register()["id"]
    assert put_registry([{"source": "users", "column": "email"}], 0).status_code == 200
    # Replaying the old expected generation is rejected.
    resp = put_registry([], 0)
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "GENERATION_CONFLICT"
    assert error["expected_generation"] == 0
    assert error["current_generation"] == 1
    # The registry and the stored conclusion are untouched.
    assert client.get("/api/sensitive-columns").json()["generation"] == 1
    got = client.get(f"/api/graphs/{gid}").json()
    assert got["generation"] == 1
    assert got["review"]["blocked"] is True


def test_entries_keep_submission_order():
    entries = [
        {"source": "b", "column": "y"},
        {"source": "a", "column": "z"},
        {"source": "b", "column": "x"},
    ]
    put_registry(entries, 0)
    got = client.get("/api/sensitive-columns").json()
    assert got["entries"] == entries


def test_failed_rereview_changes_nothing(monkeypatch):
    gid = register()["id"]
    # A re-review blow-up happens before the transaction writes anything:
    # generation, entries and the stored conclusion all stay put.
    import app.registry as reg_mod

    def boom(request, extra_sensitive):
        raise RuntimeError("rereview failed")

    monkeypatch.setattr(reg_mod, "run_review", boom)
    with pytest.raises(RuntimeError):
        registry.replace_registry([("users", "email")], 0)

    got = client.get(f"/api/graphs/{gid}").json()
    assert got["generation"] == 0
    assert got["review"]["blocked"] is False
    reg = client.get("/api/sensitive-columns").json()
    assert reg == {"generation": 0, "entries": []}


# ------------------------------ previews -----------------------------------


def test_preview_registered_graph_submits_only_rows_and_conditions():
    gid = register()["id"]
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == gid
    assert body["generation"] == 0
    assert body["columns"] == ["user_id", "mail", "bucket"]
    assert body["rows"] == [[1, "a@x.example", "ALL"]]
    assert body["review"]["blocked"] is False


def test_preview_unknown_graph_returns_404():
    resp = client.post("/api/graphs/nope/preview",
                       json={"rows": {}, "conditions": {}})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GRAPH_NOT_FOUND"


def test_preview_reuses_original_preview_rules():
    gid = register()["id"]
    # Missing rows for the only source: same coverage error as the old preview.
    resp = client.post(f"/api/graphs/{gid}/preview",
                       json={"rows": {}, "conditions": CONDITIONS})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_SOURCE_COVERAGE"

    # Wrong row keys.
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": {"users": [{"uid": 1}]}, "conditions": CONDITIONS},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_ROW_KEYS"

    # Extra field in the body is a schema error: 422.
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS, "nodes": []},
    )
    assert resp.status_code == 422

    # Missing rows entirely: 422.
    resp = client.post(f"/api/graphs/{gid}/preview",
                       json={"conditions": CONDITIONS})
    assert resp.status_code == 422


def test_preview_blocked_by_registry_returns_400_without_data():
    gid = register()["id"]
    put_registry([{"source": "users", "column": "email"}], 0)
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "PREVIEW_BLOCKED"
    assert "rows" not in error and "columns" not in error
    assert error["review"]["blocked"] is True
    mail = next(c for c in error["review"]["columns"] if c["name"] == "mail")
    assert mail["content_reasons"] == [{"node_id": "users", "column": "email"}]


def test_preview_after_clearing_registry_returns_data_at_new_generation():
    gid = register()["id"]
    put_registry([{"source": "users", "column": "email"}], 0)
    assert client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS},
    ).status_code == 400

    put_registry([], 1)
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation"] == 2
    assert body["rows"] == [[1, "a@x.example", "ALL"]]


def test_preview_registered_enforces_scalars_and_row_limits():
    gid = register()["id"]
    # Non-scalar cell: same PREVIEW_NON_SCALAR rejection as the old preview.
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": {"users": [
            {"uid": 1, "email": ["x"], "country": "CN"}]},
              "conditions": CONDITIONS},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_NON_SCALAR"
    # Over the 200-row cap.
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": {"users": [
            {"uid": i, "email": "e", "country": "CN"} for i in range(201)]},
              "conditions": CONDITIONS},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_ROW_LIMIT"


def test_preview_unknown_graph_404_for_well_formed_body():
    # Unknown id with a well-formed body: 404 from the endpoint.
    resp = client.post("/api/graphs/missing/preview",
                       json={"rows": {}, "conditions": {}})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GRAPH_NOT_FOUND"


def test_conditions_are_optional_on_registered_preview():
    gid = register()["id"]
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": {"users": [
            {"uid": 1, "email": "a@x.example", "country": "CN"}]}},
    )
    assert resp.status_code == 400
    # Without conditions the closure's filter still must be covered.
    assert resp.json()["error"]["code"] == "PREVIEW_FILTER_COVERAGE"


def test_preview_uses_current_conclusion_not_stale_safety():
    # Mark the filter's predicate column: row influence blocks every column.
    gid = register()["id"]
    put_registry([{"source": "users", "column": "country"}], 0)
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_BLOCKED"


# ------------------------- concurrency ordering ----------------------------


def test_preview_and_replacement_never_mix_registry_and_conclusion(monkeypatch):
    """A preview either fully precedes or fully follows a replacement.

    Hold the store lock inside the preview's evaluation so a replacement
    blocks: the in-flight preview must complete under gen 0 (safe, data
    returned), then the replacement commits gen 1.
    """
    gid = register()["id"]
    entered = threading.Event()
    release = threading.Event()
    import app.registry as reg_mod

    original_preview = reg_mod.run_preview

    def slow_preview(request, extra_sensitive):
        entered.set()
        assert release.wait(timeout=5)
        return original_preview(request, extra_sensitive)

    monkeypatch.setattr(reg_mod, "run_preview", slow_preview)

    outcomes = {}

    def do_preview():
        outcomes["preview"] = registry.preview_graph(gid, ROWS, CONDITIONS)

    worker = threading.Thread(target=do_preview)
    worker.start()
    assert entered.wait(timeout=5)
    # The replacement cannot commit while the preview holds the lock.
    pending = threading.Thread(
        target=lambda: outcomes.setdefault(
            "replace",
            registry.replace_registry([("users", "email")], 0),
        )
    )
    pending.start()
    # Give the replacement a chance to prove it is queued behind the lock.
    assert "replace" not in outcomes
    release.set()
    worker.join(timeout=5)
    pending.join(timeout=5)

    # The in-flight preview used the gen-0 snapshot: safe data returned.
    assert outcomes["preview"]["generation"] == 0
    assert outcomes["preview"]["rows"] == [[1, "a@x.example", "ALL"]]
    # Then the replacement applied under the same total order.
    assert outcomes["replace"]["generation"] == 1
    assert outcomes["replace"]["reviews"][gid]["blocked"] is True
    # A follow-up preview is blocked by the new conclusion.
    resp = client.post(
        f"/api/graphs/{gid}/preview",
        json={"rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_BLOCKED"


def test_concurrent_replacements_serialize_one_stales_out():
    register()
    barrier = threading.Barrier(2)
    outcomes = []

    def replace(generation):
        barrier.wait()
        try:
            registry.replace_registry(
                [("users", "email")], generation)
            outcomes.append(200)
        except registry.StaleGeneration:
            outcomes.append(409)

    t1 = threading.Thread(target=replace, args=(0,))
    t2 = threading.Thread(target=replace, args=(0,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert sorted(outcomes) == [200, 409]
    # Exactly one increment happened; the loser changed nothing.
    assert client.get("/api/sensitive-columns").json()["generation"] == 1
    assert client.get("/api/sensitive-columns").json()["entries"] == [
        {"source": "users", "column": "email"}]


def test_concurrent_registration_then_replacement_are_consistent():
    # A graph registered while gen 0 is in effect is immediately re-reviewed
    # when the replacement that follows it commits.
    gid = register()["id"]
    put_registry([{"source": "users", "column": "email"}], 0)
    # New registration under gen 1 is blocked right away.
    new_gid = register()["id"]
    body = client.get(f"/api/graphs/{new_gid}").json()
    assert body["generation"] == 1
    assert body["review"]["blocked"] is True
    assert new_gid != gid


# ------------------------------ persistence ---------------------------------


def test_state_survives_store_reopen(tmp_path):
    db_path = str(tmp_path / "persist.db")
    reset_store(db_path)
    gid = register()["id"]
    put_registry([{"source": "users", "column": "email"}], 0)
    assert client.get(f"/api/graphs/{gid}").json()["review"]["blocked"] is True

    # Simulate a restart: drop the process-wide store and reopen the same file.
    reset_store(db_path)
    body = client.get("/api/sensitive-columns").json()
    assert body["generation"] == 1
    assert body["entries"] == [{"source": "users", "column": "email"}]
    got = client.get(f"/api/graphs/{gid}").json()
    assert got["nodes"] == GRAPH["nodes"]
    assert got["generation"] == 1
    assert got["review"]["blocked"] is True


# --------------------------- legacy endpoints ------------------------------


def test_legacy_review_and_preview_ignore_registry():
    register()
    # The registry blocks registered graphs ...
    put_registry([{"source": "users", "column": "email"}], 0)
    # ... but /api/review still follows in-graph marks alone (clean graph).
    resp = client.post("/api/review", json=GRAPH)
    assert resp.status_code == 200
    assert resp.json()["blocked"] is False

    # And /api/preview likewise still serves sample data.
    resp = client.post(
        "/api/preview",
        json={**GRAPH, "rows": ROWS, "conditions": CONDITIONS},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"] == [[1, "a@x.example", "ALL"]]


def test_engine_extra_sensitive_matches_review_helper():
    # Direct engine check: registry OR is exactly review(..., extra_sensitive).
    request = ReviewRequest.model_validate(GRAPH)
    result = review(request, frozenset({("users", "email")}))
    mail = next(c for c in result["columns"] if c["name"] == "mail")
    assert mail["blocked"] is True
    # Without the entry the same graph is safe.
    assert review(ReviewRequest.model_validate(GRAPH))["blocked"] is False
