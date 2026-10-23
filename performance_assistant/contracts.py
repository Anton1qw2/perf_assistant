import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1


class SourceRange(Contract):
    path: str = Field(min_length=1, max_length=512)
    start: int = Field(ge=1)
    end: int = Field(ge=1)


class Options(Contract):
    metrics: list[str] = Field(default_factory=list, max_length=16)
    source_paths: list[str] = Field(default_factory=list, max_length=8)
    hotspots: int = Field(default=3, ge=0, le=5)
    active_seconds: int = Field(default=180, ge=10, le=600)
    executor: Literal["analysis", "external"] = "analysis"
    required_ranges: list[SourceRange] = Field(default_factory=list, max_length=8)


class TaskSpec(Contract):
    summary: str = Field(default="", max_length=4000)
    repository: str | None = Field(default=None, max_length=1024)
    ref: str = Field(default="HEAD", max_length=256)
    profile_id: str | None = Field(default=None, max_length=256)
    options: Options = Field(default_factory=Options)
    open_questions: list[str] = Field(default_factory=list, max_length=16)
    spec_version: int = 0

    @field_validator("profile_id")
    @classmethod
    def opaque_id(cls, v):
        if v is not None and (not v or not re.fullmatch(r"[\w.@+-]+", v)):
            raise ValueError("profile_must_be_opaque_id")
        return v

    def readiness(self):
        return [
            message
            for condition, message in (
                (not self.summary.strip(), "summary_required"),
                (not (self.repository or self.profile_id), "target_required"),
                (bool(self.open_questions), "open_questions"),
            )
            if condition
        ]


class TaskPatch(Contract):
    expected_version: int
    # Omitted = unchanged; null = clear. Host rejects null for required fields.
    summary: str | None = None
    repository: str | None = None
    ref: str | None = None
    profile_id: str | None = None
    options: Options | None = None
    open_questions: list[str] | None = None


def patch_task(task, patch):
    if patch.expected_version != task.spec_version:
        raise ValueError("task_version_conflict")
    updates = patch.model_dump(exclude_unset=True, exclude={"schema_version", "expected_version"})
    for name, value in list(updates.items()):
        if value is None:
            updates[name] = {
                "summary": "",
                "ref": "HEAD",
                "options": Options().model_dump(),
                "open_questions": [],
            }.get(name)
    result = TaskSpec.model_validate({**task.model_dump(), **updates})
    if result != task:
        result.spec_version += 1
    return result


class Settings(Contract):
    revision: int = 0
    repository: str | None = None
    ref: str = "HEAD"
    profile_id: str | None = None
    credential_ref: str | None = None


class Question(Contract):
    id: str
    kind: Literal["clarification", "context_confirmation", "external"]
    owner: str
    prompt: str
    expected: str = "text"
    continuation: str
    answer: str | None = None


class Evidence(Contract):
    id: str
    kind: str
    origin: str
    scope: str
    timestamp: float
    sha256: str
    payload_ref: str
    synthetic: bool = False


class Finding(Contract):
    title: str = Field(max_length=300)
    observation: str = Field(max_length=3000)
    hypothesis: str = Field(default="", max_length=3000)
    citations: list[str] = Field(max_length=16)


class Report(Contract):
    report_version: Literal[1] = 1
    scope: str
    findings: list[Finding] = Field(default_factory=list, max_length=16)
    limitations: list[str] = Field(default_factory=list, max_length=32)
    analysis_completed: bool = True
    correctness: Literal["unknown", "passed", "failed"] = "unknown"
    measured_improvement: Literal["unknown", "improved", "not_improved"] = "unknown"


class Stage(Contract):
    outcome: Literal["pass", "fail", "unknown", "not_applicable", "skipped"] = "unknown"
    reason: str = "not_checked"


class ConnectionCheck(Contract):
    id: str
    source: Literal["profiler", "git", "docs"]
    revision: int
    scope: str
    stages: dict[str, Stage]
    checked_at: float
    expires_at: float
    synthetic: bool = False


class RunContext(Contract):
    revision: int = 0
    profile_id: str | None = None
    repository: str | None = None
    ref: str = "HEAD"
    pinned_commit: str | None = None


class Run(Contract):
    id: str
    conversation_id: str
    task_snapshot: TaskSpec
    context: RunContext
    status: Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]
    workflow_version: Literal[1] = 1
    checkpoint_version: Literal[1] = 1
    active_seconds: float = 0


class Turn(Contract):
    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    text: str
    classification: str
    response_to: str | None = None


class Conversation(Contract):
    id: str
    title: str
    task: TaskSpec
    settings: Settings


class PlannedAction(Contract):
    kind: Literal["tool", "source"]
    tool: str = Field(default="", max_length=128)
    arguments: dict = Field(default_factory=dict)
    path: str = Field(default="", max_length=512)


class Plan(Contract):
    actions: list[PlannedAction] = Field(default_factory=list, max_length=4)
    question: str | None = Field(default=None, max_length=2000)
    finish: bool = False


class ConsultationReply(Contract):
    text: str = Field(max_length=8000)
    question: str | None = Field(default=None, max_length=2000)


class Routing(Contract):
    intent: Literal['task', 'mixed', 'consultation', 'capabilities', 'dialogue', 'diagnostics']
    has_task_intent: bool = False


SCHEMAS = [
    ConsultationReply,
    Routing,
    SourceRange,
    PlannedAction,
    Plan,
    Options,
    TaskSpec,
    TaskPatch,
    Settings,
    Question,
    Evidence,
    Finding,
    Report,
    Stage,
    ConnectionCheck,
    RunContext,
    Run,
    Turn,
    Conversation,
]
