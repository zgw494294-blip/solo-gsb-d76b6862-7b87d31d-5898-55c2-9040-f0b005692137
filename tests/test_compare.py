import pytest
from fastapi.testclient import TestClient

from app.engine import ValidationError, compare
from app.main import app
from app.models import CompareRequest
from pydantic import ValidationError as PydanticValidationError


def call(payload):
    return compare(CompareRequest.model_validate(payload))


def source(node_id, columns):
    return {
        "id": node_id,
        "type": "source",
        "columns": [
            {"name": name, "sensitive": bool(sensitive)}
            for name, sensitive in columns
        ],
    }


def projection(node_id, input_id, columns):
    return {
        "id": node_id,
        "type": "projection",
        "input": input_id,
        "columns": columns,
    }


def pcol(name, copy=None, constant=...):
    spec = {"name": name}
    if constant is ...:
        spec["copy"] = copy
    else:
        spec["constant"] = constant
    return spec


def filter_node(node_id, input_id, references):
    return {
        "id": node_id,
        "type": "filter",
        "input": input_id,
        "references": references,
    }


def graph(nodes, export):
    return {"nodes": nodes, "export": export}


def origin(node_id, column):
    return {"node_id": node_id, "column": column}


# ----------------------------- happy paths --------------------------------


def test_response_carries_both_full_reviews_and_a_column_diff():
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x"), pcol("b", "y")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "y"), pcol("b", "y")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})

    # Both sides keep their complete /api/review-shaped verdict.
    assert res["baseline"]["export"] == "e"
    assert res["baseline"]["blocked"] is True
    assert res["candidate"]["export"] == "e"
    assert res["candidate"]["blocked"] is False
    assert [c["name"] for c in res["baseline"]["columns"]] == ["a", "b"]

    diffs = {c["name"]: c for c in res["columns"]}
    assert diffs["a"]["baseline_blocked"] is True
    assert diffs["a"]["candidate_blocked"] is False
    assert diffs["a"]["content_added_reasons"] == []
    assert diffs["a"]["content_removed_reasons"] == [origin("s", "x")]
    assert diffs["a"]["row_added_reasons"] == []
    assert diffs["a"]["row_removed_reasons"] == []
    # The formerly safe column stays safe on both sides.
    assert diffs["b"] == {
        "name": "b",
        "baseline_blocked": False,
        "candidate_blocked": False,
        "content_added_reasons": [],
        "content_removed_reasons": [],
        "row_added_reasons": [],
        "row_removed_reasons": [],
    }


