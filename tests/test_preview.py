import json

import pytest
from fastapi.testclient import TestClient

from app.engine import ValidationError, preview, review
from app.main import app
from app.models import PreviewRequest, ReviewRequest
from pydantic import ValidationError as PydanticValidationError


def call(payload):
    return preview(PreviewRequest.model_validate(payload))


def expect_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        call(payload)
    assert exc.value.code == code
    return exc.value


# Shared graph fragments -----------------------------------------------------


# Source -> filter(country) -> projection export, mirroring the README graph.
# Nothing is sensitive, so previews may return sample data.
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
        {"uid": 3, "email": None, "country": "CN"},
    ]
}
CONDITIONS = {"only_cn": {"country": "CN"}}


# ----------------------------- happy paths ---------------------------------


def test_preview_runs_filter_projection_with_export_column_order():
    res = call({**GRAPH, "rows": ROWS, "conditions": CONDITIONS})
    assert res["columns"] == ["user_id", "mail", "bucket"]
    # Survivors keep source order; constants fill every row; null passes
    # through untouched.
    assert res["rows"] == [[1, "a@x.example", "ALL"], [3, None, "ALL"]]
    assert res["review"]["blocked"] is False
    assert [c["name"] for c in res["review"]["columns"]] == [
        "user_id",
        "mail",
        "bucket",
    ]


def test_preview_conditions_default_to_empty_and_no_filter():
    nodes = [
        {"id": "s", "type": "source",
         "columns": [{"name": "a"}, {"name": "b"}]},
        {"id": "p", "type": "projection", "input": "s",
         "columns": [{"name": "a", "copy": "a"}]},
    ]
    res = call({"nodes": nodes, "export": "p",
                "rows": {"s": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}})
    assert res["rows"] == [[1], [3]]


def test_preview_empty_source_returns_empty_rows():
    res = call({**GRAPH, "rows": {"users": []}, "conditions": CONDITIONS})
    assert res["columns"] == ["user_id", "mail", "bucket"]
    assert res["rows"] == []


def test_preview_filter_null_matches_null_and_anded_predicates():
    nodes = [
        {"id": "s", "type": "source",
         "columns": [{"name": "a"}, {"name": "b"}]},
        {"id": "f", "type": "filter", "input": "s",
         "references": ["a", "b"]},
    ]
    rows = {"s": [
        {"a": None, "b": None},
        {"a": None, "b": 1},
        {"a": 1, "b": None},
        {"a": 1, "b": 1},
    ]}
    # null = null matches; the second reference is ANDed.
    res = call({"nodes": nodes, "export": "f", "rows": rows,
                "conditions": {"f": {"a": None, "b": None}}})
    assert res["rows"] == [[None, None]]

    res = call({"nodes": nodes, "export": "f", "rows": rows,
                "conditions": {"f": {"a": 1, "b": 1}}})
    assert res["rows"] == [[1, 1]]


def test_preview_equality_follows_json_type_and_value():
    nodes = [
        {"id": "s", "type": "source",
         "columns": [{"name": "v"}]},
        {"id": "f", "type": "filter", "input": "s",
         "references": ["v"]},
    ]
    rows = {"s": [{"v": 1}, {"v": 1.0}, {"v": True}, {"v": "1"}, {"v": None}]}
    # Numeric equality: 1 and 1.0 survive; booleans and the string "1" do not.
    res = call({"nodes": nodes, "export": "f", "rows": rows,
                "conditions": {"f": {"v": 1}}})
    assert res["rows"] == [[1], [1.0]]

    # Boolean never equals number.
    res = call({"nodes": nodes, "export": "f", "rows": rows,
                "conditions": {"f": {"v": True}}})
    assert res["rows"] == [[True]]


