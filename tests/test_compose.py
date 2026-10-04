import json

import pytest
from fastapi.testclient import TestClient

from app.engine import ValidationError, compose, review
from app.main import app
from app.models import ComposeRequest, ReviewRequest
from pydantic import ValidationError as PydanticValidationError


def call_compose(payload):
    return compose(ComposeRequest.model_validate(payload))


def call_review(payload):
    return review(ReviewRequest.model_validate(payload))


def by_id(nodes):
    return {n["id"]: n for n in nodes}


# Downstream: source -> filter -> projection export, mirroring the README
# example. The `users` source (with its sensitive `email` column) is the
# splice point. Upstream: a sanitizing pipeline whose export exposes clean
# columns only (`mail` is a constant null there).
DOWNSTREAM = {
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

UPSTREAM = {
    "nodes": [
        {
            "id": "raw_users",
            "type": "source",
            "columns": [
                {"name": "uid", "sensitive": False},
                {"name": "email_hash", "sensitive": True},
                {"name": "country", "sensitive": False},
            ],
        },
        {
            "id": "safe_users",
            "type": "projection",
            "input": "raw_users",
            "columns": [
                {"name": "uid", "copy": "uid"},
                {"name": "mail", "constant": None},
                {"name": "country", "copy": "country"},
            ],
        },
    ],
    "export": "safe_users",
}

MAPPING = [
    {"column": "uid", "from": "uid"},
    {"column": "email", "from": "mail"},
    {"column": "country", "from": "country"},
]

PAYLOAD = {
    "downstream": DOWNSTREAM,
    "upstream": UPSTREAM,
    "source": "users",
    "mapping": MAPPING,
}


# ----------------------------- happy paths --------------------------------


def test_source_is_replaced_in_place_by_a_single_projection():
    res = call_compose(PAYLOAD)
    # Node order: downstream nodes in original order with the source
    # replaced in place, then the introduced upstream nodes appended.
    assert [n["id"] for n in res["nodes"]] == [
        "users",
        "only_cn",
        "export_users",
        "raw_users",
        "safe_users",
    ]
    nodes = by_id(res["nodes"])
    # One-level projection under the original source id, copying the mapped
    # upstream export columns in the source's original column order.
    assert nodes["users"] == {
        "id": "users",
        "type": "projection",
        "input": "safe_users",
        "columns": [
            {"name": "uid", "copy": "uid"},
            {"name": "email", "copy": "mail"},
            {"name": "country", "copy": "country"},
        ],
    }
    # Every other downstream node is untouched.
    assert nodes["only_cn"] == DOWNSTREAM["nodes"][1]
    assert nodes["export_users"] == DOWNSTREAM["nodes"][2]
    # Introduced upstream nodes keep their original definitions.
    assert nodes["raw_users"] == UPSTREAM["nodes"][0]
    assert nodes["safe_users"] == UPSTREAM["nodes"][1]

    assert res["export"] == "export_users"
    assert res["source"] == "users"
    assert res["id_map"] == [
        {"from": "raw_users", "to": "raw_users"},
        {"from": "safe_users", "to": "safe_users"},
    ]


def test_provenance_at_the_replacement_comes_from_upstream():
    res = call_compose(PAYLOAD)
    review_result = res["review"]
    # The replaced source's sensitive flag on `email` does not survive: the
    # column is now fed by the upstream constant `mail`, so nothing is
    # blocked any more.
    assert review_result["export"] == "export_users"
    assert review_result["blocked"] is False
    for col in review_result["columns"]:
        assert col["blocked"] is False
        assert col["content_reasons"] == []
        assert col["row_reasons"] == []
    # The original downstream graph on its own is still blocked.
    assert call_review(DOWNSTREAM)["blocked"] is True


def test_upstream_sensitive_content_flows_into_downstream_export():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": {
            "nodes": [
                {
                    "id": "sanitized",
                    "type": "source",
                    "columns": [
                        {"name": "uid", "sensitive": False},
                        {"name": "mail", "sensitive": True},
                        {"name": "country", "sensitive": False},
                    ],
                }
            ],
            "export": "sanitized",
        },
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    cols = {c["name"]: c for c in res["review"]["columns"]}
    assert res["review"]["blocked"] is True
    # `mail` copies the sensitive upstream column; the reason names the
    # upstream source in the composed graph.
    assert cols["mail"]["content_reasons"] == [
        {"node_id": "sanitized", "column": "mail"}
    ]
    assert cols["user_id"]["blocked"] is False
    assert cols["bucket"]["blocked"] is False


