import json

import pytest
from fastapi.testclient import TestClient

from app.engine import ValidationError, review, revise
from app.main import app
from app.models import ReviewRequest, ReviseRequest
from pydantic import ValidationError as PydanticValidationError


def call_revise(payload):
    return revise(ReviseRequest.model_validate(payload))


def call_review(payload):
    return review(ReviewRequest.model_validate(payload))


# Graph used across the content-only tests: one sensitive column blocked by
# content provenance, one clean copy, one existing constant column.
CONTENT_GRAPH = {
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


def test_safe_graph_returned_unchanged():
    payload = {
        "nodes": [
            {
                "id": "s",
                "type": "source",
                "columns": [{"name": "a", "sensitive": False}],
            },
            {
                "id": "p",
                "type": "projection",
                "input": "s",
                "columns": [{"name": "a", "copy": "a"}],
            },
        ],
        "export": "p",
        "allowed_replacements": ["a"],
    }
    res = call_revise(payload)
    assert res["revised"] is False
    assert res["replaced_columns"] == []
    assert res["export"] == "p"
    # No new node: the node set is byte-for-byte the original request nodes.
    assert res["nodes"] == payload["nodes"]
    assert len(res["nodes"]) == 2
    assert res["review"]["blocked"] is False


def test_safe_graph_accepts_empty_allow_list_and_returns_unchanged():
    payload = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [{"name": "a"}]}
        ],
        "export": "s",
        "allowed_replacements": [],
    }
    res = call_revise(payload)
    assert res["revised"] is False
    assert res["replaced_columns"] == []
    assert res["nodes"] == payload["nodes"]


def test_blocked_content_column_is_nulled_by_appended_projection():
    payload = {**CONTENT_GRAPH, "allowed_replacements": ["mail"]}
    res = call_revise(payload)
    assert res["revised"] is True
    assert res["replaced_columns"] == ["mail"]
    assert res["export"] == "export_users__safe_export"

    # Exactly one node appended; originals untouched.
    assert len(res["nodes"]) == len(CONTENT_GRAPH["nodes"]) + 1
    assert res["nodes"][:-1] == CONTENT_GRAPH["nodes"]

    projection = res["nodes"][-1]
    assert projection == {
        "id": "export_users__safe_export",
        "type": "projection",
        "input": "export_users",
        "columns": [
            {"name": "user_id", "copy": "user_id"},
            {"name": "mail", "constant": None},
            {"name": "bucket", "copy": "bucket"},
        ],
    }

    review_result = res["review"]
    assert review_result["export"] == "export_users__safe_export"
    assert review_result["blocked"] is False
    cols = {c["name"]: c for c in review_result["columns"]}
    assert cols["user_id"]["blocked"] is False
    assert cols["mail"]["blocked"] is False
    assert cols["mail"]["content_reasons"] == []
    assert cols["mail"]["row_reasons"] == []
    assert cols["bucket"]["blocked"] is False


def test_revised_graph_round_trips_through_review_as_safe():
    res = call_revise({**CONTENT_GRAPH, "allowed_replacements": ["mail"]})
    re_reviewed = call_review({"nodes": res["nodes"], "export": res["export"]})
    assert re_reviewed == res["review"]
    assert re_reviewed["blocked"] is False


def test_permitted_but_clean_column_is_copied_not_replaced():
    # 'user_id' is on the allow list but has no sensitive provenance, so it
    # must be copied and never reported as actually replaced.
    res = call_revise(
        {**CONTENT_GRAPH, "allowed_replacements": ["mail", "user_id"]}
    )
    assert res["replaced_columns"] == ["mail"]
    projection = res["nodes"][-1]
    assert projection["columns"][0] == {"name": "user_id", "copy": "user_id"}
    assert projection["columns"][1] == {"name": "mail", "constant": None}


