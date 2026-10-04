import json

import pytest
from fastapi.testclient import TestClient

from app.engine import (
    ValidationError,
    _compare_renamed_review,
    rename,
    review,
)
from app.main import app
from app.models import RenameRequest, ReviewRequest
from pydantic import ValidationError as PydanticValidationError


def call_rename(payload):
    return rename(RenameRequest.model_validate(payload))


def call_review(payload):
    return review(ReviewRequest.model_validate(payload))


def by_id(nodes):
    return {n["id"]: n for n in nodes}


# source -> filter -> projection export, mirroring the README example.
GRAPH = {
    "nodes": [
        {
            "id": "users",
            "type": "source",
            "columns": [
                {"name": "uid", "sensitive": False},
                {"name": "email", "sensitive": True},
                {"name": "country", "sensitive": False},
            ],
        },
        {
            "id": "only_cn",
            "type": "filter",
            "input": "users",
            "references": ["country"],
        },
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


# ----------------------------- happy paths --------------------------------


def test_basic_rename_propagates_through_filter_into_projection_copies():
    payload = {
        **GRAPH,
        "source": "users",
        "renames": [
            {"from": "uid", "to": "user_id"},
            {"from": "email", "to": "mail_addr"},
            {"from": "country", "to": "region"},
        ],
    }
    res = call_rename(payload)

    nodes = by_id(res["nodes"])
    # Source columns renamed in place; flags and order preserved.
    assert nodes["users"]["columns"] == [
        {"name": "user_id", "sensitive": False},
        {"name": "mail_addr", "sensitive": True},
        {"name": "region", "sensitive": False},
    ]
    # Filter predicate references follow the rename; the node keeps passing
    # every (now renamed) input column through.
    assert nodes["only_cn"] == {
        "id": "only_cn",
        "type": "filter",
        "input": "users",
        "references": ["region"],
    }
    # Projection copy references follow their inputs; output names, the
    # constant value and column order are unchanged -- the rename stops here.
    assert nodes["export_users"] == {
        "id": "export_users",
        "type": "projection",
        "input": "only_cn",
        "columns": [
            {"name": "user_id", "copy": "user_id"},
            {"name": "mail", "copy": "mail_addr"},
            {"name": "bucket", "constant": "ALL"},
        ],
    }

    assert res["export"] == "export_users"
    assert res["source"] == "users"
    assert res["renamed_columns"] == [
        {"from": "uid", "to": "user_id"},
        {"from": "email", "to": "mail_addr"},
        {"from": "country", "to": "region"},
    ]


def test_no_nodes_or_columns_added_or_removed_and_order_preserved():
    payload = {
        **GRAPH,
        "source": "users",
        "renames": [{"from": "uid", "to": "user_id"}],
    }
    res = call_rename(payload)
    assert [n["id"] for n in res["nodes"]] == [
        n["id"] for n in GRAPH["nodes"]
    ]
    new_nodes = by_id(res["nodes"])
    assert [c["name"] for c in new_nodes["users"]["columns"]] == [
        "user_id",
        "email",
        "country",
    ]
    assert [c["name"] for c in new_nodes["export_users"]["columns"]] == [
        "user_id",
        "mail",
        "bucket",
    ]
    # Input edges are untouched.
    assert new_nodes["only_cn"]["input"] == "users"
    assert new_nodes["export_users"]["input"] == "only_cn"


def test_review_blocked_verdict_and_reasons_follow_renamed_columns():
    payload = {
        **GRAPH,
        "source": "users",
        "renames": [{"from": "email", "to": "mail_addr"}],
    }
    res = call_rename(payload)
    cols = {c["name"]: c for c in res["review"]["columns"]}
    assert res["review"]["blocked"] is True
    assert cols["mail"]["blocked"] is True
    # The reason now points at the renamed source column.
    assert cols["mail"]["content_reasons"] == [
        {"node_id": "users", "column": "mail_addr"}
    ]
    assert cols["user_id"]["content_reasons"] == []
    assert cols["bucket"]["content_reasons"] == []


def test_passthrough_export_columns_are_renamed_at_source_export():
    payload = {
        "nodes": [
            {
                "id": "s",
                "type": "source",
                "columns": [
                    {"name": "a", "sensitive": False},
                    {"name": "b", "sensitive": True},
                ],
            }
        ],
        "export": "s",
        "source": "s",
        "renames": [{"from": "a", "to": "aa"}, {"from": "b", "to": "bb"}],
    }
    res = call_rename(payload)
    assert [c["name"] for c in res["review"]["columns"]] == ["aa", "bb"]
    cols = {c["name"]: c for c in res["review"]["columns"]}
    assert cols["bb"]["content_reasons"] == [{"node_id": "s", "column": "bb"}]
    assert cols["bb"]["blocked"] is True
    assert cols["aa"]["blocked"] is False


def test_passthrough_export_columns_are_renamed_at_filter_export():
    payload = {
        "nodes": [
            {
                "id": "s",
                "type": "source",
                "columns": [
                    {"name": "region", "sensitive": False},
                    {"name": "ssn", "sensitive": True},
                ],
            },
            {"id": "f", "type": "filter", "input": "s",
             "references": ["region"]},
        ],
        "export": "f",
        "source": "s",
        "renames": [{"from": "region", "to": "country"},
                    {"from": "ssn", "to": "tax_id"}],
    }
    res = call_rename(payload)
    nodes = by_id(res["nodes"])
    assert nodes["f"]["references"] == ["country"]
    cols = {c["name"]: c for c in res["review"]["columns"]}
    assert set(cols) == {"country", "tax_id"}
    assert cols["tax_id"]["content_reasons"] == [
        {"node_id": "s", "column": "tax_id"}
    ]


def test_row_reasons_follow_rename_through_filter():
    payload = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [
                {"name": "id_card", "sensitive": True},
                {"name": "score", "sensitive": False}]},
            {"id": "f", "type": "filter", "input": "s",
             "references": ["id_card"]},
            {"id": "p", "type": "projection", "input": "f", "columns": [
                {"name": "score", "copy": "score"},
                {"name": "lit", "constant": 1}]},
        ],
        "export": "p",
        "source": "s",
        "renames": [{"from": "id_card", "to": "card_no"}],
    }
    res = call_rename(payload)
    assert res["review"]["blocked"] is True
    for col in res["review"]["columns"]:
        assert col["row_reasons"] == [{"node_id": "s", "column": "card_no"}]