def test_preview_inner_join_null_keys_duplicates_and_order():
    payload = {
        "nodes": [
            {"id": "l", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {"id": "r", "type": "source",
             "columns": [{"name": "k"}, {"name": "w"}]},
            {"id": "j", "type": "join",
             "left_input": "l", "right_input": "r",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "k", "side": "left", "column": "k"},
                 {"alias": "v", "side": "left", "column": "v"},
                 {"alias": "w", "side": "right", "column": "w"},
             ]},
        ],
        "export": "j",
        "rows": {
            # 1 matches the numeric 1.0 key on the right; True matches only
            # the boolean True; null keys match nothing (inner join).
            "l": [
                {"k": 1, "v": "a"},
                {"k": 1, "v": "b"},
                {"k": None, "v": "n"},
                {"k": True, "v": "t"},
                {"k": 1.0, "v": "one"},
            ],
            "r": [
                {"k": 1.0, "w": "x"},
                {"k": None, "w": "y"},
                {"k": True, "w": "z"},
            ],
        },
    }
    res = call(payload)
    # Outer loop = left rows in order; inner loop = right rows in order;
    # every duplicate-key combination is kept.
    assert res["rows"] == [
        [1, "a", "x"],
        [1, "b", "x"],
        [True, "t", "z"],
        [1.0, "one", "x"],
    ]


def test_preview_join_no_match_returns_empty():
    payload = {
        "nodes": [
            {"id": "l", "type": "source", "columns": [{"name": "k"}]},
            {"id": "r", "type": "source", "columns": [{"name": "k"}]},
            {"id": "j", "type": "join",
             "left_input": "l", "right_input": "r",
             "left_key": "k", "right_key": "k",
             "outputs": [{"alias": "k", "side": "left", "column": "k"}]},
        ],
        "export": "j",
        "rows": {"l": [{"k": 1}], "r": [{"k": 2}]},
    }
    res = call(payload)
    assert res["rows"] == []


def _big_int_join_payload(left_rows, right_rows):
    """Two non-sensitive sources joined on k; outputs k, v, w."""
    return {
        "nodes": [
            {"id": "l", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {"id": "r", "type": "source",
             "columns": [{"name": "k"}, {"name": "w"}]},
            {"id": "j", "type": "join",
             "left_input": "l", "right_input": "r",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "k", "side": "left", "column": "k"},
                 {"alias": "v", "side": "left", "column": "v"},
                 {"alias": "w", "side": "right", "column": "w"},
             ]},
        ],
        "export": "j",
        "rows": {"l": left_rows, "r": right_rows},
    }


def test_preview_join_consecutive_integers_above_2p53_do_not_match():
    # float coercion used to collapse both keys into the same value, wrongly
    # returning the left row; exact integer comparison must keep them apart.
    payload = _big_int_join_payload(
        [{"k": 9007199254740992, "v": "a"}],
        [{"k": 9007199254740993, "w": "b"}],
    )
    res = call(payload)
    assert res["rows"] == []

    # The large integer still matches itself.
    payload = _big_int_join_payload(
        [{"k": 9007199254740993, "v": "a"}],
        [{"k": 9007199254740993, "w": "b"}],
    )
    res = call(payload)
    assert res["rows"] == [[9007199254740993, "a", "b"]]
    assert isinstance(res["rows"][0][0], int)


def test_preview_join_int_and_float_compare_by_exact_value():
    # Exactly representable integers still equal their float value ...
    payload = _big_int_join_payload(
        [{"k": 9007199254740992, "v": "a"}],
        [{"k": 9007199254740992.0, "w": "b"}],
    )
    assert call(payload)["rows"] == [[9007199254740992, "a", "b"]]

    # ... but the integer 2**53+1 is not equal to the float 2**53 (which the
    # float key must round-trip to), in either join direction.
    payload = _big_int_join_payload(
        [{"k": 9007199254740993, "v": "a"}],
        [{"k": 9007199254740992.0, "w": "b"}],
    )
    assert call(payload)["rows"] == []
    payload = _big_int_join_payload(
        [{"k": 9007199254740992.0, "v": "a"}],
        [{"k": 9007199254740993, "w": "b"}],
    )
    assert call(payload)["rows"] == []


def test_preview_join_huge_integer_keys_match_without_overflow():
    # 10**400 lies far beyond the finite-float range; converting either key to
    # float used to raise OverflowError (HTTP 500). Both sides carry the same
    # decimal JSON integer, so the join must return the single combination.
    big = 10 ** 400
    payload = _big_int_join_payload(
        [{"k": big, "v": "a"}],
        [{"k": big, "w": "b"}],
    )
    res = call(payload)
    assert len(res["rows"]) == 1
    assert res["rows"][0] == [big, "a", "b"]
    assert isinstance(res["rows"][0][0], int)

    # A huge integer key never matches a finite float or a neighboring integer.
    payload = _big_int_join_payload(
        [{"k": big, "v": "a"}],
        [{"k": 1.0, "w": "x"}, {"k": big + 1, "w": "y"}],
    )
    assert call(payload)["rows"] == []


