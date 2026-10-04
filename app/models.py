from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Request models reject unknown fields so typos cannot be silently ignored."""

    model_config = ConfigDict(extra="forbid")


class SourceColumnIn(StrictModel):
    name: str = Field(min_length=1)
    sensitive: bool = False


class SourceNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["source"]
    columns: list[SourceColumnIn] = Field(min_length=1)


class ProjectionColumnIn(StrictModel):
    """An output column either copies an upstream column or is replaced by a constant.

    ``constant`` may legitistically be JSON ``null``, so presence is checked with
    ``model_fields_set`` rather than comparing against the default value.
    The field is aliased to ``copy`` to avoid shadowing ``BaseModel.copy``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    copy_: str | None = Field(default=None, alias="copy", min_length=1)
    constant: object | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ProjectionColumnIn":
        has_copy = "copy_" in self.model_fields_set
        has_constant = "constant" in self.model_fields_set
        if has_copy == has_constant:
            raise ValueError(
                "each projection column must specify exactly one of 'copy' or 'constant'"
            )
        return self

    @property
    def copy_name(self) -> str | None:
        return self.copy_

    @property
    def is_constant(self) -> bool:
        return "constant" in self.model_fields_set


class ProjectionNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["projection"]
    input: str = Field(min_length=1)
    columns: list[ProjectionColumnIn] = Field(min_length=1)


class FilterNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["filter"]
    input: str = Field(min_length=1)
    references: list[str] = Field(min_length=1)


class JoinOutputIn(StrictModel):
    alias: str = Field(min_length=1)
    side: Literal["left", "right"]
    column: str = Field(min_length=1)


class JoinNodeIn(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["join"]
    left_input: str = Field(min_length=1)
    right_input: str = Field(min_length=1)
    left_key: str = Field(min_length=1)
    right_key: str = Field(min_length=1)
    outputs: list[JoinOutputIn] = Field(min_length=1)


NodeIn = Annotated[
    Union[SourceNodeIn, ProjectionNodeIn, FilterNodeIn, JoinNodeIn],
    Field(discriminator="type"),
]


class ReviewRequest(StrictModel):
    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)


class ReviseRequest(StrictModel):
    """A review request plus the export columns the caller allows nulling.

    The whole graph is still validated by the same rules as ``ReviewRequest``;
    the replacement list may be empty (distinctness is then vacuous) and every
    listed name must be non-empty. Existence of each name on the export node is
    checked in the engine together with the rest of the graph.
    """

    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)
    allowed_replacements: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _replacement_names_distinct(self) -> "ReviseRequest":
        seen: set[str] = set()
        for name in self.allowed_replacements:
            if name in seen:
                raise ValueError(
                    f"replacement column '{name}' is listed more than once"
                )
            seen.add(name)
        return self


class ColumnRenameIn(StrictModel):
    """One source-column rename, resolved against the original graph.

    All renames take effect simultaneously, so swaps are legal as long as the
    source ends with distinct column names. The JSON field is ``from`` (a
    Python keyword), aliased onto ``from_`` like ``copy`` is for projections.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)

    @property
    def old_name(self) -> str:
        return self.from_

    @property
    def new_name(self) -> str:
        return self.to


class RenameRequest(StrictModel):
    """A review request plus a source node id and a non-empty rename list.

    The whole graph is validated by the same rules as ``ReviewRequest`` before
    anything is renamed. The original column names must be non-empty, mutually
    distinct and exist on the named source node; every new name must be
    non-empty and the source's final column names must be distinct. Existence
    and final-name conflicts are checked in the engine together with the rest
    of the graph, so schema problems alone are rejected as 422.
    """

    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)
    source: str = Field(min_length=1)
    renames: list[ColumnRenameIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _old_names_distinct(self) -> "RenameRequest":
        seen: set[str] = set()
        for item in self.renames:
            if item.old_name in seen:
                raise ValueError(
                    f"rename for source column '{item.old_name}' is listed more "
                    "than once"
                )
            seen.add(item.old_name)
        return self


class ColumnMappingIn(StrictModel):
    """One downstream source column fed by an upstream export column.

    ``column`` names a column of the replaced downstream source node; ``from``
    names the column of the upstream export node whose values take its place
    (a Python keyword-free alias is not needed for ``column``, while ``from``
    is aliased onto ``from_`` like ``copy`` is for projections). Several
    source columns may legitimately draw from the same upstream column.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    column: str = Field(min_length=1)
    from_: str = Field(alias="from", min_length=1)

    @property
    def upstream_column(self) -> str:
        return self.from_