def test_common_and_new_columns_follow_candidate_order_then_deleted_baseline_order():
    baseline = graph(
        [source("s", [("a", True), ("d", True), ("e", False)]),
         projection("exp", "s", [pcol("a", "a"), pcol("d", "d"), pcol("e", "e")])],
        "exp",
    )
    candidate = graph(
        [source("s", [("a", False), ("c", False), ("e", False)]),
         projection("exp", "s", [pcol("c", "c"), pcol("a", "a"), pcol("e", "e")])],
        "exp",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    # Candidate order first (c, a, e), then baseline-only deleted columns in
    # baseline order (d).
    assert [c["name"] for c in res["columns"]] == ["c", "a", "e", "d"]
    new_col = res["columns"][0]
    assert new_col["baseline_blocked"] is None
    assert new_col["candidate_blocked"] is False
    assert new_col["content_added_reasons"] == []
    deleted = res["columns"][-1]
    assert deleted["baseline_blocked"] is True
    assert deleted["candidate_blocked"] is None
    assert deleted["content_removed_reasons"] == [origin("s", "d")]
    assert res["regression"] is False


def test_content_origin_moving_to_row_influence_is_an_added_row_reason():
    # Baseline: s.x flows as *content* into export column a.
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    # Candidate: a now copies the clean y, but a filter references the
    # sensitive x, so s.x becomes a *row* influence on every surviving row.
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         filter_node("f", "s", ["x"]),
         projection("e", "f", [pcol("a", "y")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    col = res["columns"][0]
    assert col["baseline_blocked"] is True
    assert col["candidate_blocked"] is True
    assert col["content_added_reasons"] == []
    assert col["content_removed_reasons"] == [origin("s", "x")]
    assert col["row_added_reasons"] == [origin("s", "x")]
    assert col["row_removed_reasons"] == []


def test_content_to_row_transition_is_a_regression_even_blocked_on_both_sides():
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         filter_node("f", "s", ["x"]),
         projection("e", "f", [pcol("a", "y")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    assert res["regression"] is True


def test_row_origin_moving_to_content_is_likewise_an_added_content_reason():
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         filter_node("f", "s", ["x"]),
         projection("e", "f", [pcol("a", "y")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    col = res["columns"][0]
    assert col["row_removed_reasons"] == [origin("s", "x")]
    assert col["content_added_reasons"] == [origin("s", "x")]
    assert res["regression"] is True


def test_added_content_reason_on_common_column_is_a_regression():
    baseline = graph(
        [source("s", [("x", False), ("y", True)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    col = res["columns"][0]
    assert col["baseline_blocked"] is False
    assert col["candidate_blocked"] is True
    assert col["content_added_reasons"] == [origin("s", "x")]
    assert col["content_removed_reasons"] == []
    assert res["regression"] is True


def test_added_row_reason_via_join_key_on_common_column_is_a_regression():
    left_b = source("l", [("k", False), ("v", False)])
    right = source("r", [("k", False), ("w", False)])
    join_clean = {
        "id": "j",
        "type": "join",
        "left_input": "l",
        "right_input": "r",
        "left_key": "k",
        "right_key": "k",
        "outputs": [{"alias": "v", "side": "left", "column": "v"}],
    }
    baseline = graph([left_b, right, join_clean], "j")
    left_sensitive = source("l", [("k", True), ("v", False)])
    candidate = graph([left_sensitive, right, dict(join_clean)], "j")
    res = call({"baseline": baseline, "candidate": candidate})
    col = res["columns"][0]
    assert col["row_added_reasons"] == [origin("l", "k")]
    assert col["content_added_reasons"] == []
    assert col["baseline_blocked"] is False
    assert col["candidate_blocked"] is True
    assert res["regression"] is True


def test_reasons_are_sorted_by_node_id_then_column_and_deduplicated():
    # Both origins reach the same export column via two copies of the path;
    # the review sets already de-duplicate, and the diff order is sorted.
    baseline = graph(
        [source("z", [("q", True)]),
         projection("e", "z", [pcol("a", "q")])],
        "e",
    )
    candidate = graph(
        [
            source("z", [("q", True)]),
            source("a_src", [("m", True)]),
            source("b_src", [("n", True)]),
            {
                "id": "j",
                "type": "join",
                "left_input": "z",
                "right_input": "a_src",
                "left_key": "q",
                "right_key": "m",
                "outputs": [{"alias": "q", "side": "left", "column": "q"}],
            },
            projection("e", "j", [pcol("a", "q")]),
        ],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    col = res["columns"][0]
    # Baseline content s-equivalent origin z.q stays; join keys add row
    # reasons a_src.m and z.q sorted lexicographically (node, column).
    assert col["row_added_reasons"] == [
        origin("a_src", "m"),
        origin("z", "q"),
    ]
    assert col["content_removed_reasons"] == []
    assert res["regression"] is True


def test_identical_graphs_have_no_added_or_removed_reasons_and_no_regression():
    g = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x"), pcol("b", "y")])],
        "e",
    )
    res = call({"baseline": g, "candidate": g})
    assert res["regression"] is False
    for col in res["columns"]:
        assert col["content_added_reasons"] == []
        assert col["content_removed_reasons"] == []
        assert col["row_added_reasons"] == []
        assert col["row_removed_reasons"] == []
        assert col["baseline_blocked"] == col["candidate_blocked"]


def test_newly_added_safe_column_is_not_a_regression():
    baseline = graph(
        [source("s", [("y", False)]),
         projection("e", "s", [pcol("b", "y")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", False), ("y", False)]),
         projection("e", "s", [pcol("a", "x"), pcol("b", "y")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    assert [c["name"] for c in res["columns"]] == ["a", "b"]
    added = res["columns"][0]
    assert added["baseline_blocked"] is None
    assert added["candidate_blocked"] is False
    assert res["regression"] is False


def test_deleted_blocked_column_is_not_a_regression():
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x"), pcol("b", "y")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("b", "y")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    assert [c["name"] for c in res["columns"]] == ["b", "a"]
    assert res["regression"] is False
    deleted = res["columns"][-1]
    assert deleted["candidate_blocked"] is None
    assert deleted["content_removed_reasons"] == [origin("s", "x")]


def test_newly_added_blocked_column_is_a_regression_even_with_no_common_columns():
    baseline = graph([source("s", [("x", False)])], "s")
    candidate = graph(
        [source("t", [("id_card", True)]),
         projection("p2", "t", [pcol("secret", "id_card")])],
        "p2",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    assert res["regression"] is True
    assert res["columns"][0]["baseline_blocked"] is None
    assert res["columns"][0]["candidate_blocked"] is True
    assert res["columns"][0]["content_added_reasons"] == [
        origin("t", "id_card")
    ]


def test_same_named_origin_in_different_graph_shapes_is_one_source():
    # The intermediate pipeline differs, but the export content origin is the
    # same (node id "s", column "x"): nothing added, nothing removed.
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("mid", "s", [pcol("x", "x"), pcol("y", "y")]),
         projection("e", "mid", [pcol("a", "x")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False), ("z", False)]),
         filter_node("mid", "s", ["z"]),
         projection("e", "mid", [pcol("a", "x")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    col = res["columns"][0]
    assert col["content_added_reasons"] == []
    assert col["content_removed_reasons"] == []
    assert col["row_added_reasons"] == []
    assert res["regression"] is False


def test_removed_row_reason_alone_is_not_a_regression():
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         filter_node("f", "s", ["x"]),
         projection("e", "f", [pcol("a", "y")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "y")])],
        "e",
    )
    res = call({"baseline": baseline, "candidate": candidate})
    col = res["columns"][0]
    assert col["row_removed_reasons"] == [origin("s", "x")]
    assert col["row_added_reasons"] == []
    assert col["candidate_blocked"] is False
    assert res["regression"] is False


def test_input_graphs_are_not_mutated():
    baseline = graph(
        [source("s", [("x", True)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", False)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    import copy

    baseline_snapshot = copy.deepcopy(baseline)
    candidate_snapshot = copy.deepcopy(candidate)
    call({"baseline": baseline, "candidate": candidate})
    assert baseline == baseline_snapshot
    assert candidate == candidate_snapshot


# ----------------------------- rejections ---------------------------------


def test_invalid_baseline_graph_rejects_the_whole_request_with_old_error_shape():
    good = graph([source("s", [("x", False)])], "s")
    bad = graph(
        [source("s", [("x", False)]),
         projection("e", "ghost", [pcol("a", "x")])],
        "e",
    )
    with pytest.raises(ValidationError) as exc_info:
        call({"baseline": bad, "candidate": good})
    assert exc_info.value.code == "INVALID_REFERENCE"
    detail = exc_info.value.detail()
    assert detail["code"] == "INVALID_REFERENCE"
    assert "message" in detail


def test_invalid_candidate_graph_rejects_the_whole_request():
    good = graph([source("s", [("x", False)])], "s")
    bad = graph(
        [source("s", [("x", False), ("x", False)])],
        "s",
    )
    with pytest.raises(ValidationError) as exc_info:
        call({"baseline": good, "candidate": bad})
    assert exc_info.value.code == "COLUMN_CONFLICT"


def test_cycle_in_either_graph_rejects():
    good = graph([source("s", [("x", False)])], "s")
    cyclic = {
        "nodes": [
            {"id": "a", "type": "projection", "input": "b",
             "columns": [pcol("x", "x")]},
            {"id": "b", "type": "filter", "input": "a", "references": ["x"]},
        ],
        "export": "a",
    }
    with pytest.raises(ValidationError) as exc_info:
        call({"baseline": good, "candidate": cyclic})
    assert exc_info.value.code == "CYCLE"
    with pytest.raises(ValidationError) as exc_info:
        call({"baseline": cyclic, "candidate": good})
    assert exc_info.value.code == "CYCLE"


def test_missing_export_node_in_either_graph_rejects():
    good = graph([source("s", [("x", False)])], "s")
    missing_export = graph([source("s", [("x", False)])], "ghost")
    with pytest.raises(ValidationError) as exc_info:
        call({"baseline": missing_export, "candidate": good})
    assert exc_info.value.code == "INVALID_REFERENCE"
    with pytest.raises(ValidationError) as exc_info:
        call({"baseline": good, "candidate": missing_export})
    assert exc_info.value.code == "INVALID_REFERENCE"


def test_baseline_is_validated_before_candidate():
    bad_baseline = graph(
        [source("s", [("x", False), ("x", False)])],
        "s",
    )
    bad_candidate = graph(
        [source("s", [("x", False)]),
         projection("e", "ghost", [pcol("a", "x")])],
        "e",
    )
    with pytest.raises(ValidationError) as exc_info:
        call({"baseline": bad_baseline, "candidate": bad_candidate})
    assert exc_info.value.code == "COLUMN_CONFLICT"


# ----------------------------- schema (422) -------------------------------


def test_unknown_top_level_field_is_a_schema_error():
    payload = {
        "baseline": graph([source("s", [("x", False)])], "s"),
        "candidate": graph([source("s", [("x", False)])], "s"),
        "extra": 1,
    }
    with pytest.raises(PydanticValidationError):
        CompareRequest.model_validate(payload)


def test_missing_side_is_a_schema_error():
    with pytest.raises(PydanticValidationError):
        CompareRequest.model_validate(
            {"baseline": graph([source("s", [("x", False)])], "s")}
        )


def test_more_than_60_nodes_in_either_side_is_a_schema_error():
    nodes = [source("s0", [("x", False)])]
    for i in range(1, 61):
        nodes.append(projection(f"s{i}", f"s{i - 1}", [pcol("x", "x")]))
    over = graph(nodes, "s60")
    good = graph([source("s", [("x", False)])], "s")
    with pytest.raises(PydanticValidationError):
        CompareRequest.model_validate({"baseline": over, "candidate": good})
    with pytest.raises(PydanticValidationError):
        CompareRequest.model_validate({"baseline": good, "candidate": over})


# ----------------------------- HTTP layer ---------------------------------


client = TestClient(app)


def test_http_compare_returns_200_and_diff_payload():
    baseline = graph(
        [source("s", [("x", True), ("y", False)]),
         projection("e", "s", [pcol("a", "x")])],
        "e",
    )
    candidate = graph(
        [source("s", [("x", True), ("y", False)]),
         filter_node("f", "s", ["x"]),
         projection("e", "f", [pcol("a", "y")])],
        "e",
    )
    resp = client.post(
        "/api/compare", json={"baseline": baseline, "candidate": candidate}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["regression"] is True
    col = data["columns"][0]
    assert col["row_added_reasons"] == [{"node_id": "s", "column": "x"}]
    assert data["baseline"]["export"] == "e"
    assert data["candidate"]["export"] == "e"


def test_http_compare_example_file_runs():
    import json
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).resolve().parent.parent / "examples" / "compare_request.json")
        .read_text()
    )
    resp = client.post("/api/compare", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["regression"] is True
    col = data["columns"][0]
    assert col["name"] == "a"
    assert col["content_removed_reasons"] == [{"node_id": "s", "column": "x"}]
    assert col["row_added_reasons"] == [{"node_id": "s", "column": "x"}]


def test_http_invalid_graph_keeps_old_400_error_format():
    good = graph([source("s", [("x", False)])], "s")
    bad = graph(
        [source("s", [("x", False)]),
         projection("e", "ghost", [pcol("a", "x")])],
        "e",
    )
    resp = client.post(
        "/api/compare", json={"baseline": good, "candidate": bad}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REFERENCE"
    assert "message" in body["error"]


def test_http_schema_problem_is_422():
    resp = client.post("/api/compare", json={"baseline": {}})
    assert resp.status_code == 422


def test_old_review_endpoint_still_works_unchanged():
    resp = client.post(
        "/api/review",
        json=graph(
            [source("s", [("x", True)]),
             projection("e", "s", [pcol("a", "x")])],
            "e",
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["blocked"] is True