def test_preview_join_huge_integer_duplicate_keys_keep_combinations_and_order():
    big = 10 ** 400
    payload = _big_int_join_payload(
        [{"k": big, "v": "a"}, {"k": big, "v": "b"}],
        [{"k": big, "w": "x"}, {"k": big, "w": "y"}],
    )
    assert call(payload)["rows"] == [
        [big, "a", "x"], [big, "a", "y"],
        [big, "b", "x"], [big, "b", "y"],
    ]


def test_preview_filter_compares_huge_integers_exactly():
    nodes = [
        {"id": "s", "type": "source", "columns": [{"name": "k"}]},
        {"id": "f", "type": "filter", "input": "s", "references": ["k"]},
    ]
    big = 10 ** 400
    rows = {"s": [{"k": big}, {"k": big + 1}, {"k": 1}, {"k": 1.0}, {"k": True}]}
    res = call({"nodes": nodes, "export": "f", "rows": rows,
                "conditions": {"f": {"k": big}}})
    assert res["rows"] == [[big]]
    res = call({"nodes": nodes, "export": "f", "rows": rows,
                "conditions": {"f": {"k": 1}}})
    assert res["rows"] == [[1], [1.0]]


def test_preview_self_join_is_supported_with_one_rows_entry():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {"id": "j", "type": "join",
             "left_input": "s", "right_input": "s",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "v1", "side": "left", "column": "v"},
                 {"alias": "v2", "side": "right", "column": "v"},
             ]},
        ],
        "export": "j",
        "rows": {"s": [{"k": 1, "v": "a"}, {"k": 1, "v": "b"}]},
    }
    res = call(payload)
    # Same relation on both sides; the rows entry is supplied exactly once.
    assert res["rows"] == [["a", "a"], ["a", "b"], ["b", "a"], ["b", "b"]]


def test_preview_only_closure_sources_and_filters_must_be_supplied():
    # An unreachable branch (extra source + filter) must not be covered.
    nodes = list(GRAPH["nodes"]) + [
        {"id": "other", "type": "source",
         "columns": [{"name": "x"}]},
        {"id": "of", "type": "filter", "input": "other",
         "references": ["x"]},
    ]
    # Extra rows entry for an unused source is rejected.
    expect_error(
        {**GRAPH, "nodes": nodes, "rows": {**ROWS, "other": [{"x": 1}]},
         "conditions": CONDITIONS},
        "PREVIEW_SOURCE_COVERAGE",
    )
    # Extra condition entry for an unused filter is rejected.
    expect_error(
        {**GRAPH, "nodes": nodes, "rows": ROWS,
         "conditions": {**CONDITIONS, "of": {"x": 1}}},
        "PREVIEW_FILTER_COVERAGE",
    )
    # Without the extras the unvalidated-on-export branch is fine, even though
    # the whole graph is still validated under the original rules.
    res = call({"nodes": nodes, "export": GRAPH["export"], "rows": ROWS,
                "conditions": CONDITIONS})
    assert len(res["rows"]) == 2


def test_preview_does_not_mutate_inputs_or_shared_branch():
    # A source shared by a filter branch feeding both a join and the export:
    # materialization must read the shared relation, not modify it.
    payload = {
        "nodes": [
            {"id": "s1", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {"id": "f", "type": "filter", "input": "s1",
             "references": ["v"]},
            {"id": "s2", "type": "source",
             "columns": [{"name": "k"}, {"name": "w"}]},
            {"id": "j", "type": "join",
             "left_input": "f", "right_input": "s2",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "v", "side": "left", "column": "v"},
                 {"alias": "w", "side": "right", "column": "w"},
             ]},
        ],
        "export": "j",
        "rows": {
            "s1": [{"k": 1, "v": "x"}, {"k": 2, "v": "y"}, {"k": 1, "v": "x"}],
            "s2": [{"k": 1, "w": "p"}, {"k": 1, "w": "q"}],
        },
        "conditions": {"f": {"v": "x"}},
    }
    rows_before = json.loads(json.dumps(payload["rows"]))
    cond_before = json.loads(json.dumps(payload["conditions"]))
    res = call(payload)
    assert payload["rows"] == rows_before
    assert payload["conditions"] == cond_before
    # Two filtered left rows (k=1 duplicated) each combine with two right
    # rows, left-major order.
    assert res["rows"] == [["x", "p"], ["x", "q"], ["x", "p"], ["x", "q"]]