def test_multiple_sensitive_columns_nulled_preserving_order():
    payload = {
        "nodes": [
            {
                "id": "s",
                "type": "source",
                "columns": [
                    {"name": "a", "sensitive": True},
                    {"name": "b"},
                    {"name": "c", "sensitive": True},
                ],
            },
        ],
        "export": "s",
        "allowed_replacements": ["a", "c"],
    }
    res = call_revise(payload)
    assert res["replaced_columns"] == ["a", "c"]
    assert res["nodes"][-1]["columns"] == [
        {"name": "a", "constant": None},
        {"name": "b", "copy": "b"},
        {"name": "c", "constant": None},
    ]


def test_projection_id_avoids_collision_with_existing_node():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "secret", "sensitive": True}]},
            {
                "id": "s__safe_export",
                "type": "projection",
                "input": "s",
                "columns": [{"name": "secret", "copy": "secret"}],
            },
        ],
        "export": "s__safe_export",
        "allowed_replacements": ["secret"],
    }
    res = call_revise(payload)
    assert res["export"] == "s__safe_export__safe_export"
    assert {n["id"] for n in res["nodes"]} == {
        "s",
        "s__safe_export",
        "s__safe_export__safe_export",
    }
    assert res["review"]["blocked"] is False


def test_row_set_and_schema_preserved_for_join_export():
    payload = {
        "nodes": [
            {"id": "l", "type": "source",
             "columns": [{"name": "k"}, {"name": "secret", "sensitive": True}]},
            {"id": "r", "type": "source",
             "columns": [{"name": "k"}, {"name": "v"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "l",
                "right_input": "r",
                "left_key": "k",
                "right_key": "k",
                "outputs": [
                    {"alias": "secret", "side": "left", "column": "secret"},
                    {"alias": "v", "side": "right", "column": "v"},
                ],
            },
        ],
        "export": "j",
        "allowed_replacements": ["secret"],
    }
    res = call_revise(payload)
    assert res["replaced_columns"] == ["secret"]
    assert res["nodes"][-1]["input"] == "j"
    assert [c["name"] for c in res["nodes"][-1]["columns"]] == ["secret", "v"]
    assert res["review"]["blocked"] is False


def test_original_constant_null_column_survives_serialization():
    # An unset constant must never appear alongside a copy after round-tripping.
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "secret", "sensitive": True},
                         {"name": "plain"}]},
            {
                "id": "p",
                "type": "projection",
                "input": "s",
                "columns": [
                    {"name": "secret", "copy": "secret"},
                    {"name": "lit", "constant": None},
                    {"name": "plain", "copy": "plain"},
                ],
            },
        ],
        "export": "p",
        "allowed_replacements": ["secret"],
    }
    res = call_revise(payload)
    assert res["nodes"][1]["columns"] == [
        {"name": "secret", "copy": "secret"},
        {"name": "lit", "constant": None},
        {"name": "plain", "copy": "plain"},
    ]


# ----------------------------- not revisable ------------------------------


def expect_engine_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        call_revise(payload)
    assert exc.value.code == code
    return exc.value


def test_row_influence_makes_graph_not_revisable():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "id_card", "sensitive": True},
                         {"name": "score"}]},
            {"id": "f", "type": "filter", "input": "s",
             "references": ["id_card"]},
            {
                "id": "e",
                "type": "projection",
                "input": "f",
                "columns": [
                    {"name": "score", "copy": "score"},
                    {"name": "lit", "constant": 1},
                ],
            },
        ],
        "export": "e",
        # Offering every blocked column still cannot fix row-level influence.
        "allowed_replacements": ["score", "lit"],
    }
    exc = expect_engine_error(payload, "UNREVISABLE_ROW_INFLUENCE")
    assert exc.node_id == "e"
    assert exc.detail()["row_reasons"] == [{"node_id": "s", "column": "id_card"}]