def test_upstream_row_influence_reaches_every_export_column():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": {
            "nodes": [
                {
                    "id": "raw",
                    "type": "source",
                    "columns": [
                        {"name": "uid", "sensitive": False},
                        {"name": "mail", "sensitive": False},
                        {"name": "country", "sensitive": False},
                        {"name": "id_card", "sensitive": True},
                    ],
                },
                {
                    "id": "filtered",
                    "type": "filter",
                    "input": "raw",
                    "references": ["id_card"],
                },
                {
                    "id": "safe",
                    "type": "projection",
                    "input": "filtered",
                    "columns": [
                        {"name": "uid", "copy": "uid"},
                        {"name": "mail", "copy": "mail"},
                        {"name": "country", "copy": "country"},
                    ],
                },
            ],
            "export": "safe",
        },
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    # Row-level influence from the upstream filter lands on every column of
    # the replacement projection and hence on every export column.
    assert res["review"]["blocked"] is True
    for col in res["review"]["columns"]:
        assert col["content_reasons"] == []
        assert col["row_reasons"] == [{"node_id": "raw", "column": "id_card"}]


def test_conflicting_upstream_ids_are_renamed_and_references_updated():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": {
            "nodes": [
                {
                    "id": "users",
                    "type": "source",
                    "columns": [
                        {"name": "uid", "sensitive": False},
                        {"name": "mail", "sensitive": True},
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
                    "id": "safe",
                    "type": "projection",
                    "input": "only_cn",
                    "columns": [
                        {"name": "uid", "copy": "uid"},
                        {"name": "mail", "copy": "mail"},
                        {"name": "country", "copy": "country"},
                    ],
                },
            ],
            "export": "safe",
        },
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    assert res["id_map"] == [
        {"from": "users", "to": "users__upstream"},
        {"from": "only_cn", "to": "only_cn__upstream"},
        {"from": "safe", "to": "safe"},
    ]
    nodes = by_id(res["nodes"])
    # All input references of introduced nodes follow the renames.
    assert nodes["only_cn__upstream"]["input"] == "users__upstream"
    assert nodes["safe"]["input"] == "only_cn__upstream"
    # The replacement projection still sits at the original source id.
    assert nodes["users"]["input"] == "safe"
    assert nodes["users"]["type"] == "projection"
    # Review reasons use the composed (renamed) ids.
    cols = {c["name"]: c for c in res["review"]["columns"]}
    assert cols["mail"]["content_reasons"] == [
        {"node_id": "users__upstream", "column": "mail"}
    ]


def test_renamed_id_avoids_collision_with_later_kept_upstream_id():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": {
            "nodes": [
                {
                    "id": "users",
                    "type": "source",
                    "columns": [
                        {"name": "uid", "sensitive": False},
                        {"name": "mail", "sensitive": False},
                        {"name": "country", "sensitive": False},
                    ],
                },
                {
                    "id": "users__upstream",
                    "type": "projection",
                    "input": "users",
                    "columns": [
                        {"name": "uid", "copy": "uid"},
                        {"name": "mail", "copy": "mail"},
                        {"name": "country", "copy": "country"},
                    ],
                },
            ],
            "export": "users__upstream",
        },
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    # The conflicting upstream source cannot take "users__upstream" because a
    # later upstream node already keeps that id.
    assert res["id_map"] == [
        {"from": "users", "to": "users__upstream_2"},
        {"from": "users__upstream", "to": "users__upstream"},
    ]
    nodes = by_id(res["nodes"])
    assert nodes["users__upstream"]["input"] == "users__upstream_2"
    assert nodes["users"]["input"] == "users__upstream"
    assert len({n["id"] for n in res["nodes"]}) == len(res["nodes"])


def test_multiple_source_columns_may_share_one_upstream_column():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": UPSTREAM,
        "source": "users",
        "mapping": [
            {"column": "uid", "from": "uid"},
            {"column": "email", "from": "uid"},
            {"column": "country", "from": "country"},
        ],
    }
    res = call_compose(payload)
    users = by_id(res["nodes"])["users"]
    assert users["columns"] == [
        {"name": "uid", "copy": "uid"},
        {"name": "email", "copy": "uid"},
        {"name": "country", "copy": "country"},
    ]
    assert res["review"]["blocked"] is False


def test_mapping_order_does_not_change_source_column_order():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": UPSTREAM,
        "source": "users",
        "mapping": [
            {"column": "country", "from": "country"},
            {"column": "email", "from": "mail"},
            {"column": "uid", "from": "uid"},
        ],
    }
    res = call_compose(payload)
    users = by_id(res["nodes"])["users"]
    assert [c["name"] for c in users["columns"]] == ["uid", "email", "country"]