def test_swap_of_two_source_columns_is_allowed():
    payload = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [
                {"name": "a", "sensitive": True},
                {"name": "b", "sensitive": False}]},
            {"id": "f", "type": "filter", "input": "s", "references": ["a"]},
            {"id": "p", "type": "projection", "input": "f", "columns": [
                {"name": "ca", "copy": "a"},
                {"name": "cb", "copy": "b"}]},
        ],
        "export": "p",
        "source": "s",
        "renames": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    res = call_rename(payload)
    nodes = by_id(res["nodes"])
    assert [c["name"] for c in nodes["s"]["columns"]] == ["b", "a"]
    # Sensitive flags stay with their columns: the old 'a' is now 'b'.
    assert nodes["s"]["columns"][0] == {"name": "b", "sensitive": True}
    assert nodes["f"]["references"] == ["b"]
    # Projection copy refs swap so they still point at the same original cols.
    assert nodes["p"]["columns"] == [
        {"name": "ca", "copy": "b"},
        {"name": "cb", "copy": "a"},
    ]
    cols = {c["name"]: c for c in res["review"]["columns"]}
    assert cols["ca"]["content_reasons"] == [{"node_id": "s", "column": "b"}]
    # The filter referenced the sensitive column (old 'a', now 'b'), so the
    # row-level taint lands on every export column; the clean column stays
    # blocked by row reasons alone exactly as in the original graph.
    assert cols["cb"]["blocked"] is True
    assert cols["cb"]["content_reasons"] == []
    assert cols["cb"]["row_reasons"] == [{"node_id": "s", "column": "b"}]


