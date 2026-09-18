"""FastAPI application: immutable config versions + batch flag evaluation."""

from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db as db_mod
from .comparison import compare_versions
from .evaluator import FlagEvaluator, FlagNotFound
from .preview import build_evaluation_preview
from .schemas import (
    ApprovalVoteRequest,
    BatchEvalRequest,
    BatchEvalResponse,
    ChangeApprovalCreateRequest,
    ChangeApprovalResponse,
    ConfigComparisonRequest,
    ConfigComparisonResponse,
    ConfigResponse,
    EnvironmentEvalRequest,
    EnvironmentEvalResponse,
    EnvironmentHistoryEntry,
    EnvironmentHistoryResponse,
    EnvironmentResponse,
    EnvironmentUpsertRequest,
    EvaluationPreviewRequest,
    EvaluationPreviewResponse,
    PublishConfigRequest,
    PublishResponse,
    VersionInfo,
)
from .validation import ConfigValidationError, validate_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db_mod.connect()
    try:
        db_mod.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="Feature Flag Decision Service",
    version="1.0.0",
    description=(
        "Versioned feature-flag evaluation with prerequisites, reusable "
        "audience segments, ordered attribute rules and stable user_id "
        "percentage rollouts."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling — uniform envelope: {"error": {code, message, errors?}}
# ---------------------------------------------------------------------------


def _error_response(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    return JSONResponse(status_code=status_code, content={"error": detail})


def _json_safe(value: Any) -> Any:
    """Recursively coerce a value into JSON-encodable form.

    Pydantic error contexts may carry non-JSON values (notably the original
    exception object raised by a model validator); such values are rendered as
    stable strings instead of leaking into ``JSONResponse`` and triggering a
    serialization failure.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        # Preserve order: pydantic locations are ordered tuples.
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        try:
            items = sorted(value, key=lambda item: str(item))
        except TypeError:
            items = list(value)
        return [_json_safe(item) for item in items]
    # Exceptions and any other non-JSON value: ``str`` gives a stable message
    # (e.g. ``duplicate subject_id 's1'``) without object identity/addresses.
    return str(value)


def _sanitize_validation_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """Make Pydantic's error list JSON-safe while keeping its shape.

    Every entry retains ``loc``/``type``/``msg``/``input`` (plus any extra
    keys such as ``ctx`` or ``url``); non-JSON values nested anywhere inside
    are recursively turned into stable strings.
    """
    sanitized: list[dict[str, Any]] = []
    for error in errors:
        if isinstance(error, dict):
            entry = {str(key): _json_safe(value) for key, value in error.items()}
        else:
            entry = {"msg": _json_safe(error)}
        entry.setdefault("loc", [])
        entry.setdefault("type", "value_error")
        entry.setdefault("msg", "Invalid request")
        entry.setdefault("input", None)
        sanitized.append(entry)
    return sanitized


@app.exception_handler(ConfigValidationError)
async def config_validation_handler(request: Request, exc: ConfigValidationError) -> JSONResponse:
    return _error_response(
        422,
        "invalid_config",
        "configuration validation failed",
        errors=exc.errors,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(
        422,
        "invalid_request",
        "request validation failed",
        errors=_sanitize_validation_errors(exc.errors()),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------


@app.post("/api/v1/configs", response_model=PublishResponse, status_code=201)
def publish_config(body: PublishConfigRequest) -> Any:
    config = body.model_dump()
    # Semantic validation: cycles, percentage bounds, dangling prerequisites.
    validate_config(config)

    conn = db_mod.connect()
    try:
        created_at = db_mod.insert_version(conn, body.version, config)
        if created_at is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "version_exists",
                    "message": f"version {body.version!r} already exists; versions are immutable",
                },
            )
        return PublishResponse(
            version=body.version, flag_count=len(body.flags), created_at=created_at
        )
    finally:
        conn.close()


@app.get("/api/v1/configs", response_model=list[VersionInfo])
def list_configs() -> Any:
    conn = db_mod.connect()
    try:
        return [VersionInfo(**row) for row in db_mod.list_versions(conn)]
    finally:
        conn.close()


@app.get("/api/v1/configs/{version}", response_model=ConfigResponse)
def get_config(version: str) -> Any:
    conn = db_mod.connect()
    try:
        row = db_mod.get_version(conn, version)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {version!r} not found",
                },
            )
        return ConfigResponse(**row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Version comparison (impact analysis)
# ---------------------------------------------------------------------------


@app.post("/api/v1/config-comparisons", response_model=ConfigComparisonResponse)
def compare_configs(body: ConfigComparisonRequest) -> Any:
    conn = db_mod.connect()
    try:
        from_row = db_mod.get_version(conn, body.from_version)
        if from_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.from_version!r} not found",
                },
            )
        to_row = db_mod.get_version(conn, body.to_version)
        if to_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.to_version!r} not found",
                },
            )
    finally:
        conn.close()

    analysis = compare_versions(
        from_row["config"].get("flags", {}),
        to_row["config"].get("flags", {}),
    )
    return ConfigComparisonResponse(
        from_version=body.from_version,
        to_version=body.to_version,
        **analysis,
    )


# ---------------------------------------------------------------------------
# Evaluation preview (per-subject decision diff between two versions)
# ---------------------------------------------------------------------------


@app.post("/api/v1/evaluation-previews", response_model=EvaluationPreviewResponse)
def evaluation_preview(body: EvaluationPreviewRequest) -> Any:
    conn = db_mod.connect()
    try:
        # Both versions are resolved before any evaluation runs, so a
        # missing version yields 404 with no partial preview.
        from_row = db_mod.get_version(conn, body.from_version)
        if from_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.from_version!r} not found",
                },
            )
        to_row = db_mod.get_version(conn, body.to_version)
        if to_row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.to_version!r} not found",
                },
            )
    finally:
        conn.close()

    preview = build_evaluation_preview(
        from_row["config"],
        to_row["config"],
        body.flags,
        [subject.model_dump() for subject in body.subjects],
    )
    return EvaluationPreviewResponse(
        from_version=body.from_version,
        to_version=body.to_version,
        changed=preview["changed"],
        summary=preview["summary"],
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _evaluate_flags(
    config: dict[str, Any],
    flag_names: list[str],
    user_id: Any,
    raw_attributes: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Evaluate a batch of flags against one pinned config document.

    Returns ``(context, results, missing_flags)``. One evaluator serves the
    whole request, so audience segments referenced by several flags are
    computed once and reused.
    """
    evaluator = FlagEvaluator(
        config.get("flags", {}), segments=config.get("segments")
    )

    attributes = dict(raw_attributes)
    if user_id is not None and "user_id" not in attributes:
        attributes["user_id"] = user_id

    results: list[dict[str, Any]] = []
    missing: list[str] = []
    for flag_name in flag_names:
        try:
            results.append(evaluator.evaluate(flag_name, attributes=attributes))
        except FlagNotFound:
            missing.append(flag_name)
            results.append(
                {
                    "flag": flag_name,
                    "value": False,
                    "hit": False,
                    "reason": "flag_not_found",
                    "rule_id": None,
                    "rollout": None,
                    "chain": [],
                }
            )

    return dict(attributes), results, missing


@app.post("/api/v1/evaluate", response_model=BatchEvalResponse)
def batch_evaluate(body: BatchEvalRequest) -> Any:
    conn = db_mod.connect()
    try:
        row = db_mod.get_version(conn, body.version)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {body.version!r} not found",
                },
            )
    finally:
        conn.close()

    context, results, missing = _evaluate_flags(
        row["config"], body.flags, body.user_id, body.attributes
    )
    return BatchEvalResponse(
        version=body.version, context=context, results=results, missing_flags=missing
    )