def test_preview_columns_follow_export_node_order_including_repeated_outputs():
    nodes = [
        {"id": "s", "type": "source", "columns": [{"name": "a"}]},
        {"id": "j", "type": "join",
         "left_input": "s", "right_input": "s",
         "left_key": "a", "right_key": "a",
         "outputs": [
             {"alias": "a1", "side": "left", "column": "a"},
             {"alias": "a2", "side": "right", "column": "a"},
         ]},
    ]
    res = call({"nodes": nodes, "export": "j",
                "rows": {"s": [{"a": 7}]}})
    assert res["columns"] == ["a1", "a2"]
    assert res["rows"] == [[7, 7]]


# ----------------------------- rejection cases -----------------------------


def test_preview_missing_source_rows_rejected():
    exc = expect_error({**GRAPH, "rows": {}, "conditions": CONDITIONS},
                       "PREVIEW_SOURCE_COVERAGE")
    assert exc.detail()["missing"] == ["users"]
    assert exc.detail()["extra"] == []


def test_preview_missing_filter_condition_rejected():
    exc = expect_error({**GRAPH, "rows": ROWS, "conditions": {}},
                       "PREVIEW_FILTER_COVERAGE")
    assert exc.detail()["missing"] == ["only_cn"]
    assert exc.detail()["extra"] == []


def test_preview_row_keys_must_cover_all_source_columns():
    rows = {"users": [
        {"uid": 1, "country": "CN"},                       # email missing
        {"uid": 2, "email": "b", "country": "US", "x": 9},  # x extra
    ]}
    exc = expect_error({**GRAPH, "rows": rows, "conditions": CONDITIONS},
                       "PREVIEW_ROW_KEYS")
    detail = exc.detail()
    assert detail["node_id"] == "users"
    assert detail["row_index"] == 0
    assert detail["missing"] == ["email"]
    assert detail["extra"] == []


def test_preview_condition_keys_must_equal_references():
    # Missing and extra keys at once.
    conditions = {"only_cn": {"other": "CN"}}
    exc = expect_error({**GRAPH, "rows": ROWS, "conditions": conditions},
                       "PREVIEW_CONDITION_KEYS")
    detail = exc.detail()
    assert detail["missing"] == ["country"]
    assert detail["extra"] == ["other"]


def test_preview_non_scalar_cell_rejected():
    rows = {"users": [
        {"uid": 1, "email": ["a"], "country": "CN"},
    ]}
    exc = expect_error({**GRAPH, "rows": rows, "conditions": CONDITIONS},
                       "PREVIEW_NON_SCALAR")
    detail = exc.detail()
    assert detail["kind"] == "cell"
    assert detail["column"] == "email"
    assert detail["row_index"] == 0


def test_preview_non_scalar_condition_value_rejected():
    conditions = {"only_cn": {"country": {"x": 1}}}
    exc = expect_error({**GRAPH, "rows": ROWS, "conditions": conditions},
                       "PREVIEW_NON_SCALAR")
    assert exc.detail()["kind"] == "condition"
    assert exc.detail()["column"] == "country"


def test_preview_non_scalar_projection_constant_rejected():
    nodes = [
        {"id": "s", "type": "source", "columns": [{"name": "a"}]},
        {"id": "p", "type": "projection", "input": "s",
         "columns": [{"name": "c", "constant": [1, 2]}]},
    ]
    exc = expect_error({"nodes": nodes, "export": "p",
                        "rows": {"s": [{"a": 1}]}},
                       "PREVIEW_NON_SCALAR")
    assert exc.detail()["kind"] == "constant"
    assert exc.detail()["column"] == "c"


def test_preview_scalar_constant_range_keeps_null_and_numbers():
    for value in (None, "x", 1, 1.5, True, False):
        nodes = [
            {"id": "s", "type": "source", "columns": [{"name": "a"}]},
            {"id": "p", "type": "projection", "input": "s",
             "columns": [{"name": "c", "constant": value}]},
        ]
        res = call({"nodes": nodes, "export": "p",
                    "rows": {"s": [{"a": 1}]}})
        assert res["rows"] == [[value]]


