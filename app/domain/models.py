from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorCode(StrEnum):
    INPUT = "invalid_input"
    RETRIEVAL = "retrieval_error"
    CITATION = "invalid_citation"
    SQL_GENERATION = "sql_generation_error"
    SQL_REJECTED = "sql_rejected"
    EXECUTION = "execution_error"
    QUERY_TIMEOUT = "query_timeout"
    ANALYSIS = "analysis_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_TOOL_UNSUPPORTED = "model_tool_unsupported"
    MODEL_INVALID = "model_invalid_response"
    LIMIT = "tool_limit"
    SYSTEM = "system_error"
    NOT_FOUND = "not_found"
    CONFLICT = "state_conflict"
    OVERLOADED = "overloaded"
    RUN_TIMEOUT = "run_timeout"
    CANCELLED = "cancelled"
    FORBIDDEN = "forbidden"


class ErrorInfo(Contract):
    code: ErrorCode
    message: str
    retryable: bool = False


class AppError(Exception):
    def __init__(self, code: ErrorCode, message: str, retryable: bool = False):
        self.info = ErrorInfo(code=code, message=message, retryable=retryable)
        super().__init__(message)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    APPROVAL_REQUIRED = "approval_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {RunStatus.APPROVAL_REQUIRED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


class RunRequest(Contract):
    question: str = Field(min_length=2, max_length=2000)
    start_date: date | None = None
    end_date: date | None = None
    campaign_id: int | None = Field(default=None, ge=1, le=6)

    @model_validator(mode="after")
    def validate_input(self) -> "RunRequest":
        self.question = self.question.strip()
        if len(self.question) < 2:
            raise ValueError("问题不能为空")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("起止日期必须同时提供")
        if self.start_date and self.end_date:
            if self.start_date > self.end_date:
                raise ValueError("起始日期不能晚于结束日期")
            if (self.end_date - self.start_date).days > 90:
                raise ValueError("单次查询最多 91 天")
        return self


class Evidence(Contract):
    kind: Literal["document", "query"]
    summary: str
    chunk_id: str | None = None
    doc_id: str | None = None
    version: str | None = None
    source: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    score: float | None = None
    query_hash: str | None = None
    row_count: int | None = None


class Metrics(Contract):
    duration_ms: float = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = None
    model_used: str | None = None
    retry_count: int = 0
    fallback_count: int = 0
    attempts: int = 0
    usage_complete: bool = True
    price_version: str | None = None
    cost_currency: str | None = None


class ToolResult[T](Contract):
    status: Literal["ok", "error"]
    data: T | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    error: ErrorInfo | None = None
    metrics: Metrics = Field(default_factory=Metrics)


class ToolCall(Contract):
    span_id: str
    name: str
    status: Literal["started", "ok", "error"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None


class TraceEvent(Contract):
    event_id: int
    run_id: str
    trace_id: str
    span_id: str | None = None
    parent_span_id: str | None = None
    timestamp: datetime
    event: str
    data: dict[str, Any]


class ApprovalRequest(Contract):
    action: str = "budget_change"
    summary: str
    executable: Literal[False] = False
    reason: str = "分析流程只生成提议。独立沙盒可审批和执行模拟预算，不连接真实投放平台。"
    approval_id: str | None = None


class RootCause(Contract):
    code: str
    title: str
    contribution: float
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str]
    counter_evidence: list[str]
    confidence_components: dict[str, float]


class Analysis(Contract):
    current: dict[str, float | None]
    previous: dict[str, float | None]
    changes: dict[str, dict[str, float | None]]
    root_causes: list[RootCause]
    contributions: dict[str, list[dict[str, Any]]]
    sufficient_data: bool
    limitations: list[str]


class Answer(Contract):
    conclusion: str
    evidence: list[Evidence]
    root_causes: list[RootCause] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    recommendations: list[str] = Field(default_factory=list)
    citations: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    analysis: Analysis | None = None
    markdown: str = ""


class RunResult(Contract):
    run_id: str
    trace_id: str
    root_span_id: str | None = None
    experiment_id: str | None = None
    status: RunStatus
    mode: str
    created_at: datetime
    updated_at: datetime
    answer: Answer | None = None
    approval: ApprovalRequest | None = None
    error: ErrorInfo | None = None
    metrics: Metrics = Field(default_factory=Metrics)


class SQLPlan(Contract):
    sql: str = Field(min_length=1, max_length=12000)
    metrics: list[str]
    start_date: date
    end_date: date
    dimensions: list[str]
    assumptions: list[str]


class QueryData(Contract):
    columns: list[str]
    column_types: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    query_hash: str
    normalized_sql: str
    truncated: bool = False
