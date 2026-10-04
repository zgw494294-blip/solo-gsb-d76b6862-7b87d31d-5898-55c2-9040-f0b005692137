import pytest

from app.engine import ValidationError, review
from app.models import ReviewRequest
from pydantic import ValidationError as PydanticValidationError


def call(payload):
    return review(ReviewRequest.model_validate(payload))


# ----------------------------- happy paths --------------------------------


def test_source_columns_are_classified():
    res = call(
        {
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
        }
    )
    assert res["blocked"] is True
    by_name = {c["name"]: c for c in res["columns"]}
    assert by_name["a"]["blocked"] is False
    assert by_name["b"]["content_reasons"] == [{"node_id": "s", "column": "b"}]
    assert by_name["b"]["row_reasons"] == []


def test_copy_propagates_and_constant_clears_content():
    res = call(
        {
            "nodes": [
                {
                    "id": "s",
                    "type": "source",
                    "columns": [
                        {"name": "secret", "sensitive": True},
                        {"name": "plain", "sensitive": False},
                    ],
                },
                {
                    "id": "p",
                    "type": "projection",
                    "input": "s",
                    "columns": [
                        {"name": "secret_copy", "copy": "secret"},
                        {"name": "secret_wiped", "constant": "x"},
                        {"name": "plain_copy", "copy": "plain"},
                        {"name": "null_col", "constant": None},
                    ],
                },
            ],
            "export": "p",
        }
    )
    cols = {c["name"]: c for c in res["columns"]}
    assert cols["secret_copy"]["blocked"] is True
    assert cols["secret_wiped"]["content_reasons"] == []
    assert cols["secret_wiped"]["blocked"] is False
    assert cols["plain_copy"]["blocked"] is False
    assert cols["null_col"]["content_reasons"] == []