def test_rename_to_same_name_is_a_no_op():
    payload = {
        **GRAPH,
        "source": "users",
        "renames": [{"from": "uid", "to": "uid"}],
    }
    res = call_rename(payload)
    assert res["renamed_columns"] == []
    assert res["nodes"] == GRAPH["nodes"]


def test_join_keys_and_output_columns_are_renamed_but_aliases_are_not():
    payload = {
        "nodes": [
            {"id": "orders", "type": "source", "columns": [
                {"name": "uid_enc", "sensitive": True},
                {"name": "amount", "sensitive": False}]},
            {"id": "users", "type": "source", "columns": [
                {"name": "uid", "sensitive": False},
                {"name": "name", "sensitive": False}]},
            {"id": "j", "type": "join",
             "left_input": "orders", "right_input": "users",
             "left_key": "uid_enc", "right_key": "uid",
             "outputs": [
                 {"alias": "amt", "side": "left", "column": "amount"},
                 {"alias": "uname", "side": "right", "column": "name"}]},
        ],
        "export": "j",
        "source": "orders",
        "renames": [{"from": "uid_enc", "to": "uid_cipher"},
                    {"from": "amount", "to": "amt_value"}],
    }
    res = call_rename(payload)
    join = by_id(res["nodes"])["j"]
    assert join == {
        "id": "j",
        "type": "join",
        "left_input": "orders",
        "right_input": "users",
        "left_key": "uid_cipher",
        "right_key": "uid",
        "outputs": [
            {"alias": "amt", "side": "left", "column": "amt_value"},
            {"alias": "uname", "side": "right", "column": "name"},
        ],
    }
    # Output alias 'amt' is unchanged even though the source column is now
    # 'amt_value'; the join is the boundary where the rename stops.
    assert [c["name"] for c in res["review"]["columns"]] == ["amt", "uname"]
    assert res["review"]["columns"][0]["row_reasons"] == [
        {"node_id": "orders", "column": "uid_cipher"}
    ]


def test_same_named_column_of_another_source_is_not_renamed():
    payload = {
        "nodes": [
            {"id": "l", "type": "source", "columns": [
                {"name": "k", "sensitive": False},
                {"name": "v", "sensitive": True}]},
            {"id": "r", "type": "source", "columns": [
                {"name": "k", "sensitive": False},
                {"name": "v", "sensitive": False}]},
            {"id": "j", "type": "join",
             "left_input": "l", "right_input": "r",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "lv", "side": "left", "column": "v"},
                 {"alias": "rv", "side": "right", "column": "v"}]},
        ],
        "export": "j",
        "source": "l",
        "renames": [{"from": "k", "to": "key"}, {"from": "v", "to": "value"}],
    }
    res = call_rename(payload)
    nodes = by_id(res["nodes"])
    assert [c["name"] for c in nodes["r"]["columns"]] == ["k", "v"]
    assert nodes["j"] == {
        "id": "j",
        "type": "join",
        "left_input": "l",
        "right_input": "r",
        "left_key": "key",
        "right_key": "k",
        "outputs": [
            {"alias": "lv", "side": "left", "column": "value"},
            {"alias": "rv", "side": "right", "column": "v"},
        ],
    }
    cols = {c["name"]: c for c in res["review"]["columns"]}
    assert cols["lv"]["content_reasons"] == [
        {"node_id": "l", "column": "value"}
    ]
    # The right source's 'v' is a different column and keeps its (clean) tag.
    assert cols["rv"]["content_reasons"] == []
    assert cols["rv"]["blocked"] is False


def test_rename_reaches_every_branch_including_non_export_ones():
    # The export uses only the projection branch; the filter branch is dead
    # weight but must still be kept consistent.
    payload = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [
                {"name": "a", "sensitive": False},
                {"name": "b", "sensitive": False}]},
            {"id": "dead_filter", "type": "filter", "input": "s",
             "references": ["a", "b"]},
            {"id": "p", "type": "projection", "input": "s",
             "columns": [{"name": "oa", "copy": "a"}]},
        ],
        "export": "p",
        "source": "s",
        "renames": [{"from": "a", "to": "aa"}, {"from": "b", "to": "bb"}],
    }
    res = call_rename(payload)
    nodes = by_id(res["nodes"])
    assert nodes["dead_filter"]["references"] == ["aa", "bb"]
    assert nodes["p"]["columns"] == [{"name": "oa", "copy": "aa"}]
    assert len(res["nodes"]) == 3