def test_preview_source_row_limit_rejected():
    rows = {"users": [
        {"uid": i, "email": "e", "country": "CN"} for i in range(201)
    ]}
    exc = expect_error({**GRAPH, "rows": rows, "conditions": CONDITIONS},
                       "PREVIEW_ROW_LIMIT")
    detail = exc.detail()
    assert detail["node_id"] == "users"
    assert detail["row_count"] == 201
    assert detail["limit"] == 200


def test_preview_intermediate_row_limit_rejected():
    # 200 left rows and 200 right rows with the same key would multiply to
    # 40,000; the cap rejects the whole request without partial data.
    payload = {
        "nodes": [
            {"id": "l", "type": "source", "columns": [{"name": "k"}]},
            {"id": "r", "type": "source", "columns": [{"name": "k"}]},
            {"id": "j", "type": "join",
             "left_input": "l", "right_input": "r",
             "left_key": "k", "right_key": "k",
             "outputs": [{"alias": "k", "side": "left", "column": "k"}]},
        ],
        "export": "j",
        "rows": {
            "l": [{"k": 1} for _ in range(200)],
            "r": [{"k": 1} for _ in range(200)],
        },
    }
    exc = expect_error(payload, "PREVIEW_ROW_LIMIT")
    assert exc.detail()["node_id"] == "j"
    assert exc.detail()["row_count"] == 40000


def test_preview_200_rows_are_allowed_everywhere():
    nodes = [
        {"id": "s", "type": "source", "columns": [{"name": "a"}]},
        {"id": "f", "type": "filter", "input": "s", "references": ["a"]},
        {"id": "p", "type": "projection", "input": "f",
         "columns": [{"name": "a", "copy": "a"}]},
    ]
    payload = {
        "nodes": nodes,
        "export": "p",
        "rows": {"s": [{"a": i} for i in range(200)]},
        "conditions": {"f": {"a": 1}},
    }
    res = call(payload)
    assert res["rows"] == [[1]]


def test_preview_blocked_export_returns_review_reasons_only():
    # Sensitive content reaches an export column.
    nodes = [
        {"id": "s", "type": "source",
         "columns": [{"name": "secret", "sensitive": True}]},
        {"id": "p", "type": "projection", "input": "s",
         "columns": [{"name": "leak", "copy": "secret"}]},
    ]
    payload = {"nodes": nodes, "export": "p",
               "rows": {"s": [{"secret": "ssn-1"}]}}
    with pytest.raises(ValidationError) as exc:
        call(payload)
    assert exc.value.code == "PREVIEW_BLOCKED"
    detail = exc.value.detail()
    assert "columns" not in detail and "rows" not in detail
    assert detail["review"]["blocked"] is True
    assert detail["review"]["columns"][0]["content_reasons"] == [
        {"node_id": "s", "column": "secret"}
    ]


def test_preview_blocked_by_row_influence_also_hides_samples():
    nodes = [
        {"id": "s", "type": "source",
         "columns": [{"name": "id_card", "sensitive": True},
                     {"name": "score"}]},
        {"id": "f", "type": "filter", "input": "s",
         "references": ["id_card"]},
        {"id": "p", "type": "projection", "input": "f",
         "columns": [{"name": "score", "copy": "score"},
                     {"name": "lit", "constant": 1}]},
    ]
    payload = {
        "nodes": nodes,
        "export": "p",
        "rows": {"s": [{"id_card": "x", "score": 9}]},
        "conditions": {"f": {"id_card": "x"}},
    }
    exc = expect_error(payload, "PREVIEW_BLOCKED")
    for column in exc.detail()["review"]["columns"]:
        assert column["row_reasons"] == [{"node_id": "s",
                                          "column": "id_card"}]


