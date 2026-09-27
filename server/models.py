from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemoryType = Literal["user", "feedback", "project", "reference"]
HookRuntime = Literal["python", "bash"]
SkillKind = Literal["instructions", "skill"]


class HookEvent(BaseModel):
    """Incoming hook event from a Claude Code instance."""
    model_config = ConfigDict(extra="allow")

    session_id: str
    hook_event_name: str
    cwd: str = ""
    transcript_path: str = ""
    permission_mode: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_response: Any = None
    error: str | None = None
    source: str | None = None  # SessionStart source
    model: str | None = None
    message: str | None = None  # Notification message
    notification_type: str | None = None
    agent_type: str | None = None


class SessionState(BaseModel):
    session_id: str
    instance_id: str
    status: str = "active"
    cwd: str = ""
    model: str | None = None
    started_at: str = ""
    last_event_at: str = ""
    last_tool: str | None = None
    last_tool_input_summary: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    end_reason: str | None = None


class RemoteControlUrlUpdate(BaseModel):
    """Body for PUT /api/sessions/{id}/remote-control-url. Empty string clears."""
    url: str = Field(max_length=256)


class EventRecord(BaseModel):
    id: int
    session_id: str
    instance_id: str
    event_name: str
    tool_name: str | None = None
    tool_input_summary: str | None = None
    received_at: str = ""


# --- Memory ---


class MemoryCreate(BaseModel):
    name: str
    description: str = ""
    type: MemoryType
    body: str = ""
    project_slug: str | None = None
    author_harness: str | None = None
    author_session_id: str | None = None
    author_model: str | None = None
    # Names are globally unique. A POST whose name already exists in a DIFFERENT
    # scope is rejected with 409 unless the caller explicitly opts in to moving
    # it. Without this, any by-name push could silently re-scope a memory some-
    # one deliberately pinned - which is how mirror files resurrected deleted
    # rows in the first place.
    rescope: bool = False


class MemoryUpdate(BaseModel):
    """Partial update - only fields PRESENT in the request body are applied
    (model_dump(exclude_unset=True)), so `{"project_slug": null}` unpins a
    memory to global scope while an omitted project_slug leaves scope alone.
    Existing content fields reject explicit nulls except project_slug. Author
    fields are always written from their values, including default nulls.
    """
    name: str | None = None
    description: str | None = None
    type: MemoryType | None = None
    body: str | None = None
    project_slug: str | None = None
    author_harness: str | None = None
    author_session_id: str | None = None
    author_model: str | None = None


class MemoryItem(BaseModel):
    id: int
    name: str
    description: str
    type: MemoryType
    body: str
    project_slug: str | None = None
    author_harness: str | None = None
    author_session_id: str | None = None
    author_model: str | None = None
    created_at: str
    updated_at: str


# --- Distributed skills ---


class SkillUpsert(BaseModel):
    kind: SkillKind
    enabled: bool = True
    implicit_invocation: bool = False
    instances: list[str] | None = None
    common: str
    variants: dict[str, dict[str, str]] = Field(default_factory=dict)


# --- Distributed hooks ---


class HookWiring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(min_length=1, max_length=64, pattern=r"^\S+$")
    matcher: str | None = Field(default=None, max_length=256)
    timeout: int = Field(default=10, ge=1, le=600)


class HookUpsert(BaseModel):
    """Body for PUT /api/config/hooks/{name}: a policy hook's script and its
    settings.json wiring, upserted together.

    Events are not validated against a fixed list because harnesses add them
    without notice. The legacy metadata form means Claude Code only.
    `instances` is None for "every machine", or a list of HYDRA_INSTANCE_ID
    values to restrict it to; the CLIENT filters on it, so this endpoint keeps
    returning the whole fleet's config to any machine that asks.
    """
    content: str = Field(min_length=1)
    runtime: HookRuntime = "python"
    event: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^\S+$")
    matcher: str | None = Field(default=None, max_length=256)
    timeout: int | None = Field(default=None, ge=1, le=600)
    wiring: dict[Literal["claude-code", "codex-cli"], HookWiring] | None = Field(
        default=None, min_length=1
    )
    enabled: bool = True
    instances: list[str] | None = None

    @model_validator(mode="after")
    def exactly_one_wiring_form(self) -> HookUpsert:
        legacy = bool(self.model_fields_set & {"event", "matcher", "timeout"})
        if self.wiring is not None and legacy:
            raise ValueError("Provide either legacy hook metadata or wiring, not both")
        if self.wiring is None and self.event is None:
            raise ValueError("Legacy hook metadata requires event")
        return self


# --- Projects ---


class ProjectCreate(BaseModel):
    slug: str
    path: str
    description: str = ""


class ProjectUpdate(BaseModel):
    """Partial update - only non-None fields are applied."""
    description: str | None = None


class ProjectPath(BaseModel):
    instance_id: str
    path: str
    auto_registered_at: str | None = None


class ProjectItem(BaseModel):
    slug: str
    description: str
    paths: list[ProjectPath] = Field(default_factory=list)
    created_at: str
    updated_at: str
    auto_registered_at: str | None = None


class AutoRegisterRequest(BaseModel):
    """Body for POST /api/projects/auto-register. Server derives the slug from
    the cwd basename and applies the stoplist."""
    cwd: str = Field(min_length=1, max_length=4096)


class AutoRegisterResponse(BaseModel):
    """Status values:
    - "existing": cwd matched a registered path, on this or another instance.
    - "contained": cwd is below a confirmed project's registered path.
    - "attached": slug already existed; this machine's path was added.
    - "created": brand-new slug; project + path both created.
    - "skipped": path policy rejected the cwd; no write happened.
    """
    status: Literal["existing", "contained", "attached", "created", "skipped"]
    slug: str | None = None
    reason: str | None = None


# --- Token usage ---


class UsageMessage(BaseModel):
    """One API message's token usage, as parsed from a transcript record.

    `message_id` is the transcript's `message.id` and the server's primary key,
    so re-sending a message is a no-op. Counters default to 0 rather than being
    required: older records omit fields (`speed`, the `cache_creation` split),
    and a missing counter is genuinely zero spend.
    """
    message_id: str = Field(min_length=1, max_length=128)
    ts: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    harness: str = Field(default="claude-code", min_length=1, max_length=32)
    cwd: str | None = Field(default=None, max_length=4096)
    effort: str | None = Field(default=None, max_length=32)
    is_subagent: bool = False
    agent_type: str | None = Field(default=None, max_length=128)
    service_tier: str | None = Field(default=None, max_length=32)
    speed: str | None = Field(default=None, max_length=32)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    web_search_requests: int = Field(default=0, ge=0)
    web_fetch_requests: int = Field(default=0, ge=0)


class UsageBatch(BaseModel):
    """Body for POST /api/usage/messages. One batch belongs to one session;
    `instance_id` rides the X-Instance-Id header like every other client call."""
    session_id: str = Field(min_length=1, max_length=128)
    messages: list[UsageMessage] = Field(default_factory=list)


class CodexReconcileMessage(UsageMessage):
    """One reparsed Codex row and the rollout's source session."""

    session_id: str = Field(min_length=1, max_length=128)
    harness: Literal["codex-cli"] = "codex-cli"


class CodexReconcileBatch(BaseModel):
    """Preview or apply at most one client chunk of Codex reconciliation rows."""

    apply: bool = False
    messages: list[CodexReconcileMessage] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique_message_ids(self) -> CodexReconcileBatch:
        ids = [message.message_id for message in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate message_id in reconciliation request")
        return self
