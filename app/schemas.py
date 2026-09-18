"""Pydantic models for the configuration payload and evaluation API."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Configuration upload models
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrereqSpec(StrictModel):
    flag: str
    expect: bool = True


class ConditionSpec(StrictModel):
    # ``attr`` is required for attribute operators; a segment reference
    # (``op == "segment"``) names its target in ``value`` and needs no attr.
    attr: Optional[str] = None
    op: str
    value: Any = None

    @model_validator(mode="after")
    def _attr_required_unless_segment_ref(self) -> "ConditionSpec":
        if self.op != "segment" and self.attr is None:
            raise ValueError("attr is required unless op is 'segment'")
        return self


class SegmentSpec(StrictModel):
    """A reusable audience group: all conditions must hold (AND)."""

    conditions: list[ConditionSpec] = Field(default_factory=list)


class RuleSpec(StrictModel):
    id: Optional[str] = None
    conditions: list[ConditionSpec] = Field(default_factory=list)
    value: Any = True


class RolloutSpec(StrictModel):
    percentage: int
    key: str = "user_id"
    value: Any = True


class FlagSpec(StrictModel):
    enabled: bool = True
    default: Any = False
    prerequisites: list[PrereqSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    rollout: Optional[RolloutSpec] = None


class ConfigSpec(StrictModel):
    segments: dict[str, SegmentSpec] = Field(default_factory=dict)
    flags: dict[str, FlagSpec] = Field(default_factory=dict)


class PublishConfigRequest(StrictModel):
    version: str = Field(min_length=1)
    segments: dict[str, SegmentSpec] = Field(default_factory=dict)
    flags: dict[str, FlagSpec] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PublishResponse(BaseModel):
    version: str
    flag_count: int
    created_at: str


class VersionInfo(BaseModel):
    version: str
    flag_count: int
    created_at: str


class ConfigResponse(BaseModel):
    version: str
    flag_count: int
    created_at: str
    config: ConfigSpec


class ChainNode(BaseModel):
    flag: str
    value: Any
    hit: bool
    reason: str
    rule_id: Optional[str] = None
    rollout: Optional[dict[str, Any]] = None
    prerequisite_of: Optional[str] = None
    expected: Optional[bool] = None


class EvalResult(BaseModel):
    flag: str
    value: Any
    hit: bool
    reason: str
    rule_id: Optional[str] = None
    rollout: Optional[dict[str, Any]] = None
    chain: list[ChainNode]


class BatchEvalResponse(BaseModel):
    version: str
    context: dict[str, Any]
    results: list[EvalResult]
    missing_flags: list[str]


# ---------------------------------------------------------------------------
# Version comparison models
# ---------------------------------------------------------------------------


class ConfigComparisonRequest(StrictModel):
    from_version: str = Field(min_length=1)
    to_version: str = Field(min_length=1)


class ImpactedFlag(BaseModel):
    flag: str
    path: list[str]


class FlagImpact(BaseModel):
    flag: str
    impacted: list[ImpactedFlag]


class ConfigComparisonResponse(BaseModel):
    from_version: str
    to_version: str
    added: list[str]
    removed: list[str]
    modified: list[str]
    impacts: list[FlagImpact]


class ErrorDetail(BaseModel):
    code: str
    message: str
    errors: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Evaluation preview models
# ---------------------------------------------------------------------------

MAX_PREVIEW_SUBJECTS = 200
MAX_PREVIEW_FLAGS = 50


class PreviewSubject(StrictModel):
    """One subject sample: a unique id plus the usual evaluation context."""

    subject_id: str = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EvaluationPreviewRequest(StrictModel):
    from_version: str = Field(min_length=1)
    to_version: str = Field(min_length=1)
    flags: list[str] = Field(min_length=1, max_length=MAX_PREVIEW_FLAGS)
    subjects: list[PreviewSubject] = Field(
        min_length=1, max_length=MAX_PREVIEW_SUBJECTS
    )

    @model_validator(mode="after")
    def _subject_ids_unique(self) -> "EvaluationPreviewRequest":
        seen: set[str] = set()
        for subject in self.subjects:
            if subject.subject_id in seen:
                raise ValueError(f"duplicate subject_id {subject.subject_id!r}")
            seen.add(subject.subject_id)
        return self


class PreviewSide(BaseModel):
    """One version's decision for a (subject, flag) pair."""

    exists: bool
    value: Any = None
    reason: Optional[str] = None
    rule_id: Optional[str] = None


class PreviewChange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subject_id: str
    flag: str
    from_: PreviewSide = Field(alias="from")
    to: PreviewSide


class PreviewFlagSummary(BaseModel):
    flag: str
    changed: int
    unchanged: int


class EvaluationPreviewResponse(BaseModel):
    from_version: str
    to_version: str
    changed: list[PreviewChange]
    summary: list[PreviewFlagSummary]


# ---------------------------------------------------------------------------
# Evaluation request models
# ---------------------------------------------------------------------------


class BatchEvalRequest(StrictModel):
    version: str = Field(min_length=1)
    flags: list[str] = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Environment pointer models
# ---------------------------------------------------------------------------


class EnvironmentUpsertRequest(StrictModel):
    version: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)


class EnvironmentResponse(BaseModel):
    name: str
    version: str
    revision: int
    created_at: str
    updated_at: str


class EnvironmentHistoryEntry(BaseModel):
    """One immutable audit record of a successful pointer write."""

    revision: int
    version: str
    operation: Literal["create", "update"]
    created_at: str


class EnvironmentHistoryResponse(BaseModel):
    name: str
    history: list[EnvironmentHistoryEntry]


class EnvironmentEvalRequest(StrictModel):
    flags: list[str] = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EnvironmentEvalResponse(BaseModel):
    name: str
    version: str
    revision: int
    context: dict[str, Any]
    results: list[EvalResult]
    missing_flags: list[str]


# ---------------------------------------------------------------------------
# Change approval models
# ---------------------------------------------------------------------------

# A change may require between 1 and 5 distinct approvers.
MAX_REQUIRED_APPROVALS = 5


class ChangeApprovalCreateRequest(StrictModel):
    environment: str = Field(min_length=1)
    target_version: str = Field(min_length=1)
    base_revision: int = Field(ge=1)
    applicant: str = Field(min_length=1)
    required_approvals: int = Field(ge=1, le=MAX_REQUIRED_APPROVALS)


class ApprovalVoteRequest(StrictModel):
    approver: str = Field(min_length=1)


class ChangeApprovalResponse(BaseModel):
    id: int
    environment: str
    target_version: str
    base_revision: int
    applicant: str
    required_approvals: int
    status: Literal["pending", "executed", "stale"]
    votes: int
    approvers: list[str]
    created_at: str
    executed_at: Optional[str] = None
    executed_revision: Optional[int] = None