def test_rename_stops_at_projection_outputs():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a", "sensitive": False}]},
            {"id": "p1", "type": "projection", "input": "s",
             "columns": [{"name": "x", "copy": "a"}]},
            {"id": "f", "type": "filter", "input": "p1",
             "references": ["x"]},
            {"id": "p2", "type": "projection", "input": "f",
             "columns": [{"name": "y", "copy": "x"}]},
        ],
        "export": "p2",
        "source": "s",
        "renames": [{"from": "a", "to": "z"}],
    }
    res = call_rename(payload)
    nodes = by_id(res["nodes"])
    assert nodes["s"]["columns"] == [{"name": "z", "sensitive": False}]
    assert nodes["p1"]["columns"] == [{"name": "x", "copy": "z"}]
    # Everything downstream of p1 speaks p1's output names.
    assert nodes["f"]["references"] == ["x"]
    assert nodes["p2"]["columns"] == [{"name": "y", "copy": "x"}]
    assert res["review"]["columns"][0]["name"] == "y"


def test_renamed_graph_round_trips_through_review_identically():
    payload = {**GRAPH, "source": "users",
               "renames": [{"from": "email", "to": "mail_addr"},
                           {"from": "country", "to": "region"}]}
    res = call_rename(payload)
    re_reviewed = call_review(
        {"nodes": res["nodes"], "export": res["export"]}
    )
    assert re_reviewed == res["review"]


def test_renamed_graph_is_plain_json_and_re_submittable(tmp_path):
    payload = {**GRAPH, "source": "users",
               "renames": [{"from": "uid", "to": "user_id"}]}
    res = call_rename(payload)
    decoded = json.loads(json.dumps({"nodes": res["nodes"],
                                     "export": res["export"]}))
    reviewed = call_review(decoded)
    assert reviewed["blocked"] is True
    assert reviewed["export"] == "export_users"


def test_diamond_branch_propagates_consistently_into_a_join():
    payload = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [
                {"name": "k", "sensitive": False},
                {"name": "v", "sensitive": True}]},
            {"id": "f", "type": "filter", "input": "s",
             "references": ["k"]},
            {"id": "j", "type": "join",
             "left_input": "s", "right_input": "f",
             "left_key": "k", "right_key": "k",
             "outputs": [
                 {"alias": "vk", "side": "left", "column": "v"},
                 {"alias": "fk", "side": "right", "column": "k"}]},
        ],
        "export": "j",
        "source": "s",
        "renames": [{"from": "k", "to": "key"}, {"from": "v", "to": "val"}],
    }
    res = call_rename(payload)
    join = by_id(res["nodes"])["j"]
    assert join["left_key"] == "key"
    assert join["right_key"] == "key"
    assert join["outputs"] == [
        {"alias": "vk", "side": "left", "column": "val"},
        {"alias": "fk", "side": "right", "column": "key"},
    ]


# ----------------------------- rejections ---------------------------------


def expect_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        call_rename(payload)
    assert exc.value.code == code
    return exc.value


def test_unknown_source_node_rejected():
    exc = expect_error(
        {**GRAPH, "source": "ghost", "renames": [{"from": "uid", "to": "x"}]},
        "RENAME_SOURCE_NOT_FOUND",
    )
    assert exc.node_id == "ghost"


def test_source_id_pointing_at_non_source_rejected():
    expect_error(
        {**GRAPH, "source": "only_cn",
         "renames": [{"from": "uid", "to": "x"}]},
        "RENAME_SOURCE_NOT_FOUND",
    )


def test_missing_original_column_rejected_without_partial_graph():
    exc = expect_error(
        {**GRAPH, "source": "users",
         "renames": [{"from": "ghost", "to": "x"}]},
        "RENAME_COLUMN_NOT_FOUND",
    )
    assert exc.detail()["columns"] == ["ghost"]
    assert "nodes" not in exc.detail()


