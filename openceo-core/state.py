from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentName(str, Enum):
    CEO = "ceo"
    DEV = "dev"
    FINANCE = "finance"
    SECRETARY = "secretary"
    OPERATIONS = "operations"
    RESEARCH = "research"


class TaskSource(str, Enum):
    JIRA = "jira"
    NOTION = "notion"
    GITHUB = "github"
    TELEGRAM = "telegram"
    VOICE = "voice"
    WHATSAPP = "whatsapp"
    API = "api"
    ENTREVISTA = "entrevista"
    SPILLOVER = "spillover"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    # A Tier S ticket converted into an objective (§9.2): its own execution
    # ended, the children carry the work. Terminal, never pushed to ERPNext
    # (map_status has no entry); the parent goes Completed only at
    # objective close (§9.7).
    CONVERTED = "converted"
    FAILED = "failed"
    ESCALATED = "escalated"
    INTERRUPTED = "interrupted"
    WAITING = "waiting"
    # El negocio cancelo el ticket (o lo borro) en ERPNext. Es terminal y no reanuda:
    # se distingue de WAITING, que implica que algo lo va a desbloquear.
    CANCELLED = "cancelled"


#: Typed dispatcher wait reasons (``waiting_reason`` values, F3
#: TASK-2026-00113). Shared vocabulary: the dispatcher writes them,
#: ``RateMonitor.admission_for_ticket`` returns them, the dashboard and
#: Horizon read them through the existing queries. All classify as
#: operational (``waiting_needs_human_review`` allowlists human patterns
#: only), so parked tickets never queue the owner.
WAIT_LIMIT_GLOBAL = "limit_global"
WAIT_LIMIT_PROJECT = "limit_project"
WAIT_LIMIT_OBJECTIVE = "limit_objective"
WAIT_QUOTA_RESERVED = "quota_reserved"
#: Quota-full wait. Reuses the legacy ``rate_limit_queued`` string pinned by
#: ``test_autonomy_gates`` and ``test_coding_tool_routing``.
WAIT_QUOTA = "rate_limit_queued"
WAIT_BLOCKED_BY = "blocked_by"
#: Same value as ``REPO_BUSY_REASON`` in ``agents.dev.session`` (the
#: build-time enforcement) so filters keep working.
WAIT_REPO_BUSY = "repo_busy"


class WorkflowRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED_TOOL = "blocked_tool"
    BLOCKED_HUMAN = "blocked_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ApprovalState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class QAReviewState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class TaskReviewActorType(str, Enum):
    INTERNAL = "internal"
    CLIENT = "client"
    SYSTEM = "system"