def test_row_influence_from_sensitive_join_key_is_not_revisable():
    payload = {
        "nodes": [
            {"id": "orders", "type": "source",
             "columns": [{"name": "uid_enc", "sensitive": True},
                         {"name": "amount"}]},
            {"id": "users", "type": "source",
             "columns": [{"name": "uid"}, {"name": "name"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "orders",
                "right_input": "users",
                "left_key": "uid_enc",
                "right_key": "uid",
                "outputs": [
                    {"alias": "amt", "side": "left", "column": "amount"},
                    {"alias": "uname", "side": "right", "column": "name"},
                ],
            },
        ],
        "export": "j",
        "allowed_replacements": ["amt", "uname"],
    }
    expect_engine_error(payload, "UNREVISABLE_ROW_INFLUENCE")


def test_blocked_column_outside_allow_list_is_not_revisable():
    exc = expect_engine_error(
        {**CONTENT_GRAPH, "allowed_replacements": []},
        "UNREVISABLE_COLUMN_NOT_ALLOWED",
    )
    assert exc.detail()["columns"] == ["mail"]


def test_only_one_of_several_blocked_columns_allowed_is_not_revisable():
    payload = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "a", "sensitive": True},
                         {"name": "b", "sensitive": True}]},
        ],
        "export": "s",
        "allowed_replacements": ["a"],
    }
    exc = expect_engine_error(payload, "UNREVISABLE_COLUMN_NOT_ALLOWED")
    assert exc.detail()["columns"] == ["b"]


def test_appending_at_node_limit_is_not_revisable():
    nodes = [{"id": "s0", "type": "source",
              "columns": [{"name": "a", "sensitive": True}]}]
    for i in range(1, 60):
        nodes.append(
            {
                "id": f"n{i}",
                "type": "projection",
                "input": f"n{i - 1}" if i > 1 else "s0",
                "columns": [{"name": "a", "copy": "a"}],
            }
        )
    payload = {
        "nodes": nodes,
        "export": "n59",
        "allowed_replacements": ["a"],
    }
    exc = expect_engine_error(payload, "UNREVISABLE_NODE_LIMIT")
    assert exc.detail() == {
        "code": "UNREVISABLE_NODE_LIMIT",
        "message": exc.detail()["message"],
        "node_id": "n59",
        "node_count": 60,
        "limit": 60,
    }


def test_59_nodes_can_still_be_revised():
    nodes = [{"id": "s0", "type": "source",
              "columns": [{"name": "a", "sensitive": True}]}]
    for i in range(1, 59):
        nodes.append(
            {
                "id": f"n{i}",
                "type": "projection",
                "input": f"n{i - 1}" if i > 1 else "s0",
                "columns": [{"name": "a", "copy": "a"}],
            }
        )
    res = call_revise(
        {"nodes": nodes, "export": "n58", "allowed_replacements": ["a"]}
    )
    assert len(res["nodes"]) == 60
    assert res["review"]["blocked"] is False


def test_unknown_allowed_column_rejected_even_when_graph_is_safe():
    payload = {
        "nodes": [{"id": "s", "type": "source", "columns": [{"name": "a"}]}],
        "export": "s",
        "allowed_replacements": ["ghost"],
    }
    exc = expect_engine_error(payload, "REPLACEMENT_COLUMN_NOT_FOUND")
    assert exc.detail()["columns"] == ["ghost"]


# ----------------------------- request validation -------------------------


def test_duplicate_allowed_column_names_rejected():
    with pytest.raises(PydanticValidationError):
        ReviseRequest.model_validate(
            {
                "nodes": [{"id": "s", "type": "source",
                           "columns": [{"name": "a"}]}],
                "export": "s",
                "allowed_replacements": ["a", "a"],
            }
        )


def test_missing_replacements_field_defaults_to_empty_list():
    # No allow list means no column may be replaced: safe graphs pass through.
    req = ReviseRequest.model_validate(
        {"nodes": [{"id": "s", "type": "source",
                    "columns": [{"name": "a"}]}], "export": "s"}
    )
    assert req.allowed_replacements == []
    res = revise(req)
    assert res["revised"] is False


def test_unknown_field_rejected():
    with pytest.raises(PydanticValidationError):
        ReviseRequest.model_validate(
            {
                "nodes": [{"id": "s", "type": "source",
                           "columns": [{"name": "a"}]}],
                "export": "s",
                "allowed_replacements": [],
                "nope": 1,
            }
        )