def test_two_columns_renamed_to_same_new_name_rejected():
    exc = expect_error(
        {
            "nodes": [{"id": "s", "type": "source", "columns": [
                {"name": "a"}, {"name": "b"}]}],
            "export": "s",
            "source": "s",
            "renames": [{"from": "a", "to": "x"},
                        {"from": "b", "to": "x"}],
        },
        "RENAME_CONFLICT",
    )
    assert exc.detail()["columns"] == ["x"]


def test_new_name_colliding_with_untouched_column_rejected():
    expect_error(
        {
            "nodes": [{"id": "s", "type": "source", "columns": [
                {"name": "a"}, {"name": "b"}]}],
            "export": "s",
            "source": "s",
            "renames": [{"from": "a", "to": "b"}],
        },
        "RENAME_CONFLICT",
    )


def test_swap_is_not_reported_as_a_conflict():
    # Mirrors the happy-path swap but from a single-element rename list per
    # side is impossible; the point is no RENAME_CONFLICT is raised.
    payload = {
        "nodes": [{"id": "s", "type": "source",
                   "columns": [{"name": "a"}, {"name": "b"}]}],
        "export": "s",
        "source": "s",
        "renames": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    res = call_rename(payload)
    assert [c["name"] for c in res["nodes"][0]["columns"]] == ["b", "a"]


def test_invalid_original_graph_is_rejected_first():
    bad = {
        "nodes": [
            {"id": "a", "type": "projection", "input": "b",
             "columns": [{"name": "x", "constant": 1}]},
            {"id": "b", "type": "projection", "input": "a",
             "columns": [{"name": "x", "constant": 1}]},
        ],
        "export": "a",
        "source": "a",
        "renames": [{"from": "x", "to": "y"}],
    }
    expect_error(bad, "CYCLE")

    expect_error(
        {**GRAPH, "source": "users",
         "renames": [{"from": "uid", "to": "user_id"},
                     {"from": "ghost", "to": "g2"}]},
        "RENAME_COLUMN_NOT_FOUND",
    )


def test_unknown_column_reference_anywhere_still_rejects():
    # The whole graph is validated even where the dead branch is broken.
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source",
                 "columns": [{"name": "a"}]},
                {"id": "f", "type": "filter", "input": "s",
                 "references": ["ghost"]},
            ],
            "export": "s",
            "source": "s",
            "renames": [{"from": "a", "to": "b"}],
        },
        "INVALID_REFERENCE",
    )


# ----------------------------- schema (422) -------------------------------


def test_empty_rename_list_rejected():
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {**GRAPH, "source": "users", "renames": []}
        )


def test_duplicate_old_names_rejected():
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {**GRAPH, "source": "users",
             "renames": [{"from": "uid", "to": "a"},
                         {"from": "uid", "to": "b"}]}
        )


def test_empty_names_and_unknown_fields_rejected():
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {**GRAPH, "source": "users",
             "renames": [{"from": "", "to": "a"}]}
        )
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {**GRAPH, "source": "users",
             "renames": [{"from": "uid", "to": ""}]}
        )
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {**GRAPH, "source": "users",
             "renames": [{"from": "uid", "new": "x"}]}
        )
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {**GRAPH, "source": "users",
             "renames": [{"from": "uid", "to": "x"}], "nope": 1}
        )


def test_missing_source_or_renames_rejected():
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {"nodes": GRAPH["nodes"], "export": "export_users",
             "renames": [{"from": "uid", "to": "x"}]}
        )
    with pytest.raises(PydanticValidationError):
        RenameRequest.model_validate(
            {"nodes": GRAPH["nodes"], "export": "export_users",
             "source": "users"}
        )


# ----------------------------- helper: review comparison ------------------


def _col(name, content=None, rows=None, blocked=None):
    content = content or []
    rows = rows or []
    return {
        "name": name,
        "blocked": blocked if blocked is not None else bool(content or rows),
        "content_reasons": content,
        "row_reasons": rows,
    }


