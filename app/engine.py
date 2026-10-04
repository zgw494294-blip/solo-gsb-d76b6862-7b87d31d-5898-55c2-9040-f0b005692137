"""Sensitive-provenance analysis over the node graph.

Two independent kinds of provenance are tracked for every node:

* ``content_origins`` (per column): sensitive source columns whose *values*
  reach the column. Copy propagates them; a constant replacement clears them.
* ``row_origins`` (per relation): sensitive source columns that can influence
  *which rows* the relation contains. Filters referencing a sensitive column,
  and joins whose join key carries sensitive content, taint the whole result;
  projections and constant replacements can never remove that taint, and a
  join merges the taint of both inputs.

A column of the export node is blocked when either set is non-empty.

``revise`` turns a blocked graph into one that passes review by appending a
single projection after the export node: columns carrying sensitive *content*
may be replaced by a constant ``null`` (subject to the caller-provided allow
list), while every other column is copied. Row-level influence cannot be
removed that way, so such graphs are reported as not revisable.

``rename`` batch-renames columns of one source node. Names propagate through
filters (predicate references and the passed-through columns), while
projection output names and join aliases are the boundary where the change
stops (copy/key/output ``column`` references are still rewritten to keep
pointing at the same original columns).

``compose`` splices one graph's export into another graph's source position:
the downstream source node is replaced in place by a single projection that
copies the mapped upstream export columns, and the upstream export together
with its dependency ancestors is appended (ids renamed where they collide).
Provenance at the replacement position is taken from the upstream graph.

``compare`` validates two whole graphs independently and diffs their export
reviews column by column (columns paired by name, origins identified by
``(source node id, source column name)``), listing content and row origins
added or removed by the candidate and flagging the risk the candidate newly
introduces.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Union

from .models import (
    CompareRequest,
    ComposeRequest,
    FilterNodeIn,
    JoinNodeIn,
    PreviewRequest,
    ProjectionNodeIn,
    PruneRequest,
    RenameRequest,
    ReviewRequest,
    ReviseRequest,
    SourceNodeIn,
)

# Hard cap shared with the request models: a revised graph may never exceed it.
NODE_LIMIT = 60

# Small-sample preview cap: every source and every intermediate relation may
# hold at most this many rows; exceeding it rejects the whole preview request.
PREVIEW_ROW_LIMIT = 200


class ValidationError(Exception):
    """The whole request must be rejected (HTTP 400)."""

    def __init__(
        self,
        code: str,
        message: str,
        node_id: str | None = None,
        extra: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id
        self.extra = extra or {}

    def detail(self) -> dict[str, object]:
        d: dict[str, object] = {"code": self.code, "message": self.message}
        if self.node_id is not None:
            d["node_id"] = self.node_id
        d.update(self.extra)
        return d


# A sensitive origin is a (source node id, source column name) pair.
Origin = tuple[str, str]


class Column:
    __slots__ = ("name", "content_origins")

    def __init__(self, name: str, content_origins: frozenset[Origin]):
        self.name = name
        self.content_origins = content_origins


class Relation:
    __slots__ = ("columns", "row_origins")

    def __init__(self, columns: list[Column], row_origins: frozenset[Origin]):
        self.columns = columns
        self.row_origins = row_origins

    def column_map(self) -> dict[str, Column]:
        return {c.name: c for c in self.columns}


def _unique(names: list[str], what: str, node_id: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValidationError(
                "COLUMN_CONFLICT",
                f"{what} '{name}' is declared more than once in node '{node_id}'",
                node_id,
            )
        seen.add(name)


def _inputs(node: object) -> list[str]:
    if isinstance(node, (ProjectionNodeIn, FilterNodeIn)):
        return [node.input]
    if isinstance(node, JoinNodeIn):
        return [node.left_input, node.right_input]
    return []  # source nodes have no inputs


def topological_order(by_id: dict[str, object]) -> list[str]:
    """Return every node in dependency order.

    Edges go from an input node to its consumer. A request is rejected when an
    input id is unknown or the graph contains a cycle (self-loop included).
    The *whole* request is validated even for nodes the export does not use.
    """
    indegree: dict[str, int] = {nid: 0 for nid in by_id}
    edges: dict[str, list[str]] = defaultdict(list)
    for nid, node in by_id.items():
        for ref in _inputs(node):
            if ref not in by_id:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"node '{nid}' references unknown input node '{ref}'",
                    nid,
                )
            edges[ref].append(nid)
            indegree[nid] += 1

    # Deterministic traversal: ids sorted alphabetically.
    ready = deque(sorted(nid for nid, d in indegree.items() if d == 0))
    order: list[str] = []
    while ready:
        nid = ready.popleft()
        order.append(nid)
        for consumer in sorted(edges[nid]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)

    if len(order) != len(by_id):
        stuck = sorted(nid for nid, d in indegree.items() if d > 0)
        raise ValidationError(
            "CYCLE",
            "dependency cycle detected involving nodes: " + ", ".join(stuck),
        )
    return order


def _eval_source(
    node: SourceNodeIn, extra_sensitive: frozenset[Origin]
) -> Relation:
    columns = [
        Column(
            col.name,
            # Effective sensitivity is the in-graph flag OR membership in the
            # shared sensitive-column registry; both root at the same origin.
            frozenset({(node.id, col.name)})
            if col.sensitive or (node.id, col.name) in extra_sensitive
            else frozenset(),
        )
        for col in node.columns
    ]
    return Relation(columns, frozenset())


def _eval_projection(node: ProjectionNodeIn, upstream: Relation) -> Relation:
    available = upstream.column_map()
    columns: list[Column] = []
    for spec in node.columns:
        if spec.is_constant:
            # A constant replacement clears the column's content provenance.
            columns.append(Column(spec.name, frozenset()))
        else:
            if spec.copy_name not in available:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"projection node '{node.id}' copies unknown column "
                    f"'{spec.copy_name}' from input '{node.input}'",
                    node.id,
                )
            columns.append(Column(spec.name, available[spec.copy_name].content_origins))
    # Projection cannot remove row-level influence of upstream operations.
    return Relation(columns, upstream.row_origins)


def _eval_filter(node: FilterNodeIn, upstream: Relation) -> Relation:
    available = upstream.column_map()
    row_origins = set(upstream.row_origins)
    for ref in node.references:
        if ref not in available:
            raise ValidationError(
                "INVALID_REFERENCE",
                f"filter node '{node.id}' references unknown column "
                f"'{ref}' from input '{node.input}'",
                node.id,
            )
        # A sensitive predicate column affects every surviving row.
        row_origins |= available[ref].content_origins
    # The filter keeps all input columns, contents untouched.
    return Relation(
        [Column(c.name, c.content_origins) for c in upstream.columns],
        frozenset(row_origins),
    )


def _eval_join(
    node: JoinNodeIn, left: Relation, right: Relation
) -> Relation:
    left_cols = left.column_map()
    right_cols = right.column_map()

    for side_name, key, cols, input_id in (
        ("left", node.left_key, left_cols, node.left_input),
        ("right", node.right_key, right_cols, node.right_input),
    ):
        if key not in cols:
            raise ValidationError(
                "INVALID_REFERENCE",
                f"join node '{node.id}' uses unknown {side_name} key '{key}' "
                f"from input '{input_id}'",
                node.id,
            )

    # Row influence: both inputs' influence, plus sensitive key content on
    # either side, affects every row of the joined result.
    row_origins = (
        left.row_origins
        | right.row_origins
        | left_cols[node.left_key].content_origins
        | right_cols[node.right_key].content_origins
    )

    columns: list[Column] = []
    for out in node.outputs:
        if out.side == "left":
            if out.column not in left_cols:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"join node '{node.id}' outputs unknown left column "
                    f"'{out.column}'",
                    node.id,
                )
            columns.append(Column(out.alias, left_cols[out.column].content_origins))
        else:
            if out.column not in right_cols:
                raise ValidationError(
                    "INVALID_REFERENCE",
                    f"join node '{node.id}' outputs unknown right column "
                    f"'{out.column}'",
                    node.id,
                )
            columns.append(Column(out.alias, right_cols[out.column].content_origins))
    return Relation(columns, row_origins)


def _origin_view(origins: frozenset[Origin]) -> list[dict[str, str]]:
    return [
        {"node_id": node_id, "column": column}
        for node_id, column in sorted(origins)
    ]


def _build_by_id(nodes: list[object]) -> dict[str, object]:
    """Check unique node/column names and index nodes by id."""
    by_id: dict[str, object] = {}
    for node in nodes:
        if node.id in by_id:
            raise ValidationError(
                "DUPLICATE_NODE",
                f"node id '{node.id}' is used more than once",
                node.id,
            )
        by_id[node.id] = node

        if isinstance(node, SourceNodeIn):
            _unique([c.name for c in node.columns], "column", node.id)
        elif isinstance(node, ProjectionNodeIn):
            _unique([c.name for c in node.columns], "output column", node.id)
        elif isinstance(node, FilterNodeIn):
            _unique(node.references, "reference", node.id)
        elif isinstance(node, JoinNodeIn):
            _unique([o.alias for o in node.outputs], "output alias", node.id)
    return by_id


def _evaluate_graph(
    request: Union[
        ReviewRequest, ReviseRequest, PruneRequest, RenameRequest, PreviewRequest
    ],
    extra_sensitive: frozenset[Origin] = frozenset(),
) -> tuple[dict[str, object], list[str], dict[str, Relation]]:
    """Run every original validation rule and evaluate all relations.

    ``extra_sensitive`` holds shared sensitive-column registry entries
    identified by ``(source node id, column name)``; the listed source columns
    are treated as sensitive *in addition to* every in-graph ``sensitive``
    flag (an OR). Entries naming unknown sources or columns are simply inert:
    a registered graph mentioning neither is unaffected, and a registry entry
    for a source the graph does not declare cannot influence anything.

    Returns the node index, topological order and per-node relations. Raises
    :class:`ValidationError` exactly as ``review`` always has.
    """
    by_id = _build_by_id(list(request.nodes))

    if request.export not in by_id:
        raise ValidationError(
            "INVALID_REFERENCE",
            f"export node '{request.export}' does not exist",
            request.export,
        )

    # Validate and evaluate the whole graph: invalid references in branches
    # the export does not use still reject the whole request. At most 60 nodes.
    order = topological_order(by_id)

    relations: dict[str, Relation] = {}
    for nid in order:
        node = by_id[nid]
        if isinstance(node, SourceNodeIn):
            relations[nid] = _eval_source(node, extra_sensitive)
        elif isinstance(node, ProjectionNodeIn):
            relations[nid] = _eval_projection(node, relations[node.input])
        elif isinstance(node, FilterNodeIn):
            relations[nid] = _eval_filter(node, relations[node.input])
        else:
            relations[nid] = _eval_join(
                node, relations[node.left_input], relations[node.right_input]
            )
    return by_id, order, relations


def _review_export(
    export_id: str, relations: dict[str, Relation]
) -> dict[str, object]:
    """Review every column of one evaluated relation."""
    export_relation = relations[export_id]
    columns_out: list[dict[str, object]] = []
    any_blocked = False
    for column in export_relation.columns:
        content = column.content_origins
        rows = export_relation.row_origins
        blocked = bool(content) or bool(rows)
        any_blocked = any_blocked or blocked
        columns_out.append(
            {
                "name": column.name,
                "blocked": blocked,
                "content_reasons": _origin_view(content),
                "row_reasons": _origin_view(rows),
            }
        )

    return {
        "export": export_id,
        "blocked": any_blocked,
        "columns": columns_out,
    }


def review(
    request: ReviewRequest,
    extra_sensitive: frozenset[Origin] = frozenset(),
) -> dict[str, object]:
    _by_id, _order, relations = _evaluate_graph(request, extra_sensitive)
    return _review_export(request.export, relations)


def serialize_node(node: object) -> dict[str, object]:
    """Dump a validated node back into request-compatible JSON.

    ``exclude_unset`` is essential for projection columns: an unset
    ``constant`` must not be emitted as ``null`` next to a ``copy``.
    """
    return node.model_dump(by_alias=True, exclude_unset=True)  # type: ignore[attr-defined]


def _safe_projection_id(export_id: str, by_id: dict[str, object]) -> str:
    """Pick a deterministic projection id that cannot collide with originals."""
    base = f"{export_id}__safe_export"
    candidate = base
    suffix = 2
    while candidate in by_id:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def revise(request: ReviseRequest) -> dict[str, object]:
    """Produce a re-submittable, review-safe version of a reviewed graph.

    The original graph is validated and reviewed under exactly the same rules
    as :func:`review`. When it is already safe it is returned untouched (no new
    node, no replaced columns). Otherwise one projection may be appended after
    the export node; the request is rejected as not revisable (without any
    partial graph) when row-level influence exists, a blocked column is not on
    the allow list, or the appended node would exceed the node limit.
    """
    by_id, _order, relations = _evaluate_graph(request)
    export_relation = relations[request.export]
    export_column_names = {c.name for c in export_relation.columns}

    # The allow list itself is part of the request contract: every name must
    # resolve on the export node (distinctness is enforced by the model).
    missing = [n for n in request.allowed_replacements if n not in export_column_names]
    if missing:
        raise ValidationError(
            "REPLACEMENT_COLUMN_NOT_FOUND",
            "allowed replacement column(s) not found on export node "
            f"'{request.export}': " + ", ".join(missing),
            request.export,
            {"columns": missing},
        )

    review_result = _review_export(request.export, relations)
    original_nodes = [serialize_node(n) for n in request.nodes]

    # Already safe: return the graph as-is, nothing appended, nothing replaced.
    if not review_result["blocked"]:
        return {
            "nodes": original_nodes,
            "export": request.export,
            "revised": False,
            "replaced_columns": [],
            "review": review_result,
        }

    # Row-level influence lands on every column; a trailing projection cannot
    # remove it, so no revision is possible.
    if export_relation.row_origins:
        raise ValidationError(
            "UNREVISABLE_ROW_INFLUENCE",
            "the export relation is influenced at row level by sensitive "
            "columns; appending a projection cannot remove row-level influence",
            request.export,
            {"row_reasons": _origin_view(export_relation.row_origins)},
        )

    # With no row taint every blocked column carries sensitive content; each
    # one must be on the allow list or the revision is impossible.
    allowed = set(request.allowed_replacements)
    not_allowed = [
        c.name for c in export_relation.columns if c.content_origins and c.name not in allowed
    ]
    if not_allowed:
        raise ValidationError(
            "UNREVISABLE_COLUMN_NOT_ALLOWED",
            "blocked export column(s) are not in the allowed replacement list: "
            + ", ".join(not_allowed),
            request.export,
            {"columns": not_allowed},
        )

    if len(request.nodes) + 1 > NODE_LIMIT:
        raise ValidationError(
            "UNREVISABLE_NODE_LIMIT",
            f"appending the safe-export projection would exceed the "
            f"{NODE_LIMIT}-node limit (graph already has {len(request.nodes)} "
            "nodes)",
            request.export,
            {"node_count": len(request.nodes), "limit": NODE_LIMIT},
        )

    # Append exactly one projection: same column names and order, same row set;
    # permitted content-sensitive columns become constant null, others copy.
    projection_id = _safe_projection_id(request.export, by_id)
    projection_columns: list[dict[str, object]] = []
    replaced_columns: list[str] = []
    for column in export_relation.columns:
        if column.content_origins and column.name in allowed:
            projection_columns.append({"name": column.name, "constant": None})
            replaced_columns.append(column.name)
        else:
            projection_columns.append({"name": column.name, "copy": column.name})

    new_nodes = original_nodes + [
        {
            "id": projection_id,
            "type": "projection",
            "input": request.export,
            "columns": projection_columns,
        }
    ]

    # Re-review the constructed graph under the original rules before returning.
    re_review_payload = ReviewRequest.model_validate(
        {"nodes": new_nodes, "export": projection_id}
    )
    re_review_result = review(re_review_payload)
    if re_review_result["blocked"]:  # pragma: no cover - invariant by construction
        raise ValidationError(
            "UNREVISABLE_RESIDUAL_RISK",
            "internal error: the revised export is still blocked",
            projection_id,
        )

    return {
        "nodes": new_nodes,
        "export": projection_id,
        "revised": True,
        "replaced_columns": replaced_columns,
        "review": re_review_result,
    }


# ---------------------------------------------------------------------------
# Pruning by selected export columns
# ---------------------------------------------------------------------------


def _ancestors(export_id: str, by_id: dict[str, object]) -> set[str]:
    """All nodes the export depends on (the export itself included).

    A shared node feeds every branch that needs it; the ancestor closure is
    exactly the node set that may influence the export's columns or row set.
    Every input relation of a retained node is therefore retained too — a
    constant-only projection keeps its upstream even though it copies nothing.
    """
    keep: set[str] = {export_id}
    stack = [export_id]
    while stack:
        for ref in _inputs(by_id[stack.pop()]):
            if ref not in keep:
                keep.add(ref)
                stack.append(ref)
    return keep


def _safe_projection_id_from(base: str, used: set[str]) -> str:
    """Pick a deterministic projection id that cannot collide with ``used``."""
    candidate = f"{base}__export"
    suffix = 2
    while candidate in used:
        candidate = f"{base}__export_{suffix}"
        suffix += 1
    return candidate


def _column_reason_view(review_result: dict[str, object]) -> dict[str, object]:
    """Map column name -> (content reasons, row reasons) for review comparison."""
    return {
        col["name"]: (col["content_reasons"], col["row_reasons"])
        for col in review_result["columns"]
    }


def _compare_selected_columns(
    original: dict[str, object],
    pruned: dict[str, object],
    selected: list[str],
) -> dict[str, object]:
    """Return per-column discrepancies of the pruned review against original.

    The pruned export must expose exactly the selected columns, in the export's
    original order, and every one must carry the same content/row reasons as in
    the original graph (pruning is not a safety transform, so reasons must be
    preserved, never weakened). Used both by :func:`prune` and its tests.
    """
    orig_cols = _column_reason_view(original)
    pruned_cols = _column_reason_view(pruned)
    pruned_order = list(pruned_cols)

    diffs: dict[str, object] = {}
    missing = [name for name in selected if name not in pruned_cols]
    extra = [name for name in pruned_order if name not in selected]
    if missing:
        diffs["missing_columns"] = missing
    if extra:
        diffs["extra_columns"] = extra
    if pruned_order != selected:
        diffs["expected_order"] = selected
        diffs["actual_order"] = pruned_order

    mismatched: list[dict[str, object]] = []
    for name in selected:
        if name not in pruned_cols or name not in orig_cols:
            continue
        o_content, o_rows = orig_cols[name]
        p_content, p_rows = pruned_cols[name]
        if o_content != p_content or o_rows != p_rows:
            mismatched.append(
                {
                    "name": name,
                    "original": {
                        "content_reasons": o_content,
                        "row_reasons": o_rows,
                    },
                    "pruned": {
                        "content_reasons": p_content,
                        "row_reasons": p_rows,
                    },
                }
            )
    if mismatched:
        diffs["mismatched_columns"] = mismatched
    return diffs


def prune(request: PruneRequest) -> dict[str, object]:
    """Narrow a graph to a chosen subset of export columns.

    The whole original graph is validated exactly as :func:`review` does, and
    every selected column must exist on the export node. The export node and
    all of its ancestor nodes are retained (shared nodes satisfy every branch
    feeding them); nodes unreachable from the export are dropped. Demand for
    the selected columns is seeded on the export and propagated bottom-up, and
    every node's outputs are narrowed by it — the export projection/join is
    trimmed just like any other:

    * source columns are never trimmed;
    * a filter keeps its predicate columns and passes every surviving input
      column straight through — a filter export therefore needs no trailing
      projection when an upstream projection/join already shed the unselected
      columns, and only needs one when the predicate (or a source column) still
      keeps an unselected column alive;
    * a projection and a join emit only the outputs that are needed;
    * a retained projection/join with no demanded output keeps its original
      first output (and that output's dependencies) — the export itself never
      hits this case, since the selection is non-empty;
    * both join keys are always demanded on their side inputs, and constant
      outputs demand no upstream content while their input relation is kept.

    A trailing projection (copying only the selected columns, in original
    order) is appended solely when, after the in-place narrowing, the export
    node still actually exposes columns the caller did not select — columns
    can vanish on their way through a trimmed upstream projection/join even
    when the export itself is a filter. The result must stay within the
    60-node cap and re-review identically for the selected columns; otherwise
    the request is rejected without any partial graph.
    """
    by_id, order, relations = _evaluate_graph(request)
    export_relation = relations[request.export]
    export_columns = [c.name for c in export_relation.columns]
    export_column_set = set(export_columns)

    # The selection is part of the request contract: every name must resolve on
    # the export node (distinctness/non-emptiness is enforced by the model).
    missing = [name for name in request.columns if name not in export_column_set]
    if missing:
        raise ValidationError(
            "PRUNE_COLUMN_NOT_FOUND",
            "selected column(s) not found on export node "
            f"'{request.export}': " + ", ".join(missing),
            request.export,
            {"columns": missing},
        )

    selected_set = set(request.columns)
    selected_in_order = [name for name in export_columns if name in selected_set]
    original_review = _review_export(request.export, relations)

    # --- node retention: export plus every dependency ancestor --------------
    keep = _ancestors(request.export, by_id)

    # --- reverse pass: which output columns each retained node must emit ----
    # The export node is demanded exactly the selected columns, nothing more;
    # it is trimmed in place like every other projection/join.
    demanded: dict[str, set[str]] = {request.export: set(selected_in_order)}
    for nid in reversed(order):
        if nid not in keep:
            continue
        node = by_id[nid]
        needed = demanded.setdefault(nid, set())

        if isinstance(node, SourceNodeIn):
            continue  # source columns are never trimmed

        if isinstance(node, FilterNodeIn):
            # Keep the predicate columns and whatever downstream still reads.
            demanded.setdefault(node.input, set()).update(needed)
            demanded[node.input].update(node.references)

        elif isinstance(node, ProjectionNodeIn):
            specs_by_name = {spec.name: spec for spec in node.columns}
            if not needed:
                # No live output: preserve the node's first output instead
                # of emitting an (illegal) empty projection. Mutate the set
                # stored in ``demanded`` so the build phase sees the choice.
                needed.add(node.columns[0].name)
            for name in needed:
                spec = specs_by_name[name]
                if not spec.is_constant:
                    # A constant demands no upstream content, but its input
                    # relation stays via the ancestor closure.
                    demanded.setdefault(node.input, set()).add(spec.copy_name)

        else:  # JoinNodeIn
            if not needed:
                needed.add(node.outputs[0].alias)
            left_need = demanded.setdefault(node.left_input, set())
            right_need = demanded.setdefault(node.right_input, set())
            # Both keys are always required: they shape the joined row set.
            left_need.add(node.left_key)
            right_need.add(node.right_key)
            outputs_by_alias = {out.alias: out for out in node.outputs}
            for alias in needed:
                out = outputs_by_alias[alias]
                if out.side == "left":
                    left_need.add(out.column)
                else:
                    right_need.add(out.column)

    # --- build the pruned nodes, keeping the original list/column order -----
    new_node_dicts: list[dict[str, object]] = []
    trimmed = False
    for node in request.nodes:
        nid = node.id
        if nid not in keep:
            continue
        data = serialize_node(node)

        if isinstance(node, ProjectionNodeIn):
            wanted = demanded[nid]
            kept_cols = [
                col for col in data["columns"] if col["name"] in wanted
            ]
            if len(kept_cols) != len(data["columns"]):
                trimmed = True
            data["columns"] = kept_cols

        elif isinstance(node, JoinNodeIn):
            wanted = demanded[nid]
            kept_outs = [
                out for out in data["outputs"] if out["alias"] in wanted
            ]
            if len(kept_outs) != len(data["outputs"]):
                trimmed = True
            data["outputs"] = kept_outs

        new_node_dicts.append(data)

    dropped_nodes = [node.id for node in request.nodes if node.id not in keep]

    # --- evaluate the in-place-pruned graph before deciding on an append -----
    # Whether the export still exposes unselected columns depends on the whole
    # narrowed pipeline, not just the export node's type: a filter passes its
    # input columns through, so columns already shed by an upstream trimmed
    # projection/join do not reappear at a filter export. Build the candidate
    # graph first and read the export's actual columns from its evaluation.
    pre_payload = ReviewRequest.model_validate(
        {"nodes": new_node_dicts, "export": request.export}
    )
    _pre_by_id, _pre_order, pre_relations = _evaluate_graph(pre_payload)
    pre_export_columns = [c.name for c in pre_relations[request.export].columns]

    needs_trailing = pre_export_columns != selected_in_order

    # --- trailing projection only if the export still exposes other columns -
    if needs_trailing and len(new_node_dicts) + 1 > NODE_LIMIT:
        raise ValidationError(
            "PRUNE_NODE_LIMIT",
            f"appending the export projection would exceed the {NODE_LIMIT}-node "
            f"limit (pruned graph already has {len(new_node_dicts)} nodes)",
            request.export,
            {"node_count": len(new_node_dicts) + 1, "limit": NODE_LIMIT},
        )

    final_export = request.export
    added_node = False
    if needs_trailing:
        trailing_id = _safe_projection_id_from(
            request.export, {n["id"] for n in new_node_dicts}
        )
        new_node_dicts.append(
            {
                "id": trailing_id,
                "type": "projection",
                "input": request.export,
                "columns": [
                    {"name": name, "copy": name} for name in selected_in_order
                ],
            }
        )
        final_export = trailing_id
        added_node = True

    # --- validate and re-review the constructed graph under original rules --
    re_payload = ReviewRequest.model_validate(
        {"nodes": new_node_dicts, "export": final_export}
    )
    if added_node:
        pruned_review = review(re_payload)
    else:
        # No node was appended: reuse the candidate evaluation instead of
        # walking the identical graph a second time.
        pruned_review = _review_export(final_export, pre_relations)

    diffs = _compare_selected_columns(
        original_review, pruned_review, selected_in_order
    )
    if diffs:  # pragma: no cover - invariant by the demand construction
        raise ValidationError(
            "PRUNE_REVIEW_MISMATCH",
            "the pruned export does not preserve the original review for the "
            "selected columns",
            final_export,
            {"differences": diffs},
        )

    return {
        "nodes": new_node_dicts,
        "export": final_export,
        "columns": selected_in_order,
        "pruned": bool(dropped_nodes or trimmed or added_node),
        "dropped_nodes": dropped_nodes,
        "added_projection": final_export if added_node else None,
        "review": pruned_review,
    }


# ---------------------------------------------------------------------------
# Batch-renaming source columns
# ---------------------------------------------------------------------------


def _translate_reasons(
    reasons: list[dict[str, str]],
    source_id: str,
    new_to_old: dict[str, str],
) -> list[dict[str, str]]:
    """Map a renamed graph's reason list back onto the original column names.

    Only reasons rooted at the renamed source node are translated: same-named
    columns of other sources keep their names, as do names no rename touched.
    """
    translated: list[dict[str, str]] = []
    for reason in reasons:
        column = reason["column"]
        if reason["node_id"] == source_id and column in new_to_old:
            column = new_to_old[column]
        translated.append({"node_id": reason["node_id"], "column": column})
    return translated


def _compare_renamed_review(
    original: dict[str, object],
    renamed: dict[str, object],
    source_id: str,
    export_map: dict[str, str],
    new_to_old: dict[str, str],
) -> dict[str, object]:
    """Check the renamed review against the original under the renames.

    Columns are compared positionally (the rename neither adds nor removes
    columns nor changes their order): each renamed export column must carry
    the expected passthrough-renamed name and the same blocked verdict, and
    its content/row reasons must equal the original's once the renamed source
    column names are mapped back. Used both by :func:`rename` and its tests.
    """
    diffs: dict[str, object] = {}
    if original["blocked"] != renamed["blocked"]:
        diffs["blocked"] = {
            "original": original["blocked"],
            "renamed": renamed["blocked"],
        }

    o_cols = original["columns"]
    n_cols = renamed["columns"]
    if len(o_cols) != len(n_cols):
        diffs["column_count"] = {"original": len(o_cols), "renamed": len(n_cols)}
        return diffs

    mismatched: list[dict[str, object]] = []
    for index, (o_col, n_col) in enumerate(zip(o_cols, n_cols)):
        expected_name = export_map.get(o_col["name"], o_col["name"])
        problems: dict[str, object] = {}
        if n_col["name"] != expected_name:
            problems["expected_name"] = expected_name
            problems["actual_name"] = n_col["name"]
        if n_col["blocked"] != o_col["blocked"]:
            problems["blocked"] = {
                "original": o_col["blocked"],
                "renamed": n_col["blocked"],
            }
        for kind in ("content_reasons", "row_reasons"):
            mapped_back = _translate_reasons(
                n_col[kind], source_id, new_to_old
            )
            if mapped_back != o_col[kind]:
                problems[kind] = {
                    "original": o_col[kind],
                    "renamed": n_col[kind],
                    "renamed_mapped_back": mapped_back,
                }
        if problems:
            mismatched.append(
                {"index": index, "column": o_col["name"], **problems}
            )
    if mismatched:
        diffs["mismatched_columns"] = mismatched
    return diffs


def rename(request: RenameRequest) -> dict[str, object]:
    """Batch-rename columns of one source node, keeping references consistent.

    The whole original graph is validated exactly as :func:`review` does. The
    named source must exist and be a source, every original name must resolve
    on it (distinctness/non-emptiness is enforced by the model), and the
    renames take effect simultaneously — swaps are allowed but the source's
    final column names must stay distinct. Names then propagate into *every*
    downstream branch, including branches the export does not use:

    * source column definitions take their new names, ``sensitive`` flags and
      column order untouched;
    * a filter renames its predicate references and passes the rename straight
      through to its own columns;
    * a projection renames each ``copy`` input reference, while output names
      (and constants) are unchanged — the name change stops there;
    * a join renames the relevant side's key and its output ``column``
      references, while output aliases are unchanged — the change stops there;
    * other sources, their same-named columns and all constant values are
      never touched.

    No node or column is added or removed; ids, input edges and all orders are
    preserved. The rewritten graph is re-validated and re-reviewed under the
    original rules; the blocked verdict must be unchanged and the reason sets
    must match after mapping the renamed source columns back, otherwise the
    whole request is rejected without any partial graph.
    """
    by_id, order, relations = _evaluate_graph(request)

    source_node = by_id.get(request.source)
    if not isinstance(source_node, SourceNodeIn):
        raise ValidationError(
            "RENAME_SOURCE_NOT_FOUND",
            f"source node '{request.source}' does not exist or is not a "
            "source node",
            request.source,
        )

    source_names = [c.name for c in source_node.columns]
    source_name_set = set(source_names)
    missing = [
        item.old_name
        for item in request.renames
        if item.old_name not in source_name_set
    ]
    if missing:
        raise ValidationError(
            "RENAME_COLUMN_NOT_FOUND",
            "original column(s) not found on source node "
            f"'{request.source}': " + ", ".join(missing),
            request.source,
            {"columns": missing},
        )

    # Apply simultaneously: final source names must remain distinct. Two old
    # names pointing at one new name collide; a swap (a->b, b->a) does not.
    direct = {item.old_name: item.new_name for item in request.renames}
    final_names = [direct.get(name, name) for name in source_names]
    seen: set[str] = set()
    conflicts: list[str] = []
    for name in final_names:
        if name in seen and name not in conflicts:
            conflicts.append(name)
        seen.add(name)
    if conflicts:
        raise ValidationError(
            "RENAME_CONFLICT",
            "renaming would leave duplicate column names on source node "
            f"'{request.source}': " + ", ".join(conflicts),
            request.source,
            {"columns": conflicts},
        )

    # Only genuinely changed pairs participate in propagation/reporting; a
    # pair listing the same old and new name is a harmless no-op.
    old_to_new = {old: new for old, new in direct.items() if old != new}
    new_to_old = {new: old for old, new in old_to_new.items()}

    original_review = _review_export(request.export, relations)

    # Serialize every node once, then propagate names in dependency order.
    # ``live[nid]`` is the old->new map still active for the columns of nid's
    # relation; a projection's output names and a join's aliases reset it.
    new_dicts = {nid: serialize_node(node) for nid, node in by_id.items()}
    live: dict[str, dict[str, str]] = {}

    for nid in order:
        node = by_id[nid]

        if isinstance(node, SourceNodeIn):
            if nid == request.source:
                for col in new_dicts[nid]["columns"]:
                    if col["name"] in old_to_new:
                        col["name"] = old_to_new[col["name"]]
                live[nid] = dict(old_to_new)
            continue

        if isinstance(node, FilterNodeIn):
            mapping = live.get(node.input)
            if mapping:
                new_dicts[nid]["references"] = [
                    mapping.get(ref, ref) for ref in new_dicts[nid]["references"]
                ]
                # A filter passes every input column straight through.
                live[nid] = mapping
            continue

        if isinstance(node, ProjectionNodeIn):
            mapping = live.get(node.input)
            if mapping:
                for col in new_dicts[nid]["columns"]:
                    if "copy" in col and col["copy"] in mapping:
                        col["copy"] = mapping[col["copy"]]
            # Output names are the boundary: the change does not continue.
            continue

        mapping_left = live.get(node.left_input)
        mapping_right = live.get(node.right_input)
        if mapping_left or mapping_right:
            data = new_dicts[nid]
            if mapping_left:
                if data["left_key"] in mapping_left:
                    data["left_key"] = mapping_left[data["left_key"]]
                for out in data["outputs"]:
                    if out["side"] == "left" and out["column"] in mapping_left:
                        out["column"] = mapping_left[out["column"]]
            if mapping_right:
                if data["right_key"] in mapping_right:
                    data["right_key"] = mapping_right[data["right_key"]]
                for out in data["outputs"]:
                    if out["side"] == "right" and out["column"] in mapping_right:
                        out["column"] = mapping_right[out["column"]]
        # Aliases are the boundary: join outputs keep their original names.

    renamed_nodes = [new_dicts[node.id] for node in request.nodes]

    # Re-validate and re-review the rewritten graph under the original rules.
    re_payload = ReviewRequest.model_validate(
        {"nodes": renamed_nodes, "export": request.export}
    )
    try:
        _re_by_id, _re_order, re_relations = _evaluate_graph(re_payload)
    except ValidationError as exc:  # pragma: no cover - invariant by construction
        raise ValidationError(
            "RENAME_REVIEW_MISMATCH",
            "the renamed graph fails validation under the original rules: "
            + exc.message,
            request.export,
            {"underlying": exc.detail()},
        )
    renamed_review = _review_export(request.export, re_relations)

    export_map = live.get(request.export, {})
    diffs = _compare_renamed_review(
        original_review, renamed_review, request.source, export_map, new_to_old
    )
    if diffs:  # pragma: no cover - invariant by the propagation construction
        raise ValidationError(
            "RENAME_REVIEW_MISMATCH",
            "the renamed export does not preserve the original review under "
            "the source column renames",
            request.export,
            {"differences": diffs},
        )

    return {
        "nodes": renamed_nodes,
        "export": request.export,
        "source": request.source,
        "renamed_columns": [
            {"from": old, "to": new} for old, new in old_to_new.items()
        ],
        "review": renamed_review,
    }


# ---------------------------------------------------------------------------
# Composing an upstream export into a downstream source position
# ---------------------------------------------------------------------------


def _fresh_upstream_id(old_id: str, used: set[str]) -> str:
    """Pick a deterministic graph-unique id for a conflicting upstream node."""
    candidate = f"{old_id}__upstream"
    suffix = 2
    while candidate in used:
        candidate = f"{old_id}__upstream_{suffix}"
        suffix += 1
    return candidate


def compose(request: ComposeRequest) -> dict[str, object]:
    """Splice an upstream graph's export into a downstream source position.

    Both whole graphs are validated under exactly the same rules as
    :func:`review` before anything is composed. The named downstream node
    must exist and be a source; the mapping must cover every one of its
    columns exactly (no missing, no extra entries — several source columns
    may draw from the same upstream export column) and every referenced
    upstream column must exist on the upstream export node's relation.

    The source is replaced *in place* by a single projection that keeps the
    original source id and copies the mapped upstream export columns in the
    source's original column order, so every downstream branch reference
    stays valid; all other downstream nodes are untouched. Only the upstream
    export and its dependency ancestors are appended, in the upstream nodes'
    original order and with their original column definitions. Upstream node
    ids colliding with a downstream id are renamed to graph-unique ids (all
    input references updated); the old->new id map of every introduced node
    is returned.

    Content provenance and row-level influence at the replacement position
    come from the upstream graph — the replaced source's ``sensitive`` flags
    do not survive. The two input graphs are never modified. The composed
    graph is re-validated and re-reviewed under the original rules (reasons
    therefore use the composed graph's ids); when it would exceed the
    60-node limit the whole request is rejected without any partial graph.
    """
    down_by_id, _down_order, _down_relations = _evaluate_graph(request.downstream)
    up_by_id, _up_order, up_relations = _evaluate_graph(request.upstream)

    source_node = down_by_id.get(request.source)
    if not isinstance(source_node, SourceNodeIn):
        raise ValidationError(
            "COMPOSE_SOURCE_NOT_FOUND",
            f"source node '{request.source}' does not exist or is not a "
            "source node",
            request.source,
        )

    # The mapping must cover exactly the source's columns: no source column
    # left unmapped, no entry naming a column the source does not have.
    source_columns = [c.name for c in source_node.columns]
    source_column_set = set(source_columns)
    mapped = {item.column: item.upstream_column for item in request.mapping}
    missing = [name for name in source_columns if name not in mapped]
    extra = [
        item.column for item in request.mapping if item.column not in source_column_set
    ]
    if missing or extra:
        raise ValidationError(
            "COMPOSE_MAPPING_MISMATCH",
            "the mapping must cover every column of source node "
            f"'{request.source}' exactly once",
            request.source,
            {"missing": missing, "extra": extra},
        )

    # Every referenced column must resolve on the upstream export relation
    # (for a filter export that is the passed-through input columns).
    up_export_columns = {
        c.name for c in up_relations[request.upstream.export].columns
    }
    unknown: list[str] = []
    for item in request.mapping:
        if (
            item.upstream_column not in up_export_columns
            and item.upstream_column not in unknown
        ):
            unknown.append(item.upstream_column)
    if unknown:
        raise ValidationError(
            "COMPOSE_COLUMN_NOT_FOUND",
            "mapped column(s) not found on upstream export node "
            f"'{request.upstream.export}': " + ", ".join(unknown),
            request.upstream.export,
            {"columns": unknown},
        )

    # --- introduced nodes: the upstream export and its dependency ancestors,
    # in the upstream request's original node order --------------------------
    keep = _ancestors(request.upstream.export, up_by_id)
    introduced = [node for node in request.upstream.nodes if node.id in keep]

    # --- id assignment: keep non-conflicting ids, rename the rest -----------
    # ``used`` is pre-seeded with every downstream id and every introduced
    # old id, so a freshly minted id can never collide with a node kept
    # as-is that is only processed later.
    downstream_ids = set(down_by_id)
    used = downstream_ids | {node.id for node in introduced}
    id_map: dict[str, str] = {}
    for node in introduced:
        if node.id not in downstream_ids:
            id_map[node.id] = node.id
        else:
            id_map[node.id] = _fresh_upstream_id(node.id, used)
            used.add(id_map[node.id])

    introduced_dicts: list[dict[str, object]] = []
    for node in introduced:
        data = serialize_node(node)
        data["id"] = id_map[node.id]
        if isinstance(node, (ProjectionNodeIn, FilterNodeIn)):
            data["input"] = id_map[node.input]
        elif isinstance(node, JoinNodeIn):
            data["left_input"] = id_map[node.left_input]
            data["right_input"] = id_map[node.right_input]
        introduced_dicts.append(data)

    # --- the replacement projection: original source id, original column
    # order, each column copying its mapped upstream export column ----------
    projection = {
        "id": request.source,
        "type": "projection",
        "input": id_map[request.upstream.export],
        "columns": [
            {"name": name, "copy": mapped[name]} for name in source_columns
        ],
    }

    # Downstream nodes keep their order with the source replaced in place;
    # the introduced upstream nodes are appended after them.
    composed: list[dict[str, object]] = []
    for node in request.downstream.nodes:
        if node.id == request.source:
            composed.append(projection)
        else:
            composed.append(serialize_node(node))
    composed.extend(introduced_dicts)

    if len(composed) > NODE_LIMIT:
        raise ValidationError(
            "COMPOSE_NODE_LIMIT",
            f"the composed graph would exceed the {NODE_LIMIT}-node limit "
            f"({len(request.downstream.nodes)} downstream nodes plus "
            f"{len(introduced)} introduced nodes make {len(composed)})",
            request.downstream.export,
            {"node_count": len(composed), "limit": NODE_LIMIT},
        )

    # Re-validate and re-review the composed graph under the original rules;
    # reasons in the review therefore use the composed graph's ids.
    re_payload = ReviewRequest.model_validate(
        {"nodes": composed, "export": request.downstream.export}
    )
    try:
        composed_review = review(re_payload)
    except ValidationError as exc:  # pragma: no cover - invariant by construction
        raise ValidationError(
            "COMPOSE_INVALID_RESULT",
            "internal error: the composed graph fails validation under the "
            "original rules: " + exc.message,
            request.downstream.export,
            {"underlying": exc.detail()},
        )

    return {
        "nodes": composed,
        "export": request.downstream.export,
        "source": request.source,
        "id_map": [
            {"from": old_id, "to": new_id} for old_id, new_id in id_map.items()
        ],
        "review": composed_review,
    }


# ---------------------------------------------------------------------------
# Small-sample preview of one export
# ---------------------------------------------------------------------------
#
# A preview materializes the export relation over caller-provided sample rows.
# The tables are plain column-aligned row lists (each row a dict keyed by the
# node's output column names); the provenance machinery above is reused for
# validation and review, but sample data never feeds the provenance logic and
# the request objects / shared branches are never mutated — every node's
# relation is materialized once, in topological order.


# JSON scalar: str, bool, number (int/float, excluding non-finite values) or
# null. ``bool`` is checked before numbers because ``isinstance(True, int)``.
def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, bool, int)) or (
        isinstance(value, float) and math.isfinite(value)
    )


def _scalar_equal(left: object, right: object) -> bool:
    """JSON-type-and-value equality.

    Numbers compare by their exact numeric value (``1`` equals ``1.0``, and
    integers beyond ``2**53`` still compare at full precision — no float
    coercion), booleans never equal numbers (``True`` is not ``1``), and
    values of different JSON kinds never match. ``None`` equals ``None``.
    """
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        # bool was already handled above, so numeric int/float compares here.
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return False


def _join_key(value: object) -> tuple[bool, object] | None:
    """Hashable key for the inner join; ``None`` keys never match.

    Numbers keep their original int/float value rather than being coerced to
    float: Python compares ints with floats by exact numeric value (``1``
    matches ``1.0`` but the integer 9007199254740993 does not match the float
    9007199254740992.0), integers of arbitrary size stay hashable, and equal
    int/float pairs share a hash so a dict keeps matching them. Coercing to
    float would instead collapse consecutive integers beyond ``2**53`` into
    one key and raise ``OverflowError`` on the largest JSON integers.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return (True, value)
    if isinstance(value, (int, float)):
        return (False, value)
    if isinstance(value, str):
        return (False, value)
    return None  # non-scalar values are rejected before materialization


def _reject_non_scalar_preview_constants(by_id: dict[str, object]) -> None:
    """The preview restricts projection constants to JSON scalars or null.

    This is a preview-only restriction: the other endpoints keep accepting the
    old constant range unchanged. The whole graph is checked, like the rest of
    the shared validation.
    """
    for nid, node in by_id.items():
        if isinstance(node, ProjectionNodeIn):
            for spec in node.columns:
                if spec.is_constant and not _is_scalar(spec.constant):
                    raise ValidationError(
                        "PREVIEW_NON_SCALAR",
                        "preview projection constants must be a JSON scalar "
                        f"or null; projection node '{nid}' column "
                        f"'{spec.name}' uses a non-scalar constant",
                        nid,
                        {"node_id": nid, "column": spec.name, "kind": "constant"},
                    )


def preview(
    request: PreviewRequest,
    extra_sensitive: frozenset[Origin] = frozenset(),
) -> dict[str, object]:
    """Run a small-sample dry run of the export over caller-supplied rows.

    The whole graph is validated under exactly the same rules as
    :func:`review`. The ``rows`` map must cover exactly the source nodes in the
    export's dependency closure (no missing, no extra — sources of unused
    branches must not be supplied), with every row object keyed by exactly the
    source's columns; the ``conditions`` map must cover exactly the closure's
    filter nodes, each with a key set equal to that filter's ``references`` and
    JSON scalar/null values; the predicates combine with AND. Cells and
    projection constants are likewise restricted to JSON scalars/null.

    When the export is blocked, the request is rejected (HTTP 400) carrying the
    review result only — no sample data is ever returned. Otherwise the export
    is materialized topologically (shared branches computed once, never
    mutated): projections copy or substitute constants, filters pass all
    columns through, joins are inner equi-joins (null keys never match,
    duplicate keys keep every combination). Join keys and filter constants
    compare numbers at their exact value, so integers of any size (including
    integers beyond ``2**53`` or the finite-float range) match only themselves
    and exact numeric float equals (``1`` still matches ``1.0``). Every source
    and intermediate relation must stay within :data:`PREVIEW_ROW_LIMIT` rows;
    any overflow or malformed input rejects the whole request without partial
    data.

    ``extra_sensitive`` is the shared sensitive-column registry: its entries
    add to the graph's in-graph ``sensitive`` flags before validation, so a
    preview stays blocked whenever either source of sensitivity applies.
    """
    by_id, order, relations = _evaluate_graph(request, extra_sensitive)

    # --- closure: export plus its dependency ancestors ----------------------
    keep = _ancestors(request.export, by_id)
    source_ids = sorted(
        nid for nid in keep if isinstance(by_id[nid], SourceNodeIn)
    )
    filter_ids = sorted(
        nid for nid in keep if isinstance(by_id[nid], FilterNodeIn)
    )

    # --- rows/conditions cover exactly the closure's sources/filters --------
    row_ids = set(request.rows)
    condition_ids = set(request.conditions)
    missing_sources = [nid for nid in source_ids if nid not in row_ids]
    extra_rows = sorted(row_ids.difference(source_ids))
    missing_filters = [nid for nid in filter_ids if nid not in condition_ids]
    extra_conditions = sorted(condition_ids.difference(filter_ids))
    if missing_sources or extra_rows:
        raise ValidationError(
            "PREVIEW_SOURCE_COVERAGE",
            "the rows map must provide data for exactly the source nodes "
            "reachable from the export node, neither more nor less",
            request.export,
            {"missing": missing_sources, "extra": extra_rows},
        )
    if missing_filters or extra_conditions:
        raise ValidationError(
            "PREVIEW_FILTER_COVERAGE",
            "the conditions map must provide predicates for exactly the "
            "filter nodes reachable from the export node, neither more nor "
            "less",
            request.export,
            {"missing": missing_filters, "extra": extra_conditions},
        )

    # Preview-only scalar restriction on projection constants (whole graph).
    _reject_non_scalar_preview_constants(by_id)

    # --- per-source rows: row cap, exactly the source's columns, scalar cells
    for nid in source_ids:
        node = by_id[nid]
        assert isinstance(node, SourceNodeIn)
        input_rows = request.rows[nid]
        expected = [c.name for c in node.columns]
        expected_set = set(expected)
        if len(input_rows) > PREVIEW_ROW_LIMIT:
            raise ValidationError(
                "PREVIEW_ROW_LIMIT",
                f"source node '{nid}' provides {len(input_rows)} sample rows; "
                f"the preview limit is {PREVIEW_ROW_LIMIT}",
                nid,
                {"node_id": nid, "row_count": len(input_rows),
                 "limit": PREVIEW_ROW_LIMIT},
            )
        for index, row in enumerate(input_rows):
            keys = set(row)
            missing = [name for name in expected if name not in keys]
            extra = sorted(keys.difference(expected_set))
            if missing or extra:
                raise ValidationError(
                    "PREVIEW_ROW_KEYS",
                    f"row {index} of source node '{nid}' must be keyed by "
                    "exactly the source's columns",
                    nid,
                    {"node_id": nid, "row_index": index,
                     "missing": missing, "extra": extra},
                )
            for name in expected:
                if not _is_scalar(row[name]):
                    raise ValidationError(
                        "PREVIEW_NON_SCALAR",
                        f"cell value of source node '{nid}' row {index} "
                        f"column '{name}' must be a JSON scalar or null",
                        nid,
                        {"node_id": nid, "row_index": index,
                         "column": name, "kind": "cell"},
                    )

    # --- per-filter conditions: key set == references, scalar values --------
    for nid in filter_ids:
        node = by_id[nid]
        assert isinstance(node, FilterNodeIn)
        expected = list(node.references)
        expected_set = set(expected)
        cond = request.conditions[nid]
        keys = set(cond)
        missing = [name for name in expected if name not in keys]
        extra = sorted(keys.difference(expected_set))
        if missing or extra:
            raise ValidationError(
                "PREVIEW_CONDITION_KEYS",
                f"conditions of filter node '{nid}' must be keyed by exactly "
                "the filter's references",
                nid,
                {"node_id": nid, "missing": missing, "extra": extra},
            )
        for name in expected:
            if not _is_scalar(cond[name]):
                raise ValidationError(
                    "PREVIEW_NON_SCALAR",
                    f"condition value of filter node '{nid}' column '{name}' "
                    "must be a JSON scalar or null",
                    nid,
                    {"node_id": nid, "column": name, "kind": "condition"},
                )

    # A blocked export must never expose sample data: return the review
    # reasons alone (HTTP 400), not columns/rows.
    review_result = _review_export(request.export, relations)
    if review_result["blocked"]:
        raise ValidationError(
            "PREVIEW_BLOCKED",
            "the export is blocked by sensitive content or row-level "
            "influence; sample data is not returned for blocked exports",
            request.export,
            {"review": review_result},
        )

    # --- materialize the closure once, in topological order -----------------
    tables: dict[str, list[dict[str, object]]] = {}
    for nid in order:
        if nid not in keep:
            continue
        node = by_id[nid]

        if isinstance(node, SourceNodeIn):
            # Preserve the supplied row order and the node's column order.
            table = [
                {name: row[name] for name in (c.name for c in node.columns)}
                for row in request.rows[nid]
            ]

        elif isinstance(node, ProjectionNodeIn):
            upstream = tables[node.input]
            table = []
            for row in upstream:
                out: dict[str, object] = {}
                for spec in node.columns:
                    if spec.is_constant:
                        out[spec.name] = spec.constant
                    else:
                        out[spec.name] = row[spec.copy_name]
                table.append(out)

        elif isinstance(node, FilterNodeIn):
            cond = request.conditions[nid]
            # Filter order is stable: survivors keep the upstream row order;
            # every reference must compare equal (AND), null matches null.
            table = [
                row
                for row in tables[node.input]
                if all(
                    _scalar_equal(row[name], cond[name]) for name in cond
                )
            ]

        else:  # JoinNodeIn — inner equi-join
            left_rows = tables[node.left_input]
            right_rows = tables[node.right_input]
            right_index: dict[object, list[dict[str, object]]] = defaultdict(list)
            for row in right_rows:
                key = _join_key(row[node.right_key])
                if key is not None:
                    right_index[key].append(row)

            table = []
            for left in left_rows:
                key = _join_key(left[node.left_key])
                if key is None:
                    continue
                # Outer left rows, inner right rows: the nested-loop order of
                # every (left, right) combination. Duplicate keys on either
                # side keep all combinations.
                for right in right_index.get(key, ()):
                    combined: dict[str, object] = {}
                    for out_spec in node.outputs:
                        source_row = left if out_spec.side == "left" else right
                        source_col = out_spec.column
                        combined[out_spec.alias] = source_row[source_col]
                    table.append(combined)

        if len(table) > PREVIEW_ROW_LIMIT:
            raise ValidationError(
                "PREVIEW_ROW_LIMIT",
                f"intermediate node '{nid}' yields {len(table)} sample rows; "
                f"the preview limit is {PREVIEW_ROW_LIMIT}",
                nid,
                {"node_id": nid, "row_count": len(table),
                 "limit": PREVIEW_ROW_LIMIT},
            )
        tables[nid] = table

    export_table = tables[request.export]
    export_columns = [c.name for c in relations[request.export].columns]
    return {
        "export": request.export,
        "columns": export_columns,
        "rows": [[row[name] for name in export_columns] for row in export_table],
        "review": review_result,
    }


# ---------------------------------------------------------------------------
# Comparing a baseline graph against a candidate graph
# ---------------------------------------------------------------------------
#
# Each side is a whole, independently validated review-shaped graph. The two
# exports are compared column by column, pairing columns by name; an origin is
# a (source node id, source column name) pair, so an identically named origin
# in the two graphs denotes the same source. For every paired column the diff
# lists the content and row origins that were added or removed by the
# candidate. A side missing the column carries a ``null`` verdict and its
# reason sets are treated as empty for the comparison. Neither input graph is
# ever mutated and no revised graph is ever produced.


def _reason_set(reasons: list[dict[str, str]]) -> frozenset[Origin]:
    """Parse a review's reason list back into a set of origin tuples."""
    return frozenset((item["node_id"], item["column"]) for item in reasons)


def _column_origin_sets(
    review_result: dict[str, object],
) -> dict[str, tuple[bool, frozenset[Origin], frozenset[Origin]]]:
    """Map export column name to (blocked, content origins, row origins)."""
    columns: dict[str, tuple[bool, frozenset[Origin], frozenset[Origin]]] = {}
    for col in review_result["columns"]:
        columns[col["name"]] = (
            bool(col["blocked"]),
            _reason_set(col["content_reasons"]),
            _reason_set(col["row_reasons"]),
        )
    return columns


def _added_removed(
    before: frozenset[Origin], after: frozenset[Origin]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Sorted, de-duplicated reason lists for the candidate minus / plus set."""
    return _origin_view(after - before), _origin_view(before - after)


def compare(request: CompareRequest) -> dict[str, object]:
    """Diff the export reviews of a baseline graph and a candidate graph.

    Both graphs are validated independently under exactly the same rules as
    :func:`review`; an invalid graph on either side rejects the whole request
    with that side's original error format, and no partial diff is returned.

    Export columns are paired by name. Common and candidate-only columns are
    emitted in the candidate's column order; columns the candidate deleted
    follow in the baseline's column order. Each entry carries both sides'
    ``blocked`` verdicts (``null`` on the side the column is absent, whose
    reasons count as the empty set) and, split by content and row influence,
    the sorted de-duplicated origins added and removed by the candidate.

    ``regression`` marks newly introduced risk only: it is true when a blocked
    column is newly added, or a column common to both exports gains any kind
    of reason — including an origin that moved from content to row influence
    on the same source (the new row reason counts even though the column was
    blocked before). A deleted blocked column and a newly added safe column
    are not regressions. The input graphs are never modified and no revised
    graph is produced.
    """
    _b_by_id, _b_order, baseline_relations = _evaluate_graph(request.baseline)
    _c_by_id, _c_order, candidate_relations = _evaluate_graph(request.candidate)

    baseline_review = _review_export(request.baseline.export, baseline_relations)
    candidate_review = _review_export(request.candidate.export, candidate_relations)

    baseline_columns = _column_origin_sets(baseline_review)
    candidate_columns = _column_origin_sets(candidate_review)

    candidate_order = [col["name"] for col in candidate_review["columns"]]
    baseline_order = [col["name"] for col in baseline_review["columns"]]

    # Common and new columns follow the candidate order; deleted columns then
    # follow the baseline order.
    paired_order = candidate_order + [
        name for name in baseline_order if name not in candidate_columns
    ]

    columns_out: list[dict[str, object]] = []
    regression = False
    for name in paired_order:
        baseline_entry = baseline_columns.get(name)
        candidate_entry = candidate_columns.get(name)

        if baseline_entry is None:
            baseline_blocked: bool | None = None
            baseline_content: frozenset[Origin] = frozenset()
            baseline_rows: frozenset[Origin] = frozenset()
        else:
            baseline_blocked, baseline_content, baseline_rows = baseline_entry

        if candidate_entry is None:
            candidate_blocked: bool | None = None
            candidate_content: frozenset[Origin] = frozenset()
            candidate_rows: frozenset[Origin] = frozenset()
        else:
            candidate_blocked, candidate_content, candidate_rows = candidate_entry

        content_added, content_removed = _added_removed(
            baseline_content, candidate_content
        )
        row_added, row_removed = _added_removed(baseline_rows, candidate_rows)

        if candidate_entry is not None and baseline_entry is None:
            # A newly added blocked column is newly introduced risk; a newly
            # added safe column is not.
            if candidate_blocked:
                regression = True
        elif baseline_entry is not None and candidate_entry is not None:
            # Gaining any reason kind on a common column is a regression; an
            # origin moving from content to row influence gains a row reason
            # even though the column was blocked on both sides.
            if content_added or row_added:
                regression = True
        # Deleted columns can never add risk, regardless of their old verdict.

        columns_out.append(
            {
                "name": name,
                "baseline_blocked": baseline_blocked,
                "candidate_blocked": candidate_blocked,
                "content_added_reasons": content_added,
                "content_removed_reasons": content_removed,
                "row_added_reasons": row_added,
                "row_removed_reasons": row_removed,
            }
        )

    return {
        "baseline": baseline_review,
        "candidate": candidate_review,
        "regression": regression,
        "columns": columns_out,
    }