def test_original_graph_validation_errors_still_apply_to_revise():
    # Cycle in the supplied graph must be rejected by the original rules.
    expect_engine_error(
        {
            "nodes": [
                {"id": "a", "type": "projection", "input": "b",
                 "columns": [{"name": "x", "constant": 1}]},
                {"id": "b", "type": "projection", "input": "a",
                 "columns": [{"name": "x", "constant": 1}]},
            ],
            "export": "a",
            "allowed_replacements": ["x"],
        },
        "CYCLE",
    )
    expect_engine_error(
        {
            "nodes": [{"id": "s", "type": "source",
                       "columns": [{"name": "a"}]}],
            "export": "missing",
            "allowed_replacements": [],
        },
        "INVALID_REFERENCE",
    )


# ----------------------------- HTTP endpoint ------------------------------


client = TestClient(app)


def test_http_revise_success_round_trips_to_review():
    resp = client.post("/api/revise", json={**CONTENT_GRAPH,
                                            "allowed_replacements": ["mail"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["replaced_columns"] == ["mail"]
    assert body["review"]["blocked"] is False

    follow = client.post("/api/review",
                         json={"nodes": body["nodes"], "export": body["export"]})
    assert follow.status_code == 200
    assert follow.json()["blocked"] is False


def test_http_revise_safe_graph_passes_through():
    resp = client.post(
        "/api/revise",
        json={
            "nodes": [{"id": "s", "type": "source",
                       "columns": [{"name": "a"}]}],
            "export": "s",
            "allowed_replacements": [],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revised"] is False
    assert body["replaced_columns"] == []
    assert len(body["nodes"]) == 1


def test_http_revise_row_influence_returns_400_without_partial_graph():
    resp = client.post(
        "/api/revise",
        json={
            "nodes": [
                {"id": "s", "type": "source",
                 "columns": [{"name": "id_card", "sensitive": True}]},
                {"id": "f", "type": "filter", "input": "s",
                 "references": ["id_card"]},
            ],
            "export": "f",
            "allowed_replacements": ["id_card"],
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "UNREVISABLE_ROW_INFLUENCE"
    assert "nodes" not in body and "review" not in body


def test_http_revise_blocked_column_not_allowed_returns_400():
    resp = client.post(
        "/api/revise",
        json={**CONTENT_GRAPH, "allowed_replacements": []},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNREVISABLE_COLUMN_NOT_ALLOWED"


def test_http_revise_node_limit_returns_400():
    nodes = [{"id": "s0", "type": "source",
              "columns": [{"name": "a", "sensitive": True}]}]
    for i in range(1, 60):
        nodes.append({"id": f"n{i}", "type": "projection",
                      "input": f"n{i - 1}" if i > 1 else "s0",
                      "columns": [{"name": "a", "copy": "a"}]})
    resp = client.post("/api/revise",
                       json={"nodes": nodes, "export": "n59",
                             "allowed_replacements": ["a"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNREVISABLE_NODE_LIMIT"


def test_http_revise_schema_error_returns_422():
    resp = client.post(
        "/api/revise",
        json={
            "nodes": [{"id": "s", "type": "source",
                       "columns": [{"name": "a"}]}],
            "export": "s",
            "allowed_replacements": ["a", "a"],
        },
    )
    assert resp.status_code == 422


def test_http_review_endpoint_behavior_unchanged():
    resp = client.post("/api/review", json=CONTENT_GRAPH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["export"] == "export_users"
    assert body["blocked"] is True
    assert "replaced_columns" not in body
    cols = {c["name"]: c for c in body["columns"]}
    assert cols["mail"]["blocked"] is True
    assert cols["mail"]["content_reasons"] == [
        {"node_id": "users", "column": "email"}
    ]


def test_revised_graph_is_actual_json_submittable(tmp_path):
    # The response must be plain JSON data (no pydantic models) and re-submittable.
    res = call_revise({**CONTENT_GRAPH, "allowed_replacements": ["mail"]})
    encoded = json.dumps({"nodes": res["nodes"], "export": res["export"]})
    decoded = json.loads(encoded)
    assert call_review(decoded)["blocked"] is False