def test_preview_shares_original_graph_validation():
    # Invalid reference in a branch the export does not even use still rejects.
    nodes = [
        {"id": "s", "type": "source", "columns": [{"name": "a"}]},
        {"id": "p", "type": "projection", "input": "s",
         "columns": [{"name": "a", "copy": "a"}]},
        {"id": "f", "type": "filter", "input": "s",
         "references": ["ghost"]},
    ]
    expect_error({"nodes": nodes, "export": "p", "rows": {"s": [{"a": 1}]}},
                 "INVALID_REFERENCE")
    expect_error({"nodes": nodes, "export": "missing",
                  "rows": {"s": [{"a": 1}]}},
                 "INVALID_REFERENCE")
    # Cycle.
    cyclic = [
        {"id": "a", "type": "filter", "input": "b", "references": ["x"]},
        {"id": "b", "type": "filter", "input": "a", "references": ["x"]},
    ]
    expect_error({"nodes": cyclic, "export": "a",
                  "rows": {}, "conditions": {}},
                 "CYCLE")


def test_preview_rows_unknown_node_key_rejected():
    exc = expect_error(
        {**GRAPH, "rows": {**ROWS, "ghost": []}, "conditions": CONDITIONS},
        "PREVIEW_SOURCE_COVERAGE",
    )
    assert exc.detail()["extra"] == ["ghost"]


def test_preview_conditions_unknown_node_key_rejected():
    exc = expect_error(
        {**GRAPH, "rows": ROWS,
         "conditions": {**CONDITIONS, "ghost": {"x": 1}}},
        "PREVIEW_FILTER_COVERAGE",
    )
    assert exc.detail()["extra"] == ["ghost"]


def test_preview_rows_for_non_source_node_rejected():
    # A filter id must not appear in rows.
    expect_error(
        {**GRAPH, "rows": {**ROWS, "only_cn": []},
         "conditions": CONDITIONS},
        "PREVIEW_SOURCE_COVERAGE",
    )
    # A source id must not appear in conditions.
    expect_error(
        {**GRAPH, "rows": ROWS,
         "conditions": {**CONDITIONS, "users": {"uid": 1}}},
        "PREVIEW_FILTER_COVERAGE",
    )


# ----------------------------- schema validation ---------------------------


def test_preview_schema_errors_return_422():
    base = {**GRAPH, "rows": ROWS, "conditions": CONDITIONS}
    with pytest.raises(PydanticValidationError):
        PreviewRequest.model_validate({k: v for k, v in base.items()
                                       if k != "rows"})
    with pytest.raises(PydanticValidationError):
        PreviewRequest.model_validate({**base, "nope": 1})
    with pytest.raises(PydanticValidationError):
        PreviewRequest.model_validate({**base, "nodes": []})
    with pytest.raises(PydanticValidationError):
        # Wrong structural type: the rows map must map id -> list of objects.
        PreviewRequest.model_validate({**base, "rows": {"users": {}}})
    with pytest.raises(PydanticValidationError):
        PreviewRequest.model_validate({**base, "conditions": []})
    with pytest.raises(PydanticValidationError):
        PreviewRequest.model_validate({**base, "rows": [{"a": 1}]})


def test_preview_constant_range_unchanged_on_other_endpoints():
    # A non-scalar constant stays legal on /api/review and /api/revise.
    nodes = [
        {"id": "s", "type": "source", "columns": [{"name": "a"}]},
        {"id": "p", "type": "projection", "input": "s",
         "columns": [{"name": "c", "constant": {"nested": [1, 2]}}]},
    ]
    res = review(ReviewRequest.model_validate({"nodes": nodes, "export": "p"}))
    assert res["blocked"] is False
    # And it is still accepted by the preview request models' graph schema;
    # only the preview engine rejects it.
    PreviewRequest.model_validate(
        {"nodes": nodes, "export": "p", "rows": {"s": [{"a": 1}]}}
    )


# ----------------------------- HTTP endpoint -------------------------------


client = TestClient(app)


def test_http_preview_success():
    resp = client.post("/api/preview",
                       json={**GRAPH, "rows": ROWS, "conditions": CONDITIONS})
    assert resp.status_code == 200
    body = resp.json()
    assert body["export"] == "export_users"
    assert body["columns"] == ["user_id", "mail", "bucket"]
    assert body["rows"] == [[1, "a@x.example", "ALL"], [3, None, "ALL"]]
    assert body["review"]["blocked"] is False