class ComposeRequest(StrictModel):
    """Two review-shaped graphs plus the splice point and column mapping.

    ``downstream`` and ``upstream`` are each a whole graph with its export
    node, validated by exactly the same rules as ``ReviewRequest``. ``source``
    names the downstream source node to replace; ``mapping`` lists, for every
    column of that source, the upstream export column feeding it. The mapping
    must cover every source column exactly once (duplicates are a schema
    error); coverage, extra entries and the existence of the referenced
    upstream columns are checked in the engine together with the graphs.
    """

    downstream: ReviewRequest
    upstream: ReviewRequest
    source: str = Field(min_length=1)
    mapping: list[ColumnMappingIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _mapped_columns_distinct(self) -> "ComposeRequest":
        seen: set[str] = set()
        for item in self.mapping:
            if item.column in seen:
                raise ValueError(
                    f"mapping for source column '{item.column}' is listed more "
                    "than once"
                )
            seen.add(item.column)
        return self


class CompareRequest(StrictModel):
    """Two whole review-shaped graphs to diff for newly introduced export risk.

    ``baseline`` and ``candidate`` each carry ``nodes`` + ``export`` and are
    validated independently under exactly the same rules as
    :class:`ReviewRequest` (duplicate ids, column conflicts, invalid
    references and cycles included): an invalid graph on either side rejects
    the whole request. Export columns are paired by name; an origin is the
    ``(node_id, column)`` pair, so an identically named origin in the two
    graphs is the same source even when the surrounding graph differs.
    """

    baseline: ReviewRequest
    candidate: ReviewRequest


class PreviewRequest(StrictModel):
    """A small-sample dry run of one export over caller-provided source rows.

    Besides a review-shaped graph (``nodes`` + ``export``), the caller gives
    ``rows`` keyed by source node id (ordered row arrays) and ``conditions``
    keyed by filter node id (column name -> equality constant). Both maps
    must cover *exactly* the source/filter nodes among the export's dependency
    ancestors; every row object must carry exactly that source's columns and
    every condition object exactly the filter's ``references``. Coverage and
    key-set mismatches, the scalar-only value restriction and the 200-row cap
    are engine-level rejections (HTTP 400); structural problems such as wrong
    field types or unknown fields stay HTTP 422.
    """

    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)
    rows: dict[str, list[dict[str, object]]]
    conditions: dict[str, dict[str, object]] = Field(default_factory=dict)


class GraphRegisterRequest(ReviewRequest):
    """A whole, self-contained graph registered for later registry-driven review.

    The shape is identical to :class:`ReviewRequest` (``nodes`` + ``export``)
    and the whole graph is validated by exactly the same rules at registration
    time and on every registry replacement afterwards.
    """


class GraphPreviewRequest(StrictModel):
    """A preview of a previously registered graph: sample inputs only.

    The graph itself is referenced by its registered id (in the URL); the body
    carries just ``rows`` and ``conditions``, with the same shapes and rules as
    :class:`PreviewRequest`. Coverage is still checked against the stored
    graph's closure, so a wrong structural type or missing rows stays a 422.
    """

    rows: dict[str, list[dict[str, object]]]
    conditions: dict[str, dict[str, object]] = Field(default_factory=dict)


class SensitiveColumnEntryIn(StrictModel):
    """One shared sensitive-column entry, identified by source id and column.

    Entries are global: they need not name a source any registered graph
    declares. ``source`` is non-empty; ``column`` is non-empty; two entries
    equal on both fields are duplicates and rejected at the schema level.
    """

    source: str = Field(min_length=1)
    column: str = Field(min_length=1)


class SensitiveColumnsReplaceRequest(StrictModel):
    """Whole-list replacement of the shared sensitive-column registry.

    ``entries`` replaces the registry wholesale (an empty list clears it) and
    ``expected_generation`` must equal the current generation; a stale value
    rejects the request as HTTP 409 without changing anything. On success the
    generation advances by one and every registered graph is re-reviewed.
    """

    entries: list[SensitiveColumnEntryIn] = Field(default_factory=list)
    expected_generation: int = Field(ge=0)

    @model_validator(mode="after")
    def _entries_distinct(self) -> "SensitiveColumnsReplaceRequest":
        seen: set[tuple[str, str]] = set()
        for item in self.entries:
            key = (item.source, item.column)
            if key in seen:
                raise ValueError(
                    f"sensitive-column entry '{item.source}/{item.column}' is "
                    "listed more than once"
                )
            seen.add(key)
        return self


class PackageReleaseRequest(StrictModel):
    """Release a sealed export package under the current registry generation.

    ``expected_generation`` must equal the current registry generation; a
    stale value rejects the release as HTTP 409 without recording anything.
    """

    expected_generation: int = Field(ge=0)


class PackagePurgeColumnIn(StrictModel):
    """One source column whose dependent sealed samples must be purged.

    The column is identified by its source node id (``source``) and its
    name; both are non-empty. The pair need not name a source or column any
    registered graph declares — unmatched sources simply purge nothing.
    """

    source: str = Field(min_length=1)
    column: str = Field(min_length=1)


class PackagePurgeRequest(StrictModel):
    """Purge every sealed package whose export depends on a listed column.

    ``columns`` is a non-empty list of ``(source, column)`` pairs; the pairs
    must be mutually distinct (duplicates are a schema error). The dependency
    decision follows the original content-provenance and row-influence rules
    with sensitivity marks ignored entirely.
    """

    columns: list[PackagePurgeColumnIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _columns_distinct(self) -> "PackagePurgeRequest":
        seen: set[tuple[str, str]] = set()
        for item in self.columns:
            key = (item.source, item.column)
            if key in seen:
                raise ValueError(
                    f"purge column '{item.source}/{item.column}' is listed "
                    "more than once"
                )
            seen.add(key)
        return self


class PruneRequest(StrictModel):
    """A review request plus the export columns the caller wants to keep.

    ``columns`` selects a non-empty subset of the export node's columns. The
    names must be non-empty and distinct (their order never matters: the result
    always follows the export node's original column order). The whole graph is
    still validated by the same rules as ``ReviewRequest``; existence of every
    selected name on the export node is checked in the engine.
    """

    nodes: list[NodeIn] = Field(min_length=1, max_length=60)
    export: str = Field(min_length=1)
    columns: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)

    @model_validator(mode="after")
    def _column_names_distinct(self) -> "PruneRequest":
        seen: set[str] = set()
        for name in self.columns:
            if name in seen:
                raise ValueError(
                    f"selected column '{name}' is listed more than once"
                )
            seen.add(name)
        return self