def test_compare_helper_maps_reasons_back_under_source_rename():
    original = {
        "export": "f",
        "blocked": True,
        "columns": [
            _col("b", content=[{"node_id": "s", "column": "b"}]),
        ],
    }
    renamed = {
        "export": "f",
        "blocked": True,
        "columns": [
            _col("bb", content=[{"node_id": "s", "column": "bb"}]),
        ],
    }
    assert _compare_renamed_review(
        original, renamed, "s", {"b": "bb"}, {"bb": "b"}
    ) == {}


def test_compare_helper_leaves_other_source_names_untouched():
    original = {
        "export": "j",
        "blocked": True,
        "columns": [
            _col("v", content=[{"node_id": "other", "column": "v"}]),
        ],
    }
    renamed = {
        "export": "j",
        "blocked": True,
        "columns": [
            _col("v", content=[{"node_id": "other", "column": "v"}]),
        ],
    }
    # 's' is renamed but the reason is rooted at another source.
    assert _compare_renamed_review(
        original, renamed, "s", {}, {}
    ) == {}


def test_compare_helper_detects_verdict_and_reason_drift():
    original = {
        "export": "s",
        "blocked": True,
        "columns": [_col("b", content=[{"node_id": "s", "column": "b"}])],
    }
    renamed = {
        "export": "s",
        "blocked": False,
        "columns": [_col("bb", blocked=False)],
    }
    diffs = _compare_renamed_review(
        original, renamed, "s", {"b": "bb"}, {"bb": "b"}
    )
    assert diffs["blocked"] == {"original": True, "renamed": False}
    assert diffs["mismatched_columns"][0]["column"] == "b"


def test_compare_helper_detects_unexpected_export_name():
    original = {"export": "f", "blocked": False,
                "columns": [_col("a", blocked=False)]}
    renamed = {"export": "f", "blocked": False,
               "columns": [_col("wrong", blocked=False)]}
    diffs = _compare_renamed_review(
        original, renamed, "s", {"a": "aa"}, {"aa": "a"}
    )
    problem = diffs["mismatched_columns"][0]
    assert problem["expected_name"] == "aa"
    assert problem["actual_name"] == "wrong"


# ----------------------------- HTTP endpoint ------------------------------


client = TestClient(app)


def test_http_rename_success_and_follow_up_review():
    resp = client.post(
        "/api/rename",
        json={**GRAPH, "source": "users",
              "renames": [{"from": "email", "to": "mail_addr"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["export"] == "export_users"
    assert body["source"] == "users"
    assert body["review"]["blocked"] is True

    follow = client.post("/api/review",
                         json={"nodes": body["nodes"], "export": body["export"]})
    assert follow.status_code == 200
    assert follow.json() == body["review"]


def test_http_rename_invalid_source_returns_400_without_partial_graph():
    resp = client.post(
        "/api/rename",
        json={**GRAPH, "source": "ghost",
              "renames": [{"from": "uid", "to": "x"}]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "RENAME_SOURCE_NOT_FOUND"
    assert "nodes" not in body and "review" not in body


def test_http_rename_conflict_returns_400():
    resp = client.post(
        "/api/rename",
        json={
            "nodes": [{"id": "s", "type": "source",
                       "columns": [{"name": "a"}, {"name": "b"}]}],
            "export": "s",
            "source": "s",
            "renames": [{"from": "a", "to": "x"},
                        {"from": "b", "to": "x"}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "RENAME_CONFLICT"


def test_http_rename_schema_error_returns_422():
    resp = client.post(
        "/api/rename",
        json={**GRAPH, "source": "users", "renames": []},
    )
    assert resp.status_code == 422

    resp = client.post(
        "/api/rename",
        json={**GRAPH, "source": "users",
              "renames": [{"from": "uid", "to": "a"},
                          {"from": "uid", "to": "b"}]},
    )
    assert resp.status_code == 422


def test_http_other_endpoints_unchanged():
    resp = client.post("/api/review", json=GRAPH)
    assert resp.status_code == 200
    assert resp.json()["blocked"] is True
    assert "renamed_columns" not in resp.json()
