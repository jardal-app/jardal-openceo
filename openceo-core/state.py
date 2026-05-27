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
    PM = "pm"
    MARKETING = "marketing"
    FINANCE = "finance"
    SECRETARY = "secretary"
    SALES = "sales"
    MEDIA = "media"


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
    FAILED = "failed"
    ESCALATED = "escalated"
    INTERRUPTED = "interrupted"
    WAITING = "waiting"


class WorkflowRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED_TOOL = "blocked_tool"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class DiscoveryRunStatus(str, Enum):
    PENDING = "pending"
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    BLOCKED_TOOL = "blocked_tool"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"


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


class CodingTier(str, Enum):
    TIER1 = "tier1"
    TIER2 = "tier2"
    TIER3 = "tier3"
    TIER4 = "tier4"


class CodingTool(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    CLAUDE_CODE = "claude"
    GLM = "glm"
    GEMINI = "gemini"
    OPENCODE = "opencode"
    KIMI = "kimi"
    TIER_S = "tier_s"


class DevProvider(str, Enum):
    AUTO = "auto"
    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"
    OPENCODE = "opencode"
    GLM = "glm"


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
    coding_tier: CodingTier | None = None
    coding_tool: CodingTool | None = None


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
    coding_tier: CodingTier | None = None
    coding_tool: CodingTool | None = None
    coding_session_id: str | None = None
    coding_session_title: str | None = None
    steps: list[TaskStep] = Field(default_factory=list)
    post_actions_executed: list[str] = Field(default_factory=list)
    last_prompt_sent: str | None = None
    last_response_summary: str | None = None
    error: str | None = None
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
    coding_tier: CodingTier | None = None
    coding_tool: CodingTool | None = None


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


class DiscoveryApprovalRequest(BaseModel):
    actor_id: str = "workflow-api"
    approve: bool = True
    comment: str = ""
    execute: bool = False
    workflow_yaml: str | None = None


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
    coding_tier: str | None
    coding_tool: str | None
    coding_session_id: str | None
    coding_session_title: str | None
    modified_files: list[str]
    status: str


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