# ---------------------------------------------------------------------------
# Environment pointers
# ---------------------------------------------------------------------------


@app.put(
    "/api/v1/environments/{name}",
    response_model=EnvironmentResponse,
    status_code=200,
)
def put_environment(name: str, body: EnvironmentUpsertRequest) -> Any:
    conn = db_mod.connect()
    try:
        try:
            env, created = db_mod.upsert_environment(
                conn, name, body.version, body.expected_revision
            )
        except db_mod.StaleRevision as exc:
            detail: dict[str, Any] = {
                "code": "stale_revision",
                "message": str(exc),
            }
            if exc.current is not None:
                detail["current_revision"] = exc.current
            raise HTTPException(status_code=409, detail=detail)
        except db_mod.VersionNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {exc.version!r} not found",
                },
            )
    finally:
        conn.close()

    response = EnvironmentResponse(**env)
    # Creation returns 201, successful updates 200.
    if created:
        return JSONResponse(status_code=201, content=response.model_dump())
    return response


@app.get("/api/v1/environments/{name}", response_model=EnvironmentResponse)
def get_environment(name: str) -> Any:
    conn = db_mod.connect()
    try:
        env = db_mod.get_environment(conn, name)
        if env is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "environment_not_found",
                    "message": f"environment {name!r} not found",
                },
            )
        return EnvironmentResponse(**env)
    finally:
        conn.close()