def test_filter_on_sensitive_column_taints_every_row():
    res = call(
        {
            "nodes": [
                {
                    "id": "s",
                    "type": "source",
                    "columns": [
                        {"name": "id_card", "sensitive": True},
                        {"name": "score", "sensitive": False},
                    ],
                },
                {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
                {
                    "id": "p",
                    "type": "projection",
                    "input": "f",
                    "columns": [
                        {"name": "score", "copy": "score"},
                        {"name": "lit", "constant": 1},
                    ],
                },
            ],
            "export": "p",
        }
    )
    cols = {c["name"]: c for c in res["columns"]}
    # Even the constant column cannot escape the row-level taint.
    for name in ("score", "lit"):
        assert cols[name]["blocked"] is True
        assert cols[name]["content_reasons"] == []
        assert cols[name]["row_reasons"] == [
            {"node_id": "s", "column": "id_card"}
        ]


def test_filter_on_non_sensitive_column_is_clean():
    res = call(
        {
            "nodes": [
                {
                    "id": "s",
                    "type": "source",
                    "columns": [
                        {"name": "region", "sensitive": False},
                        {"name": "ssn", "sensitive": True},
                    ],
                },
                {
                    "id": "f",
                    "type": "filter",
                    "input": "s",
                    "references": ["region"],
                },
                {
                    "id": "p",
                    "type": "projection",
                    "input": "f",
                    "columns": [{"name": "ssn", "copy": "ssn"}],
                },
            ],
            "export": "p",
        }
    )
    col = res["columns"][0]
    assert col["content_reasons"] == [{"node_id": "s", "column": "ssn"}]
    assert col["row_reasons"] == []
    assert col["blocked"] is True


def test_join_merges_row_influence_and_key_taint():
    res = call(
        {
            "nodes": [
                {
                    "id": "orders",
                    "type": "source",
                    "columns": [
                        {"name": "uid_enc", "sensitive": True},
                        {"name": "amount", "sensitive": False},
                    ],
                },
                {
                    "id": "users",
                    "type": "source",
                    "columns": [
                        {"name": "uid", "sensitive": False},
                        {"name": "name", "sensitive": False},
                    ],
                },
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
        }
    )
    # Sensitive left key taints all rows, even though the output columns are
    # themselves non-sensitive.
    for col in res["columns"]:
        assert col["blocked"] is True
        assert col["content_reasons"] == []
        assert col["row_reasons"] == [
            {"node_id": "orders", "column": "uid_enc"}
        ]


def test_join_content_provenance_per_side():
    res = call(
        {
            "nodes": [
                {
                    "id": "l",
                    "type": "source",
                    "columns": [
                        {"name": "k", "sensitive": False},
                        {"name": "secret", "sensitive": True},
                    ],
                },
                {
                    "id": "r",
                    "type": "source",
                    "columns": [
                        {"name": "k", "sensitive": False},
                        {"name": "label", "sensitive": False},
                    ],
                },
                {
                    "id": "j",
                    "type": "join",
                    "left_input": "l",
                    "right_input": "r",
                    "left_key": "k",
                    "right_key": "k",
                    "outputs": [
                        {"alias": "a_secret", "side": "left", "column": "secret"},
                        {"alias": "a_label", "side": "right", "column": "label"},
                    ],
                },
            ],
            "export": "j",
        }
    )
    cols = {c["name"]: c for c in res["columns"]}
    assert cols["a_secret"]["content_reasons"] == [
        {"node_id": "l", "column": "secret"}
    ]
    assert cols["a_secret"]["blocked"] is True
    assert cols["a_label"]["blocked"] is False


def test_row_taint_survives_downstream_join_and_projection():
    # left branch is row-tainted by a filter; right branch is clean.
    res = call(
        {
            "nodes": [
                {
                    "id": "s1",
                    "type": "source",
                    "columns": [
                        {"name": "k", "sensitive": False},
                        {"name": "token", "sensitive": True},
                        {"name": "v", "sensitive": False},
                    ],
                },
                {"id": "f", "type": "filter", "input": "s1", "references": ["token"]},
                {
                    "id": "s2",
                    "type": "source",
                    "columns": [
                        {"name": "k", "sensitive": False},
                        {"name": "info", "sensitive": False},
                    ],
                },
                {
                    "id": "j",
                    "type": "join",
                    "left_input": "f",
                    "right_input": "s2",
                    "left_key": "k",
                    "right_key": "k",
                    "outputs": [
                        {"alias": "v", "side": "left", "column": "v"},
                        {"alias": "info", "side": "right", "column": "info"},
                    ],
                },
                {
                    "id": "p",
                    "type": "projection",
                    "input": "j",
                    "columns": [
                        {"name": "info", "copy": "info"},
                        {"name": "fixed", "constant": "Z"},
                    ],
                },
            ],
            "export": "p",
        }
    )
    for col in res["columns"]:
        assert col["blocked"] is True
        assert col["row_reasons"] == [{"node_id": "s1", "column": "token"}]


def test_nodes_may_be_out_of_order():
    res = call(
        {
            "nodes": [
                {
                    "id": "p",
                    "type": "projection",
                    "input": "s",
                    "columns": [{"name": "a", "copy": "a"}],
                },
                {
                    "id": "s",
                    "type": "source",
                    "columns": [{"name": "a", "sensitive": False}],
                },
            ],
            "export": "p",
        }
    )
    assert res["blocked"] is False


def test_multiple_origins_are_aggregated_and_sorted():
    res = call(
        {
            "nodes": [
                {
                    "id": "b",
                    "type": "source",
                    "columns": [
                        {"name": "k", "sensitive": False},
                        {"name": "bz", "sensitive": True},
                    ],
                },
                {
                    "id": "a",
                    "type": "source",
                    "columns": [
                        {"name": "k", "sensitive": False},
                        {"name": "az", "sensitive": True},
                    ],
                },
                {
                    "id": "j",
                    "type": "join",
                    "left_input": "a",
                    "right_input": "b",
                    "left_key": "k",
                    "right_key": "k",
                    "outputs": [
                        {"alias": "x", "side": "left", "column": "az"},
                        {"alias": "y", "side": "right", "column": "bz"},
                    ],
                },
                {
                    "id": "p",
                    "type": "projection",
                    "input": "j",
                    "columns": [
                        {"name": "x", "copy": "x"},
                        {"name": "y", "copy": "y"},
                        {"name": "c", "constant": 0},
                    ],
                },
            ],
            "export": "p",
        }
    )
    cols = {c["name"]: c for c in res["columns"]}
    assert cols["x"]["content_reasons"] == [{"node_id": "a", "column": "az"}]
    assert cols["y"]["content_reasons"] == [{"node_id": "b", "column": "bz"}]
    assert cols["c"]["row_reasons"] == []


# ----------------------------- rejection cases -----------------------------


def expect_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        call(payload)
    assert exc.value.code == code


def test_duplicate_node_id_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {"id": "s", "type": "source", "columns": [{"name": "b"}]},
            ],
            "export": "s",
        },
        "DUPLICATE_NODE",
    )


def test_duplicate_source_columns_rejected():
    expect_error(
        {
            "nodes": [
                {
                    "id": "s",
                    "type": "source",
                    "columns": [{"name": "a"}, {"name": "a"}],
                }
            ],
            "export": "s",
        },
        "COLUMN_CONFLICT",
    )


def test_duplicate_projection_outputs_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {
                    "id": "p",
                    "type": "projection",
                    "input": "s",
                    "columns": [
                        {"name": "o", "copy": "a"},
                        {"name": "o", "constant": 1},
                    ],
                },
            ],
            "export": "p",
        },
        "COLUMN_CONFLICT",
    )


def test_duplicate_join_aliases_rejected():
    expect_error(
        {
            "nodes": [
                {
                    "id": "l",
                    "type": "source",
                    "columns": [{"name": "k"}, {"name": "a"}],
                },
                {
                    "id": "r",
                    "type": "source",
                    "columns": [{"name": "k"}, {"name": "a"}],
                },
                {
                    "id": "j",
                    "type": "join",
                    "left_input": "l",
                    "right_input": "r",
                    "left_key": "k",
                    "right_key": "k",
                    "outputs": [
                        {"alias": "dup", "side": "left", "column": "a"},
                        {"alias": "dup", "side": "right", "column": "a"},
                    ],
                },
            ],
            "export": "j",
        },
        "COLUMN_CONFLICT",
    )