class TaskReviewAction(str, Enum):
    COMMENT = "comment"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class WorkflowPhaseType(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    TERMINAL = "terminal"


class CodingTool(str, Enum):
    """Execution tier of a task (routing policy §§18-27, TASK-2026-00112).

    S = independent high assurance; A = discovery / architecture /
    recovery; B = complex strong solving with a why_not_c; C = the
    default safe execution for specified work (TASK-2026-00148). All
    four executors are top-tier models; letters order the work, never
    the models. Quota is admission control, not the tier definition.
    No account names here: accounts live only in configuration
    (``account:``). Explicit provider choice for loose chat sessions
    still uses :class:`DevProvider` and is unaffected.
    """

    TIER_S = "tier_s"
    TIER_A = "tier_a"
    TIER_B = "tier_b"
    TIER_C = "tier_c"


class ReviewMode(str, Enum):
    """Per-ticket review switch (epic TASK-2026-00248, child 00249).

    A/B/C review on by default; an explicit off keeps the tier,
    builder and gates and only skips reviewer invocations. S always
    reviews and rejects off. Absent input means on.
    """

    ON = "on"
    OFF = "off"


class DevProvider(str, Enum):
    AUTO = "auto"
    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"
    OPENCODE = "opencode"
    GLM = "glm"
    MUSE = "muse"


class DevSessionMode(str, Enum):
    AUTONOMOUS = "autonomous"
    PEER = "peer"


class DevSessionStatus(str, Enum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class SessionMessageChannel(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    SYSTEM = "system"


class SessionMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SessionControlAction(str, Enum):
    RESUME = "resume"
    CANCEL = "cancel"
    APPROVE = "approve"
    REJECT = "reject"
    COMPLETE = "complete"


class TaskStep(BaseModel):
    step: str
    status: str
    detail: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)


class TaskHistoryEntry(BaseModel):
    action: str
    actor_id: str
    detail: str = ""
    timestamp: datetime = Field(default_factory=utc_now)


class TaskCreateRequest(BaseModel):
    project: str | None = None
    source: TaskSource
    source_id: str | None = None
    title: str
    description: str
    attachments: list[str] = Field(default_factory=list)
    agent_hint: AgentName | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    params: dict[str, Any] | None = None
    workflow_key: str | None = None
    workflow_phases: list[Any] | None = None
    phase_assignments: dict[str, list[str]] | None = None
    approval_required: bool = False
    approval_state: ApprovalState | None = None
    qa_review_required: bool = False
    tags: list[str] | None = None
    created_by: str | None = None
    autorun: bool = True
    coding_tool: CodingTool | None = None
    # Per-ticket review switch (00249): on/off, absent means on. Single
    # source, stored verbatim; the run resolves it (review_policy).
    review: ReviewMode | None = None
    # Tier S design v2 §15.1 (F4/F6): internal creation (inbox promotion,
    # objective planner, closure fixes) sets these at creation, like any
    # other origin. They flow into TaskDocument via make_task_document.
    acceptance: list[str] | None = None
    objective_id: str | None = None
    blocked_by: list[str] | None = None
    origin: str | None = None
    finding_fingerprint: str | None = None
    # Business hierarchy (TASK-2026-00013): ERPNext `Task` names, consumed at
    # business registration and recorded in the erpnext mirror. They do NOT flow
    # into TaskDocument: ERPNext owns the hierarchy, Mongo must not cache it.
    parent_task: str | None = None
    depends_on: list[str] | None = None
    # Group birth (TASK-2026-00117): the ticket is created as an ERPNext group
    # (the parent of an objective). Same rule: registration-only, never cached
    # in TaskDocument.
    is_group: bool = False


class ObjectiveCreateRequest(BaseModel):
    """Top-down objective creation (TASK-2026-00117, F2)."""

    project: str
    goal: str
    acceptance: list[str] | None = None
    entry: str = "top_down"
    source: str = "api"
    source_id: str | None = None


class MissionCreateRequest(BaseModel):
    """Top-down mission creation (TASK-2026-00218)."""

    project: str
    goal: str
    acceptance: list[str] | None = None
    entry: str = "top_down"
    source: str = "api"
    source_id: str | None = None


class TaskDocument(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    ticket_number: int | None = None
    project: str | None = None
    agent: AgentName = AgentName.CEO
    source: TaskSource
    source_id: str | None = None
    title: str
    description: str
    attachments: list[str] = Field(default_factory=list)
    agent_hint: AgentName | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    params: dict[str, Any] | None = None
    workflow_key: str | None = None
    workflow_phases: list[dict[str, Any]] = Field(default_factory=list)
    phase: str | None = None
    phase_index: int = 0
    phase_assignments: dict[str, list[str]] = Field(default_factory=dict)
    phase_started_at: datetime | None = None
    approval_required: bool = False
    approval_state: ApprovalState = ApprovalState.NOT_REQUIRED
    status: TaskStatus = TaskStatus.QUEUED
    waiting_reason: str | None = None
    resume_at: datetime | None = None
    # Intencion de ejecucion: False = registrado sin ejecutar (el scheduler lo salta).
    # None = legacy sin campo, el scheduler lo trata como True. TASK-2026-00043: el
    # campo no existia y el False del registro se perdia -> Working fantasma.
    autorun: bool | None = None
    coding_tool: CodingTool | None = None
    # Per-ticket review switch (00249): on/off, absent means on.
    review: ReviewMode | None = None
    # Tier S design v2 §15.1 (F2/F6): objective membership, OpenCEO-side
    # dependencies (the enqueuer never dispatches with open blockers),
    # ticket origin and findings-inbox fingerprint for promoted tickets.
    objective_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    origin: str | None = None
    finding_fingerprint: str | None = None
    # Verifiable acceptance criteria. The planner derives them in PLAN.md
    # when the ticket does not carry them (design §20-8).
    acceptance: list[str] | None = None
    coding_session_id: str | None = None
    coding_session_title: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    post_actions_executed: list[str] = Field(default_factory=list)
    last_prompt_sent: str | None = None
    last_response_summary: str | None = None
    error: str | None = None
    # Measured cost, folded by run_task from each execution's runs
    # (TASK-2026-00212). Absent until the first recorded run; unknown
    # stays null, never 0.
    cost: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_by: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Visibility & collaboration
    involved_user_ids: list[str] = Field(default_factory=list)
    assigned_reviewer_ids: list[str] = Field(default_factory=list)
    # Client review fields
    client_visible: bool = False
    client_description: Optional[str] = None
    client_evidence: list[str] = Field(default_factory=list)
    client_review_url: Optional[str] = None
    # QA review fields
    qa_review_required: bool = False
    qa_review_state: QAReviewState = QAReviewState.NOT_REQUIRED
    qa_reviewer_id: Optional[str] = None
    qa_session_id: Optional[str] = None
    qa_evidence: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # Audit trail
    history: list[TaskHistoryEntry] = Field(default_factory=list)


class GroupDocument(BaseModel):
    group_id: str = Field(default_factory=lambda: f"grp_{uuid4().hex[:12]}")
    name: str  # unique, indexed
    display_name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    is_default: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectDocument(BaseModel):
    project_id: str = Field(default_factory=lambda: f"prj_{uuid4().hex[:12]}")
    name: str  # unique, indexed
    display_name: str
    description: str = ""
    workflow_templates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserDocument(BaseModel):
    user_id: str = Field(default_factory=lambda: f"usr_{uuid4().hex[:12]}")
    username: str  # unique, indexed
    email: str  # unique
    password_hash: str = ""
    display_name: str = ""
    is_active: bool = True
    is_admin: bool = False
    is_client: bool = False
    group_ids: list[str] = Field(default_factory=list)
    all_permissions: list[str] = Field(default_factory=list)
    allowed_projects: list[str] = Field(default_factory=list)
    must_change_password: bool = False
    last_login_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    user: dict
    token: str


class GroupCreate(BaseModel):
    name: str
    display_name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    is_default: bool = False


class TaskPatchRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    project: str | None = None
    priority: TaskPriority | None = None
    agent_hint: AgentName | None = None
    workflow_key: str | None = None
    approval_required: bool | None = None
    autorun: bool | None = None
    client_visible: bool | None = None
    client_description: str | None = None
    attachments: list[str] | None = None
    client_evidence: list[str] | None = None
    qa_evidence: list[str] | None = None
    status: TaskStatus | None = None
    waiting_reason: str | None = None
    resume_at: datetime | None = None
    error: str | None = None
    workflow_phases: list[Any] | None = None
    phase: str | None = None
    phase_index: int | None = None
    phase_assignments: dict[str, list[str]] | None = None
    approval_state: ApprovalState | None = None
    updated_by: str | None = None
    params: dict[str, Any] | None = None
    coding_tool: CodingTool | None = None
    # Per-ticket review switch (00249): patchable so a review_conflict
    # or review_invalid wait is fixable through the API.
    review: ReviewMode | None = None


class TaskWorkflowApplyRequest(BaseModel):
    workflow_key: str | None = None
    workflow_phases: list[Any] | None = None
    phase_assignments: dict[str, list[str]] = Field(default_factory=dict)
    reset_phase: bool = True
    autorun: bool | None = None


class BulkTaskWorkflowApplyRequest(BaseModel):
    task_ids: list[str]
    workflow_key: str | None = None
    workflow_phases: list[Any] | None = None
    phase_assignments: dict[str, list[str]] = Field(default_factory=dict)
    reset_phase: bool = True
    autorun: bool | None = None


class TaskPhaseActionRequest(BaseModel):
    action: TaskReviewAction
    comment: str = ""
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskPhaseKimiRequest(BaseModel):
    phase: str | None = None
    work_dir: str | None = None


class TaskPhaseRevertRequest(BaseModel):
    comment: str = ""


class TaskReviewCreateRequest(BaseModel):
    actor_type: TaskReviewActorType
    actor_id: str
    action: TaskReviewAction
    comment: str = ""
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskReviewDocument(BaseModel):
    review_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    project: str | None = None
    actor_type: TaskReviewActorType
    actor_id: str
    action: TaskReviewAction
    comment: str = ""
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    approval_state_after: ApprovalState | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SessionMessage(BaseModel):
    role: SessionMessageRole
    channel: SessionMessageChannel
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class DevSessionCreateRequest(BaseModel):
    project: str | None = None
    user_id: str = "default"
    mode: DevSessionMode = DevSessionMode.PEER
    provider: DevProvider = DevProvider.AUTO
    title: str
    objective: str
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DevSessionMessageRequest(BaseModel):
    user_id: str = "default"
    message: str
    channel: SessionMessageChannel = SessionMessageChannel.TEXT


class DevSessionControlRequest(BaseModel):
    user_id: str = "default"
    action: SessionControlAction
    reason: str | None = None


class VoicePeerProgrammingRequest(BaseModel):
    user_id: str = "default"
    session_id: str | None = None
    project: str | None = None
    provider: DevProvider = DevProvider.AUTO
    transcript: str


class ProjectSelectionRequest(BaseModel):
    user_id: str = "default"
    project: str


class ProjectSelectionResponse(BaseModel):
    user_id: str
    project: str
    updated_at: datetime


class DevSessionDocument(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    project: str | None = None
    user_id: str
    mode: DevSessionMode
    provider: DevProvider
    title: str
    objective: str
    attachments: list[str] = Field(default_factory=list)
    status: DevSessionStatus = DevSessionStatus.WAITING_USER
    waiting_reason: str | None = None
    branch_name: str | None = None
    repo_name: str | None = None
    repo_path: str | None = None
    model_used: str | None = None
    tmux_session_name: str | None = None
    messages: list[SessionMessage] = Field(default_factory=list)
    last_output: str | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class OpenAIMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "ceo"
    messages: list[OpenAIMessage]
    stream: bool = False
    project: str | None = None
    user: str | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: OpenAIMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]


class GraphState(TypedDict, total=False):
    task_id: str
    ticket_number: int | None
    project: str
    title: str
    description: str
    source: str
    source_id: str | None
    attachments: list[str]
    params: dict[str, Any] | None
    priority: str
    agent_hint: str | None
    phase: str | None
    phase_index: int
    workflow_phases: list[dict[str, Any]]
    selected_agent: str
    project_config: dict[str, Any]
    last_prompt_sent: str | None
    last_response_summary: str | None
    steps: list[dict[str, Any]]
    error: str | None
    waiting_reason: str | None
    resume_at: datetime | None
    result_comment: str | None
    coding_tool: str | None
    review: str | None
    objective_id: str | None
    coding_session_id: str | None
    coding_session_title: str | None
    modified_files: list[str]
    status: str
    cost_runs: list[dict[str, Any]] | None


def make_task_document(payload: TaskCreateRequest) -> TaskDocument:
    data = payload.model_dump()
    approval_required = bool(data.get("approval_required"))
    if data.get("tags") is None:
        data["tags"] = []
    if data.get("workflow_phases") is None:
        data.pop("workflow_phases", None)
    if data.get("phase_assignments") is None:
        data.pop("phase_assignments", None)
    if data.get("approval_state") is None:
        data["approval_state"] = ApprovalState.PENDING if approval_required else ApprovalState.NOT_REQUIRED
    qa_review_required = bool(data.get("qa_review_required"))
    if data.get("qa_review_state") is None:
        data["qa_review_state"] = QAReviewState.PENDING if qa_review_required else QAReviewState.NOT_REQUIRED
    # Optional list fields: None means "absent", not an empty override.
    if data.get("blocked_by") is None:
        data.pop("blocked_by", None)
    # If agent_hint is provided, use it as the initial agent value
    if data.get("agent_hint"):
        data.setdefault("agent", data["agent_hint"])
    # Build involved_user_ids from creator at creation time
    involved: set[str] = set()
    created_by = data.get("created_by")
    if created_by:
        involved.add(str(created_by))
    phase_assignments = data.get("phase_assignments")
    if isinstance(phase_assignments, dict):
        for user_ids in phase_assignments.values():
            if not isinstance(user_ids, list):
                continue
            involved.update(str(user_id) for user_id in user_ids if str(user_id).strip())
    if involved:
        data["involved_user_ids"] = list(involved)
    # Initial history entry
    history: list[dict[str, Any]] = []
    if created_by:
        history.append({
            "action": "created",
            "actor_id": str(created_by),
            "detail": "Ticket created",
        })
    if history:
        data["history"] = history
    return TaskDocument(**data)


def make_task_review_document(
    *,
    task_id: str,
    project: str | None,
    payload: TaskReviewCreateRequest,
    approval_state_after: ApprovalState | None = None,
) -> TaskReviewDocument:
    return TaskReviewDocument(
        task_id=task_id,
        project=project,
        actor_type=payload.actor_type,
        actor_id=payload.actor_id,
        action=payload.action,
        comment=payload.comment,
        attachments=payload.attachments,
        metadata=payload.metadata,
        approval_state_after=approval_state_after,
    )


def make_dev_session_document(payload: DevSessionCreateRequest, project: str | None) -> DevSessionDocument:
    return DevSessionDocument(
        project=project,
        user_id=payload.user_id,
        mode=payload.mode,
        provider=payload.provider,
        title=payload.title,
        objective=payload.objective,
        attachments=payload.attachments,
        metadata=payload.metadata,
    )