@app.get(
    "/api/v1/environments/{name}/history",
    response_model=EnvironmentHistoryResponse,
)
def get_environment_history(
    name: str,
    from_revision: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> Any:
    conn = db_mod.connect()
    try:
        # A 404 must win over an empty page: only an existing environment
        # gets a (possibly filtered) history listing.
        env = db_mod.get_environment(conn, name)
        if env is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "environment_not_found",
                    "message": f"environment {name!r} not found",
                },
            )
        entries = db_mod.list_environment_history(
            conn, name, from_revision=from_revision, limit=limit
        )
        return EnvironmentHistoryResponse(
            name=name,
            history=[EnvironmentHistoryEntry(**entry) for entry in entries],
        )
    finally:
        conn.close()


@app.post(
    "/api/v1/environments/{name}/evaluate",
    response_model=EnvironmentEvalResponse,
)
def evaluate_environment(name: str, body: EnvironmentEvalRequest) -> Any:
    conn = db_mod.connect()
    try:
        # One read -> one consistent (pointer, version) snapshot for the
        # whole request; the pointer cannot change halfway through.
        snapshot = db_mod.get_environment_snapshot(conn, name)
        if snapshot is None:
            # Distinguish "environment missing" from "dangling pointer":
            # foreign keys make the latter unreachable from the API.
            env = db_mod.get_environment(conn, name)
            if env is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "environment_not_found",
                        "message": f"environment {name!r} not found",
                    },
                )
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "version_not_found",
                    "message": f"config version {env['version']!r} not found",
                },
            )
    finally:
        conn.close()

    context, results, missing = _evaluate_flags(
        snapshot["config"], body.flags, body.user_id, body.attributes
    )
    return EnvironmentEvalResponse(
        name=snapshot["name"],
        version=snapshot["version"],
        revision=snapshot["revision"],
        context=context,
        results=results,
        missing_flags=missing,
    )


# ---------------------------------------------------------------------------
# Change approvals (review-then-execute pointer moves)
# ---------------------------------------------------------------------------


def _approval_error(exc: Exception) -> HTTPException:
    """Translate approval storage exceptions into the error envelope."""
    if isinstance(exc, db_mod.EnvironmentNotFound):
        return HTTPException(
            status_code=404,
            detail={"code": "environment_not_found", "message": str(exc)},
        )
    if isinstance(exc, db_mod.VersionNotFound):
        return HTTPException(
            status_code=404,
            detail={"code": "version_not_found", "message": str(exc)},
        )
    if isinstance(exc, db_mod.ApprovalNotFound):
        return HTTPException(
            status_code=404,
            detail={"code": "approval_not_found", "message": str(exc)},
        )
    if isinstance(exc, db_mod.StaleRevision):
        detail: dict[str, Any] = {"code": "stale_revision", "message": str(exc)}
        if exc.current is not None:
            detail["current_revision"] = exc.current
        return HTTPException(status_code=409, detail=detail)
    if isinstance(exc, db_mod.NoChange):
        return HTTPException(
            status_code=409,
            detail={"code": "no_change", "message": str(exc)},
        )
    if isinstance(exc, db_mod.ApprovalNotPending):
        return HTTPException(
            status_code=409,
            detail={
                "code": "approval_closed",
                "message": str(exc),
                "status": exc.status,
            },
        )
    if isinstance(exc, db_mod.SelfApproval):
        return HTTPException(
            status_code=409,
            detail={"code": "self_approval", "message": str(exc)},
        )
    if isinstance(exc, db_mod.DuplicateVote):
        return HTTPException(
            status_code=409,
            detail={"code": "duplicate_vote", "message": str(exc)},
        )
    if isinstance(exc, db_mod.ApprovalsNotMet):
        return HTTPException(
            status_code=409,
            detail={
                "code": "approvals_not_met",
                "message": str(exc),
                "votes": exc.votes,
                "required_approvals": exc.required,
            },
        )
    raise exc  # pragma: no cover - defensive: all cases handled above