def test_http_preview_from_example_file():
    with open("examples/preview_request.json") as fh:
        payload = json.load(fh)
    resp = client.post("/api/preview", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["columns"] == ["user_id", "mail", "bucket"]
    # uid 4 carries the lowercase "cn" string, which does not equal "CN".
    assert body["rows"] == [
        [1, "a@x.example", "ALL"],
        [3, None, "ALL"],
    ]


def test_http_preview_large_integer_keys_compare_exactly():
    # The two distinct integers beyond 2**53 must not be conflated by float
    # coercion: the response is 200 with an empty rows array.
    payload = {
        "nodes": [
            {"id": "l", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {"id": "r", "type": "source",
             "columns": [{"name": "k"}, {"name": "w"}]},
            {"id": "j", "type": "join",
             "left_input": "l", "right_input": "r",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "k", "side": "left", "column": "k"},
                 {"alias": "v", "side": "left", "column": "v"},
                 {"alias": "w", "side": "right", "column": "w"},
             ]},
        ],
        "export": "j",
        "rows": {
            "l": [{"k": 9007199254740992, "v": "a"}],
            "r": [{"k": 9007199254740993, "w": "b"}],
        },
    }
    resp = client.post("/api/preview", json=payload)
    assert resp.status_code == 200
    assert resp.json()["rows"] == []


def test_http_preview_huge_integer_key_match_roundtrips_decimal():
    # A decimal JSON integer beyond the finite-float range must match on both
    # sides instead of failing with 500/INTERNAL, and the joined key must come
    # back verbatim as the same decimal JSON integer (no float serialization).
    big = 10 ** 400
    big_literal = str(big)
    payload = {
        "nodes": [
            {"id": "l", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {"id": "r", "type": "source",
             "columns": [{"name": "k"}, {"name": "w"}]},
            {"id": "j", "type": "join",
             "left_input": "l", "right_input": "r",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "k", "side": "left", "column": "k"},
                 {"alias": "v", "side": "left", "column": "v"},
                 {"alias": "w", "side": "right", "column": "w"},
             ]},
        ],
        "export": "j",
        "rows": {
            "l": [{"k": big, "v": "a"}],
            "r": [{"k": big, "w": "b"}],
        },
    }
    resp = client.post("/api/preview", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == [[big, "a", "b"]]
    # Raw JSON transport: the value stays an integer literal of full length.
    assert big_literal in resp.text
    assert (big_literal + ".0") not in resp.text
    assert "1e+400" not in resp.text.replace(" ", "")


def test_http_preview_blocked_returns_400_without_sample_data():
    nodes = [
        {"id": "s", "type": "source",
         "columns": [{"name": "id_card", "sensitive": True},
                     {"name": "score"}]},
        {"id": "f", "type": "filter", "input": "s",
         "references": ["id_card"]},
        {"id": "p", "type": "projection", "input": "f",
         "columns": [{"name": "score", "copy": "score"},
                     {"name": "lit", "constant": 1}]},
    ]
    payload = {
        "nodes": nodes,
        "export": "p",
        "rows": {"s": [{"id_card": "x", "score": 9}]},
        "conditions": {"f": {"id_card": "x"}},
    }
    resp = client.post("/api/preview", json=payload)
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "PREVIEW_BLOCKED"
    assert "rows" not in body and "columns" not in body
    assert body["review"]["blocked"] is True
    assert body["review"]["columns"][1]["row_reasons"] == [
        {"node_id": "s", "column": "id_card"}
    ]


def test_http_preview_semantic_errors_return_400():
    resp = client.post(
        "/api/preview",
        json={**GRAPH, "rows": {"users": []}, "conditions": {}},
    )
    # Missing filter condition surfaces first.
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_FILTER_COVERAGE"

    resp = client.post(
        "/api/preview",
        json={**GRAPH, "rows": ROWS,
              "conditions": {"only_cn": {"country": ["CN"]}}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PREVIEW_NON_SCALAR"


def test_http_preview_schema_error_returns_422():
    # Missing the required rows map, or unknown fields: 422.
    resp = client.post("/api/preview",
                       json={**GRAPH, "conditions": CONDITIONS})
    assert resp.status_code == 422

    resp = client.post(
        "/api/preview",
        json={**GRAPH, "rows": ROWS, "conditions": CONDITIONS, "nope": 1},
    )
    assert resp.status_code == 422


def test_http_other_endpoints_unchanged():
    # The review endpoint keeps working on the classic graph (email sensitive
    # in the README example) and exposes no preview fields.
    resp = client.post("/api/review", json={
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a", "sensitive": True}]},
        ],
        "export": "s",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    assert "rows" not in body
