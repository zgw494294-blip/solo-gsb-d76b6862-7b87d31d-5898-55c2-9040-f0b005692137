from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import registry
from .engine import (
    ValidationError,
    compare,
    compose,
    preview,
    prune,
    rename,
    review,
    revise,
)
from .models import (
    CompareRequest,
    ComposeRequest,
    GraphPreviewRequest,
    GraphRegisterRequest,
    PackagePurgeRequest,
    PackageReleaseRequest,
    PreviewRequest,
    PruneRequest,
    RenameRequest,
    ReviewRequest,
    ReviseRequest,
    SensitiveColumnsReplaceRequest,
)
from .storage import get_store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Open (and, if needed, create) the SQLite database eagerly so a missing or
    # unwritable volume is visible at startup; the location is RISK_API_DB.
    get_store()
    yield


app = FastAPI(
    title="Local Data Export Risk Review API",
    description=(
        "Reviews every output column of an export node for sensitive content "
        "provenance and sensitive row-level influence, can revise a blocked "
        "graph into a re-submittable, review-safe export, can prune a graph "
        "down to a chosen subset of export columns, can batch-rename "
        "columns of a source node while keeping every in-graph reference "
        "pointing at the same original columns, can compose an upstream "
        "graph's export into a downstream graph's source position, yielding "
        "one reviewable combined graph, can run a small-sample preview "
        "of an export over caller-provided source rows and filter "
        "conditions, returning sample data only when the export passes "
        "review, and can diff a baseline graph against a candidate graph "
        "to report the export risks the candidate newly introduces. "
        "Graphs can additionally be registered by id and reviewed/previewed "
        "against a shared, versioned sensitive-column registry that is ORed "
        "with every graph's in-graph sensitive marks. A registered graph's "
        "passing small-sample result can be sealed into a pending-release "
        "export package, released under the current registry generation and "
        "claimed afterwards — release and claim always serve the sealed "
        "data, never a different batch. Sealed samples can finally be "
        "purged by source column: every live package whose export depends "
        "on a listed column (by the original content and row-influence "
        "rules, ignoring sensitivity marks) loses its stored rows and "
        "release record and stays purged for good — release and claim then "
        "answer 410."
    ),
    version="1.9.0",
    lifespan=lifespan,
)


@app.exception_handler(ValidationError)
async def _validation_error_handler(_request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": exc.detail()})


@app.exception_handler(Exception)
async def _unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    # Never leak stack traces from a local review service.
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL", "message": str(exc)}},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/review")
async def review_export(request: ReviewRequest) -> dict[str, object]:
    # Legacy review stays driven by in-graph sensitive marks only.
    return review(request)


@app.post("/api/revise")
async def revise_export(request: ReviseRequest) -> dict[str, object]:
    return revise(request)


@app.post("/api/prune")
async def prune_export(request: PruneRequest) -> dict[str, object]:
    return prune(request)


@app.post("/api/rename")
async def rename_source_columns(request: RenameRequest) -> dict[str, object]:
    return rename(request)


@app.post("/api/compose")
async def compose_graphs(request: ComposeRequest) -> dict[str, object]:
    return compose(request)


@app.post("/api/preview")
async def preview_export(request: PreviewRequest) -> dict[str, object]:
    # Legacy preview stays gated on in-graph marks only.
    return preview(request)


@app.post("/api/compare")
async def compare_exports(request: CompareRequest) -> dict[str, object]:
    return compare(request)


# ---------------------------------------------------------------------------
# Registered graphs and the shared sensitive-column registry
# ---------------------------------------------------------------------------


@app.post("/api/graphs", status_code=201)
async def register_graph(request: GraphRegisterRequest) -> dict[str, object]:
    # Store the original submission verbatim (FastAPI already parsed and
    # schema-validated it); the engine re-runs the whole-graph validation.
    raw_nodes = [
        node.model_dump(by_alias=True, exclude_unset=True)
        for node in request.nodes  # type: ignore[attr-defined]
    ]
    return registry.register_graph(raw_nodes, request.export)


@app.get("/api/graphs/{graph_id}")
async def read_graph(graph_id: str) -> object:
    try:
        return registry.read_graph(graph_id)
    except registry.GraphNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "GRAPH_NOT_FOUND",
                               "message": f"no registered graph with id '{graph_id}'"}},
        )


@app.post("/api/graphs/{graph_id}/preview")
async def preview_registered_graph(graph_id: str, request: GraphPreviewRequest) -> object:
    try:
        return registry.preview_graph(graph_id, request.rows, request.conditions)
    except registry.GraphNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "GRAPH_NOT_FOUND",
                               "message": f"no registered graph with id '{graph_id}'"}},
        )


@app.post("/api/graphs/{graph_id}/packages", status_code=201)
async def seal_graph_package(graph_id: str, request: GraphPreviewRequest) -> object:
    try:
        return registry.seal_package(graph_id, request.rows, request.conditions)
    except registry.GraphNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "GRAPH_NOT_FOUND",
                               "message": f"no registered graph with id '{graph_id}'"}},
        )


@app.post("/api/packages/{package_id}/release")
async def release_sealed_package(
    package_id: str, request: PackageReleaseRequest
) -> object:
    try:
        return registry.release_package(package_id, request.expected_generation)
    except registry.PackageNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "PACKAGE_NOT_FOUND",
                               "message": f"no sealed package with id '{package_id}'"}},
        )
    except registry.PackagePurged as exc:
        return JSONResponse(
            status_code=410,
            content={"error": {"code": "PACKAGE_PURGED", "message": str(exc)}},
        )
    except registry.StaleGeneration as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "GENERATION_CONFLICT",
                    "message": str(exc),
                    "expected_generation": exc.expected,
                    "current_generation": exc.current,
                }
            },
        )


@app.get("/api/packages/{package_id}")
async def claim_sealed_package(package_id: str) -> object:
    try:
        return registry.claim_package(package_id)
    except registry.PackageNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "PACKAGE_NOT_FOUND",
                               "message": f"no sealed package with id '{package_id}'"}},
        )
    except registry.PackagePurged as exc:
        return JSONResponse(
            status_code=410,
            content={"error": {"code": "PACKAGE_PURGED", "message": str(exc)}},
        )
    except registry.PackageNotReleased as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "PACKAGE_NOT_RELEASED",
                    "message": str(exc),
                    "release_generation": exc.release_generation,
                    "current_generation": exc.current,
                }
            },
        )


@app.post("/api/packages/purge")
async def purge_sealed_packages(request: PackagePurgeRequest) -> object:
    columns = [(item.source, item.column) for item in request.columns]
    return registry.purge_packages(columns)


@app.get("/api/sensitive-columns")
async def read_sensitive_columns() -> dict[str, object]:
    return registry.get_registry()


@app.put("/api/sensitive-columns")
async def replace_sensitive_columns(
    request: SensitiveColumnsReplaceRequest,
) -> object:
    entries = [(item.source, item.column) for item in request.entries]
    try:
        return registry.replace_registry(entries, request.expected_generation)
    except registry.StaleGeneration as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "GENERATION_CONFLICT",
                    "message": str(exc),
                    "expected_generation": exc.expected,
                    "current_generation": exc.current,
                }
            },
        )
