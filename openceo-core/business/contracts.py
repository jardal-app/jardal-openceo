from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExecutionReference(BaseModel):
    ref: str = Field(min_length=1)
    taskId: str | None = None
    ticketNumber: int = Field(default=0, ge=0)
    codingTool: str | None = None
    autorun: Literal[False] = False


class WorkTicketV1(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: UUID
    businessReference: str = Field(pattern=r"^TASK-\d{4}-\d{5,}$")
    projectKey: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: Literal["Open", "Working", "Pending Review", "Completed", "Cancelled"]
    priority: Literal["Low", "Medium", "High", "Urgent"] = "Medium"
    kind: Literal["epic", "task", "story", "bug", "subtask"] = "task"
    version: int = Field(ge=1)
    execution: ExecutionReference | None = None