def test_unknown_export_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]}
            ],
            "export": "missing",
        },
        "INVALID_REFERENCE",
    )


def test_unknown_input_node_rejected():
    expect_error(
        {
            "nodes": [
                {
                    "id": "p",
                    "type": "projection",
                    "input": "ghost",
                    "columns": [{"name": "a", "constant": 1}],
                }
            ],
            "export": "p",
        },
        "INVALID_REFERENCE",
    )


def test_unknown_column_reference_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {
                    "id": "f",
                    "type": "filter",
                    "input": "s",
                    "references": ["nope"],
                },
            ],
            "export": "f",
        },
        "INVALID_REFERENCE",
    )


def test_unknown_projection_copy_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {
                    "id": "p",
                    "type": "projection",
                    "input": "s",
                    "columns": [{"name": "o", "copy": "nope"}],
                },
            ],
            "export": "p",
        },
        "INVALID_REFERENCE",
    )


def test_unknown_join_key_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "l", "type": "source", "columns": [{"name": "k"}]},
                {"id": "r", "type": "source", "columns": [{"name": "k"}]},
                {
                    "id": "j",
                    "type": "join",
                    "left_input": "l",
                    "right_input": "r",
                    "left_key": "nope",
                    "right_key": "k",
                    "outputs": [
                        {"alias": "k", "side": "left", "column": "k"}
                    ],
                },
            ],
            "export": "j",
        },
        "INVALID_REFERENCE",
    )


def test_cycle_rejected():
    expect_error(
        {
            "nodes": [
                {
                    "id": "a",
                    "type": "projection",
                    "input": "b",
                    "columns": [{"name": "x", "constant": 1}],
                },
                {
                    "id": "b",
                    "type": "projection",
                    "input": "a",
                    "columns": [{"name": "x", "constant": 1}],
                },
            ],
            "export": "a",
        },
        "CYCLE",
    )


def test_self_loop_rejected():
    expect_error(
        {
            "nodes": [
                {
                    "id": "a",
                    "type": "filter",
                    "input": "a",
                    "references": ["x"],
                }
            ],
            "export": "a",
        },
        "CYCLE",
    )


def test_unreachable_branch_with_cycle_is_still_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {
                    "id": "x",
                    "type": "projection",
                    "input": "y",
                    "columns": [{"name": "a", "constant": 1}],
                },
                {
                    "id": "y",
                    "type": "projection",
                    "input": "x",
                    "columns": [{"name": "a", "constant": 1}],
                },
            ],
            "export": "s",
        },
        "CYCLE",
    )


def test_unreachable_branch_with_bad_column_reference_is_still_rejected():
    expect_error(
        {
            "nodes": [
                {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                {
                    "id": "f",
                    "type": "filter",
                    "input": "s",
                    "references": ["ghost"],
                },
            ],
            "export": "s",
        },
        "INVALID_REFERENCE",
    )


def test_self_join_with_same_branch_input_is_ok():
    # Joining one relation to itself (same input twice) is not a cycle.
    res = call(
        {
            "nodes": [
                {
                    "id": "s",
                    "type": "source",
                    "columns": [
                        {"name": "k", "sensitive": False},
                        {"name": "v", "sensitive": False},
                    ],
                },
                {
                    "id": "j",
                    "type": "join",
                    "left_input": "s",
                    "right_input": "s",
                    "left_key": "k",
                    "right_key": "k",
                    "outputs": [
                        {"alias": "v1", "side": "left", "column": "v"},
                        {"alias": "v2", "side": "right", "column": "v"},
                    ],
                },
            ],
            "export": "j",
        }
    )
    assert res["blocked"] is False


# ----------------------------- schema validation ---------------------------


def test_projection_column_requires_copy_or_constant():
    with pytest.raises(PydanticValidationError):
        call(
            {
                "nodes": [
                    {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                    {
                        "id": "p",
                        "type": "projection",
                        "input": "s",
                        "columns": [{"name": "o"}],
                    },
                ],
                "export": "p",
            }
        )


def test_projection_column_rejects_both_copy_and_constant():
    with pytest.raises(PydanticValidationError):
        call(
            {
                "nodes": [
                    {"id": "s", "type": "source", "columns": [{"name": "a"}]},
                    {
                        "id": "p",
                        "type": "projection",
                        "input": "s",
                        "columns": [
                            {"name": "o", "copy": "a", "constant": 1}
                        ],
                    },
                ],
                "export": "p",
            }
        )


def test_more_than_60_nodes_rejected():
    nodes = [
        {"id": "s0", "type": "source", "columns": [{"name": "a"}]}
    ]
    for i in range(1, 61):
        nodes.append(
            {
                "id": f"n{i}",
                "type": "projection",
                "input": f"n{i - 1}" if i > 1 else "s0",
                "columns": [{"name": "a", "copy": "a"}],
            }
        )
    with pytest.raises(PydanticValidationError):
        call({"nodes": nodes, "export": "n60"})