def test_non_ancestor_upstream_nodes_are_not_introduced():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": {
            "nodes": [
                UPSTREAM["nodes"][0],
                UPSTREAM["nodes"][1],
                {
                    "id": "dead_branch",
                    "type": "filter",
                    "input": "raw_users",
                    "references": ["email_hash"],
                },
            ],
            "export": "safe_users",
        },
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    assert "dead_branch" not in by_id(res["nodes"])
    assert [m["from"] for m in res["id_map"]] == ["raw_users", "safe_users"]


def test_downstream_non_export_branches_keep_working():
    payload = {
        "downstream": {
            "nodes": [
                DOWNSTREAM["nodes"][0],
                DOWNSTREAM["nodes"][1],
                DOWNSTREAM["nodes"][2],
                {
                    "id": "audit",
                    "type": "projection",
                    "input": "users",
                    "columns": [{"name": "mail_copy", "copy": "email"}],
                },
            ],
            "export": "export_users",
        },
        "upstream": UPSTREAM,
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    nodes = by_id(res["nodes"])
    # The dead branch still resolves `email` on the replacement projection.
    assert nodes["audit"] == payload["downstream"]["nodes"][3]
    assert res["review"]["blocked"] is False
    # The composed graph as a whole is re-submittable.
    re_reviewed = call_review({"nodes": res["nodes"], "export": "audit"})
    assert re_reviewed["blocked"] is False


def test_filter_export_upstream_maps_passthrough_columns():
    payload = {
        "downstream": DOWNSTREAM,
        "upstream": {
            "nodes": [
                {
                    "id": "raw",
                    "type": "source",
                    "columns": [
                        {"name": "uid", "sensitive": False},
                        {"name": "mail", "sensitive": False},
                        {"name": "country", "sensitive": False},
                    ],
                },
                {"id": "f", "type": "filter", "input": "raw",
                 "references": ["country"]},
            ],
            "export": "f",
        },
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    assert by_id(res["nodes"])["users"]["input"] == "f"
    assert res["review"]["blocked"] is False


def test_export_may_be_the_replaced_source_itself():
    payload = {
        "downstream": {
            "nodes": [DOWNSTREAM["nodes"][0]],
            "export": "users",
        },
        "upstream": UPSTREAM,
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    assert res["export"] == "users"
    assert res["nodes"][0]["type"] == "projection"
    assert res["review"]["export"] == "users"
    assert res["review"]["blocked"] is False
    assert [c["name"] for c in res["review"]["columns"]] == [
        "uid",
        "email",
        "country",
    ]


def test_composed_graph_round_trips_through_review_identically():
    res = call_compose(PAYLOAD)
    re_reviewed = call_review({"nodes": res["nodes"], "export": res["export"]})
    assert re_reviewed == res["review"]


def test_composed_graph_is_plain_json_and_re_submittable():
    res = call_compose(PAYLOAD)
    decoded = json.loads(
        json.dumps({"nodes": res["nodes"], "export": res["export"]})
    )
    reviewed = call_review(decoded)
    assert reviewed == res["review"]


def test_input_graphs_are_not_modified():
    request = ComposeRequest.model_validate(PAYLOAD)
    before_down = request.downstream.model_dump(
        by_alias=True, exclude_unset=True
    )
    before_up = request.upstream.model_dump(by_alias=True, exclude_unset=True)
    compose(request)
    assert request.downstream.model_dump(by_alias=True, exclude_unset=True) == before_down
    assert request.upstream.model_dump(by_alias=True, exclude_unset=True) == before_up
    # The response shares no mutable state with the request models.
    res = compose(request)
    assert res["nodes"] is not request.downstream.nodes


def test_join_export_upstream_and_join_heavy_downstream():
    payload = {
        "downstream": {
            "nodes": [
                DOWNSTREAM["nodes"][0],
                {
                    "id": "tags",
                    "type": "source",
                    "columns": [
                        {"name": "uid", "sensitive": False},
                        {"name": "tag", "sensitive": False},
                    ],
                },
                {
                    "id": "j",
                    "type": "join",
                    "left_input": "users",
                    "right_input": "tags",
                    "left_key": "uid",
                    "right_key": "uid",
                    "outputs": [
                        {"alias": "mail", "side": "left", "column": "email"},
                        {"alias": "tag", "side": "right", "column": "tag"},
                    ],
                },
            ],
            "export": "j",
        },
        "upstream": UPSTREAM,
        "source": "users",
        "mapping": MAPPING,
    }
    res = call_compose(payload)
    nodes = by_id(res["nodes"])
    # The join's key and output references still resolve on the projection.
    assert nodes["j"]["left_input"] == "users"
    assert res["review"]["blocked"] is False
    assert [c["name"] for c in res["review"]["columns"]] == ["mail", "tag"]


# ----------------------------- rejections ---------------------------------


def expect_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        call_compose(payload)
    assert exc.value.code == code
    return exc.value


def test_unknown_source_rejected():
    exc = expect_error({**PAYLOAD, "source": "ghost"}, "COMPOSE_SOURCE_NOT_FOUND")
    assert exc.node_id == "ghost"


def test_non_source_target_rejected():
    expect_error({**PAYLOAD, "source": "only_cn"}, "COMPOSE_SOURCE_NOT_FOUND")
    expect_error({**PAYLOAD, "source": "export_users"}, "COMPOSE_SOURCE_NOT_FOUND")


def test_mapping_missing_a_source_column_rejected():
    exc = expect_error(
        {**PAYLOAD, "mapping": MAPPING[:2]},
        "COMPOSE_MAPPING_MISMATCH",
    )
    detail = exc.detail()
    assert detail["missing"] == ["country"]
    assert detail["extra"] == []
    assert "nodes" not in detail


def test_mapping_with_extra_entry_rejected():
    exc = expect_error(
        {**PAYLOAD, "mapping": MAPPING + [{"column": "ghost", "from": "uid"}]},
        "COMPOSE_MAPPING_MISMATCH",
    )
    detail = exc.detail()
    assert detail["missing"] == []
    assert detail["extra"] == ["ghost"]


def test_mapped_upstream_column_not_found_rejected():
    exc = expect_error(
        {
            **PAYLOAD,
            "mapping": [
                {"column": "uid", "from": "uid"},
                {"column": "email", "from": "ghost"},
                {"column": "country", "from": "ghost"},
            ],
        },
        "COMPOSE_COLUMN_NOT_FOUND",
    )
    assert exc.detail()["columns"] == ["ghost"]
    assert exc.node_id == "safe_users"


def test_invalid_downstream_graph_rejected_with_original_code():
    bad = {
        "downstream": {
            "nodes": [
                {"id": "a", "type": "projection", "input": "b",
                 "columns": [{"name": "x", "constant": 1}]},
                {"id": "b", "type": "projection", "input": "a",
                 "columns": [{"name": "x", "constant": 1}]},
            ],
            "export": "a",
        },
        "upstream": UPSTREAM,
        "source": "a",
        "mapping": [{"column": "x", "from": "uid"}],
    }
    expect_error(bad, "CYCLE")

    broken_ref = {
        **PAYLOAD,
        "downstream": {
            "nodes": DOWNSTREAM["nodes"]
            + [{"id": "dead", "type": "filter", "input": "users",
                "references": ["ghost"]}],
            "export": "export_users",
        },
    }
    expect_error(broken_ref, "INVALID_REFERENCE")


def test_invalid_upstream_graph_rejected_with_original_code():
    bad = {
        **PAYLOAD,
        "upstream": {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {"id": "f", "type": "filter", "input": "s",
                 "references": ["ghost"]},
            ],
            "export": "f",
        },
    }
    expect_error(bad, "INVALID_REFERENCE")

    dup = {
        **PAYLOAD,
        "upstream": {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {"id": "s", "type": "source", "columns": [{"name": "b"}]},
            ],
            "export": "s",
        },
    }
    expect_error(dup, "DUPLICATE_NODE")


def _chain_graph(prefix, source_count, length, export):
    """A source plus a chain of projections, ``length`` nodes in total."""
    nodes = [
        {
            "id": f"{prefix}0",
            "type": "source",
            "columns": [{"name": f"c{i}", "sensitive": False}
                        for i in range(source_count)],
        }
    ]
    for i in range(1, length):
        nodes.append(
            {
                "id": f"{prefix}{i}",
                "type": "projection",
                "input": f"{prefix}{i - 1}",
                "columns": [
                    {"name": f"c{j}", "copy": f"c{j}"}
                    for j in range(source_count)
                ],
            }
        )
    return {"nodes": nodes, "export": export}


def test_composed_graph_over_60_nodes_rejected_without_partial_graph():
    downstream = _chain_graph("d", 2, 55, "d54")
    upstream = _chain_graph("u", 2, 6, "u5")
    payload = {
        "downstream": downstream,
        "upstream": upstream,
        "source": "d0",
        "mapping": [
            {"column": "c0", "from": "c0"},
            {"column": "c1", "from": "c1"},
        ],
    }
    exc = expect_error(payload, "COMPOSE_NODE_LIMIT")
    detail = exc.detail()
    assert detail["node_count"] == 61
    assert detail["limit"] == 60
    assert "nodes" not in detail


def test_composed_graph_at_exactly_60_nodes_succeeds():
    downstream = _chain_graph("d", 2, 55, "d54")
    upstream = _chain_graph("u", 2, 5, "u4")
    payload = {
        "downstream": downstream,
        "upstream": upstream,
        "source": "d0",
        "mapping": [
            {"column": "c0", "from": "c0"},
            {"column": "c1", "from": "c1"},
        ],
    }
    res = call_compose(payload)
    assert len(res["nodes"]) == 60
    assert res["review"]["blocked"] is False


# ----------------------------- schema (422) -------------------------------


def test_empty_mapping_rejected():
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate({**PAYLOAD, "mapping": []})


def test_duplicate_mapping_column_rejected():
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate(
            {
                **PAYLOAD,
                "mapping": MAPPING + [{"column": "uid", "from": "country"}],
            }
        )


def test_empty_names_and_unknown_fields_rejected():
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate(
            {**PAYLOAD, "mapping": [{"column": "", "from": "uid"}]}
        )
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate(
            {**PAYLOAD, "mapping": [{"column": "uid", "from": ""}]}
        )
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate({**PAYLOAD, "source": ""})
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate({**PAYLOAD, "nope": 1})
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate(
            {**PAYLOAD, "mapping": [{"column": "uid", "to": "uid"}]}
        )


def test_missing_sections_rejected():
    for field in ("downstream", "upstream", "source", "mapping"):
        payload = {k: v for k, v in PAYLOAD.items() if k != field}
        with pytest.raises(PydanticValidationError):
            ComposeRequest.model_validate(payload)


def test_subgraph_schema_limits_still_apply():
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate(
            {**PAYLOAD, "downstream": {"nodes": [], "export": "x"}}
        )
    too_many = _chain_graph("u", 1, 61, "u60")
    with pytest.raises(PydanticValidationError):
        ComposeRequest.model_validate({**PAYLOAD, "upstream": too_many})


# ----------------------------- HTTP endpoint ------------------------------


client = TestClient(app)


def test_http_compose_success_and_follow_up_review():
    resp = client.post("/api/compose", json=PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["export"] == "export_users"
    assert body["source"] == "users"
    assert body["review"]["blocked"] is False
    assert body["id_map"] == [
        {"from": "raw_users", "to": "raw_users"},
        {"from": "safe_users", "to": "safe_users"},
    ]

    follow = client.post(
        "/api/review", json={"nodes": body["nodes"], "export": body["export"]}
    )
    assert follow.status_code == 200
    assert follow.json() == body["review"]


def test_http_compose_from_example_file():
    with open("examples/compose_request.json") as fh:
        payload = json.load(fh)
    resp = client.post("/api/compose", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["review"]["blocked"] is False
    follow = client.post(
        "/api/review", json={"nodes": body["nodes"], "export": body["export"]}
    )
    assert follow.json() == body["review"]


def test_http_compose_mapping_mismatch_returns_400_without_partial_graph():
    resp = client.post(
        "/api/compose", json={**PAYLOAD, "mapping": MAPPING[:2]}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "COMPOSE_MAPPING_MISMATCH"
    assert body["error"]["missing"] == ["country"]
    assert "nodes" not in body and "review" not in body


def test_http_compose_unknown_upstream_column_returns_400():
    resp = client.post(
        "/api/compose",
        json={
            **PAYLOAD,
            "mapping": [
                {"column": "uid", "from": "uid"},
                {"column": "email", "from": "ghost"},
                {"column": "country", "from": "country"},
            ],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "COMPOSE_COLUMN_NOT_FOUND"


def test_http_compose_non_source_target_returns_400():
    resp = client.post("/api/compose", json={**PAYLOAD, "source": "only_cn"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "COMPOSE_SOURCE_NOT_FOUND"


def test_http_compose_schema_error_returns_422():
    resp = client.post("/api/compose", json={**PAYLOAD, "mapping": []})
    assert resp.status_code == 422

    resp = client.post(
        "/api/compose",
        json={**PAYLOAD, "mapping": MAPPING + [{"column": "uid", "from": "x"}]},
    )
    assert resp.status_code == 422


def test_http_other_endpoints_unchanged():
    resp = client.post("/api/review", json=DOWNSTREAM)
    assert resp.status_code == 200
    assert resp.json()["blocked"] is True
    assert "id_map" not in resp.json()