@app.post(
    "/api/v1/change-approvals",
    response_model=ChangeApprovalResponse,
    status_code=201,
)
def create_change_approval(body: ChangeApprovalCreateRequest) -> Any:
    conn = db_mod.connect()
    try:
        try:
            approval = db_mod.create_change_approval(
                conn,
                environment=body.environment,
                target_version=body.target_version,
                base_revision=body.base_revision,
                applicant=body.applicant,
                required_approvals=body.required_approvals,
            )
        except (
            db_mod.EnvironmentNotFound,
            db_mod.StaleRevision,
            db_mod.VersionNotFound,
            db_mod.NoChange,
        ) as exc:
            raise _approval_error(exc)
    finally:
        conn.close()
    return ChangeApprovalResponse(**approval)


@app.get(
    "/api/v1/change-approvals/{approval_id}",
    response_model=ChangeApprovalResponse,
)
def get_change_approval(approval_id: int) -> Any:
    conn = db_mod.connect()
    try:
        approval = db_mod.get_change_approval(conn, approval_id)
        if approval is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "approval_not_found",
                    "message": f"change approval {approval_id} not found",
                },
            )
    finally:
        conn.close()
    return ChangeApprovalResponse(**approval)


@app.post(
    "/api/v1/change-approvals/{approval_id}/votes",
    response_model=ChangeApprovalResponse,
    status_code=201,
)
def vote_change_approval(approval_id: int, body: ApprovalVoteRequest) -> Any:
    conn = db_mod.connect()
    try:
        try:
            approval = db_mod.add_approval_vote(conn, approval_id, body.approver)
        except (
            db_mod.ApprovalNotFound,
            db_mod.ApprovalNotPending,
            db_mod.SelfApproval,
            db_mod.DuplicateVote,
        ) as exc:
            raise _approval_error(exc)
    finally:
        conn.close()
    return ChangeApprovalResponse(**approval)


@app.post(
    "/api/v1/change-approvals/{approval_id}/execute",
    response_model=ChangeApprovalResponse,
)
def execute_change_approval(approval_id: int) -> Any:
    conn = db_mod.connect()
    try:
        try:
            approval = db_mod.execute_change_approval(conn, approval_id)
        except (
            db_mod.ApprovalNotFound,
            db_mod.ApprovalNotPending,
            db_mod.ApprovalsNotMet,
            db_mod.StaleRevision,
        ) as exc:
            raise _approval_error(exc)
    finally:
        conn.close()
    return ChangeApprovalResponse(**approval)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "feature-flag-decision-service",
        "docs": "/docs",
        "health": "/healthz",
        "endpoints": [
            "POST /api/v1/configs",
            "GET /api/v1/configs",
            "GET /api/v1/configs/{version}",
            "POST /api/v1/config-comparisons",
            "POST /api/v1/evaluation-previews",
            "PUT /api/v1/environments/{name}",
            "GET /api/v1/environments/{name}",
            "GET /api/v1/environments/{name}/history",
            "POST /api/v1/environments/{name}/evaluate",
            "POST /api/v1/change-approvals",
            "GET /api/v1/change-approvals/{id}",
            "POST /api/v1/change-approvals/{id}/votes",
            "POST /api/v1/change-approvals/{id}/execute",
            "POST /api/v1/evaluate",
        ],
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
