"""One definition of each MCP tool's registration and caller policy.

Domain checks still run inside their operations. This catalog describes only the
transport boundary; compatibility views below are derived, never edited separately.
"""

from dataclasses import dataclass
from typing import Literal

from baton.auth import SCOPE_READ, SCOPE_WRITE


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    scope: str = SCOPE_READ
    read_only: bool = True
    group: str = "core"
    guest: Literal["no", "yes", "binding", "grant"] = "no"
    identity: Literal["no", "guest", "bound"] = "no"
    post_quota: bool = False
    literal: bool = False
    job_authority: bool = False
    error_result: bool = False


POLICIES = (
    ToolPolicy("whoami", guest="yes"),
    ToolPolicy("alias_exists", guest="yes"),
    ToolPolicy("claim_alias", SCOPE_WRITE, False, guest="yes"),
    ToolPolicy("bind_events", SCOPE_WRITE, False),
    ToolPolicy("unbind_events", SCOPE_WRITE, False),
    ToolPolicy("send_message", SCOPE_WRITE, False, guest="yes", identity="guest", post_quota=True),
    ToolPolicy("read_messages", read_only=False),
    ToolPolicy("wait", read_only=False),
    ToolPolicy("create_job", SCOPE_WRITE, False, job_authority=True),
    ToolPolicy("move_job", SCOPE_WRITE, False, job_authority=True),
    ToolPolicy("get_job"),
    ToolPolicy("list_jobs", error_result=True),
    ToolPolicy("read_job_events"),
    ToolPolicy("create_conversation", SCOPE_WRITE, False, "conversation", guest="yes", identity="guest"),
    ToolPolicy("list_conversations", group="conversation", guest="yes", error_result=True),
    ToolPolicy("read_history", group="conversation", guest="yes"),
    ToolPolicy("read_context", group="conversation"),
    ToolPolicy("add_participant", SCOPE_WRITE, False, "conversation"),
    ToolPolicy("remove_participant", SCOPE_WRITE, False, "conversation"),
    ToolPolicy("set_topic", SCOPE_WRITE, False, "conversation", guest="grant", identity="bound"),
    ToolPolicy("create_topic", SCOPE_WRITE, False, "conversation", guest="grant", identity="bound"),
    ToolPolicy("update_topic", SCOPE_WRITE, False, "conversation", identity="bound"),
    ToolPolicy("rename_topic", SCOPE_WRITE, False, "conversation", identity="bound"),
    ToolPolicy("list_topics", group="conversation", guest="yes"),
    ToolPolicy("get_topic", group="conversation", guest="yes"),
    ToolPolicy(
        "edit_reference",
        SCOPE_WRITE,
        False,
        "reference",
        guest="grant",
        identity="bound",
        post_quota=True,
        literal=True,
    ),
    ToolPolicy("read_reference", group="reference", guest="binding", literal=True),
    ToolPolicy("reference_history", group="reference", guest="binding", literal=True),
    ToolPolicy("search", group="search", guest="yes"),
    ToolPolicy("get_message", group="search", guest="yes"),
    ToolPolicy("read_artifacts", group="artifact"),
    ToolPolicy("read_secretary_summaries"),
)
BY_NAME = {policy.name: policy for policy in POLICIES}


def registrations(*groups: str) -> tuple[tuple[str, str, bool], ...]:
    return tuple((policy.name, policy.scope, policy.read_only) for policy in POLICIES if policy.group in groups)


TOOLS = registrations("core")
CONVERSATION_TOOLS = registrations("conversation", "reference")
REFERENCE_TOOLS = registrations("reference")
SEARCH_TOOLS = registrations("search")
ARTIFACT_TOOLS = registrations("artifact")
GUEST_TOOLS = frozenset(p.name for p in POLICIES if p.guest in {"yes", "binding"})
REFERENCE_GUEST_TOOLS = frozenset(p.name for p in POLICIES if p.group == "reference" and p.guest == "binding")
GUEST_IDENTIFIED = frozenset(p.name for p in POLICIES if p.identity == "guest")
IDENTIFIED_CHANGES = frozenset(p.name for p in POLICIES if p.identity == "bound")
GRANT_TOOLS = frozenset(p.name for p in POLICIES if p.guest == "grant")
LITERAL_TOOLS = frozenset(p.name for p in POLICIES if p.literal)
