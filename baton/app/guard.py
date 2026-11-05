"""Caller binding, validation, rate limits and auditing around MCP tool bodies."""

import functools
import inspect
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.tools import Tool
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp.types import CallToolResult, TextContent

from baton import aliases, auth, datadir, messages, topics
from baton.agents.permissions import Permissions
from baton.app.reference_tools import LiteralArguments
from baton.app.tool_policy import BY_NAME, ToolPolicy
from baton.config import Config
from baton.database.store import connect
from baton.errors import BatonError, Forbidden, InvalidRequest

from .catalog import (
    AUDITED_IDS,
    CORE_PROOF_NOTE,
    GUEST_TOOLS,
    TOKEN_ARGUMENT,
)

HOURLY_LIMITS = {auth.SCOPE_READ: 1200, auth.SCOPE_WRITE: 300}


def _has_scope(principal: Any, call: dict[str, Any], scope: str) -> bool:
    return principal is not None and call["credential_role"] is not None and scope in principal.scopes


class Guard:
    """Scope check, hourly limit and audit line around every tool call; binds the caller's role."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._audit_path = config.audit_path
        self._calls: dict[tuple[str, str], deque[float]] = {}
        self._validators: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._lock = threading.Lock()

    def wrap(self, name: str, scope: str, body: Callable[..., Any]) -> Callable[..., Any]:
        policy = BY_NAME[name]
        passes_context = "call" in inspect.signature(body).parameters

        @functools.wraps(body)
        def guarded(**kwargs: Any) -> Any:
            return self._guarded_call(name, scope, body, policy, passes_context, kwargs)

        # The role is bound from the credential, so it is not part of the tool's schema; `token` is everyone's.
        guarded.__dict__["__signature__"] = _tool_signature(name, body)
        if name not in GUEST_TOOLS:
            guarded.__doc__ = (body.__doc__ or "") + CORE_PROOF_NOTE
        self._validators[name] = func_metadata(guarded, structured_output=False).validate_arguments
        return guarded

    def _guarded_call(
        self,
        name: str,
        scope: str,
        body: Callable[..., Any],
        policy: ToolPolicy,
        passes_context: bool,
        kwargs: dict[str, Any],
    ) -> Any:
        proof = kwargs.pop("token", None)
        principal = get_access_token()
        call = _call_context(principal)
        # The key is out of the arguments from here on, so nothing that copies them can copy it.
        call["grant_key"] = kwargs.pop("grant_token", None)

        def refuse(code: str, detail: str, extra: dict[str, Any] | None = None) -> Any:
            for secret in (proof, call["grant_key"]):
                if isinstance(secret, str) and secret:
                    detail = detail.replace(secret, "[redacted]")
            refusal = {**self._refuse(name, call, kwargs, code, detail, proof), **(extra or {})}
            if policy.error_result:
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(refusal))], is_error=True)
            return refusal

        if not _has_scope(principal, call, scope):
            return refuse("insufficient_scope", f"{name} needs {scope}")
        try:
            refusal = self._bind_and_check(name, policy, principal, call, kwargs, proof)
        except BatonError as error:
            return refuse(error.code, str(error), error.extra)
        if refusal is not None:
            return refuse("forbidden", refusal)
        if not self._within_limit(call["role"], name, HOURLY_LIMITS[scope]):
            return refuse("rate_limited", f"{name} is over its hourly limit")
        if passes_context:
            kwargs["call"] = call
        try:
            result = body(call["role"], **kwargs)
        except BatonError as error:
            return refuse(error.code, str(error), error.extra)
        self._audit(name, call, kwargs, "ok", proof)
        return result

    def _bind_and_check(
        self,
        name: str,
        policy: ToolPolicy,
        principal: Any,
        call: dict[str, Any],
        kwargs: dict[str, Any],
        proof: object,
    ) -> str | None:
        if proof is not None:
            self._resolve(call, proof)
        refusal = self._capability(name, call, kwargs)
        if refusal is not None:
            return refusal
        self._bind_identity(policy, principal, call, kwargs)
        return None

    def _bind_identity(self, policy: ToolPolicy, principal: Any, call: dict[str, Any], kwargs: dict[str, Any]) -> None:
        if call["identity"] is not None and policy.identity == "bound":
            kwargs["identity"] = call["identity"]
        if call["role"] == "guest" and policy.identity == "guest":
            if policy.post_quota:
                self._admit_post(principal, call)
            elif call["identity"] is None:
                raise Forbidden("alias binding is missing, invalid or expired")
            kwargs["identity"] = call["identity"]
        if (
            call["role"] == "guest"
            and policy.identity == "bound"
            and policy.post_quota
            and call["identity"] is not None
        ):
            # A bound edit spends quota even if a later grant check refuses it.
            self._admit_post(principal, call)

    def take_literally(self, tool: Tool) -> None:
        """Make `tool` read its arguments as sent, and check the calls that carry a token by that same metadata."""
        tool.fn_metadata = LiteralArguments(**dict(tool.fn_metadata))
        self._validators[tool.name] = tool.fn_metadata.validate_arguments

    def _resolve(self, call: dict[str, Any], proof: object) -> None:
        """A present token selects the alias, and the alias's mapping the role the call runs as."""
        if call["credential_role"] != "guest":
            raise Forbidden("a binding token is accepted only on a guest credential")
        conn = connect(self._config.db_path)
        try:
            call["identity"] = aliases.resolve_binding(conn, proof)
        finally:
            conn.close()
        if call["identity"]["effective_role"] not in self._config.roles:
            raise Forbidden("this alias is mapped to a role the host no longer has")
        call["role"] = call["identity"]["effective_role"]

    def _capability(self, name: str, call: dict[str, Any], kwargs: dict[str, Any]) -> str | None:
        """Why the role this call runs as may not make it, or None."""
        policy = BY_NAME[name]
        return Permissions.for_role(self._config, call["role"]).tool_refusal(
            name,
            guest_access=policy.guest,
            requires_job_authority=policy.job_authority,
            has_binding=call["identity"] is not None,
            has_grant=call["grant_key"] is not None,
            arguments=kwargs,
        )

    def _admit_post(self, principal: Any, call: dict[str, Any]) -> None:
        if call["identity"] is None:
            raise Forbidden("alias binding is missing, invalid or expired")
        conn = connect(self._config.db_path)
        try:
            aliases.admit_post(conn, self._config, principal, call["identity"])
        finally:
            conn.close()

    async def refuse_author_fields(self, ctx: Any, call_next: Any) -> Any:
        """Server middleware: refuse a tool call that names an author, before its arguments are validated.

        The SDK would otherwise drop an unknown argument silently, so a client trying to speak as someone
        else would never learn that it can't. The refusal is an MCP tool error carrying the usual
        {"error", "detail"}, and nothing is stored.
        """
        params = ctx.params if isinstance(getattr(ctx, "params", None), dict) else {}
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return await call_next(ctx)
        named = sorted(set(arguments) & set(messages.AUTHOR_FIELDS))
        if getattr(ctx, "method", None) != "tools/call" or not named:
            return await call_next(ctx)
        name = str(params.get("name", ""))
        refusal = self._refuse(
            name,
            _call_context(get_access_token()),
            arguments,
            "forbidden_field",
            f"{named[0]} is not accepted: a message's author is always your credential's role",
        )
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(refusal))], is_error=True)

    async def refuse_claim_validation(self, ctx: Any, call_next: Any) -> Any:
        """Prevent SDK validation from echoing a claim's passphrase, even if another argument is missing.

        A token is refused here too: claim_alias's schema has none, and the SDK would otherwise drop it silently.
        """
        params = ctx.params if isinstance(getattr(ctx, "params", None), dict) else {}
        if getattr(ctx, "method", None) != "tools/call" or params.get("name") != "claim_alias":
            return await call_next(ctx)
        arguments = params.get("arguments")
        # A missing field's SDK error can include the entire input object, including the passphrase.
        try:
            if not isinstance(arguments, dict) or not isinstance(arguments.get("name"), str):
                raise InvalidRequest("claim_alias requires a name and a passphrase of 1-1024 characters")
            if arguments.get("token") is not None:
                raise InvalidRequest("claim_alias takes no token: claim with a name and passphrase only")
            aliases.validate_passphrase(arguments.get("passphrase"))
        except InvalidRequest as error:
            refusal = self._refuse("claim_alias", _call_context(get_access_token()), {}, error.code, str(error))
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(refusal))], is_error=True)
        return await call_next(ctx)

    async def refuse_token_validation(self, ctx: Any, call_next: Any) -> Any:
        """Shape-check a present `token` or `grant_token` on any tool, and a guest post's, before SDK errors can
        echo them. A grant key needs no token beside it for this: a call that leaves out a required argument
        reaches the SDK either way, and its error quotes part of the input."""
        params = ctx.params if isinstance(getattr(ctx, "params", None), dict) else {}
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)
        name, arguments = str(params.get("name", "")), params.get("arguments")
        call = _call_context(get_access_token())
        present = isinstance(arguments, dict) and (
            arguments.get("token") is not None or arguments.get("grant_token") is not None
        )
        if not present and not (call["credential_role"] == "guest" and name == "send_message"):
            return await call_next(ctx)
        try:
            # Past this check the arguments are a dict: either a token or key was present in one, or it's a post.
            if not isinstance(arguments, dict):
                raise InvalidRequest("send_message requires text")
            if name == "send_message" and not isinstance(arguments.get("text"), str):
                raise InvalidRequest("send_message requires text")
            if arguments.get("token") is not None or (call["credential_role"] == "guest" and name == "send_message"):
                aliases.validate_proof(arguments.get("token"))
            validate = self._validators.get(name)
            if validate is None:
                raise InvalidRequest(f"{name} takes no token")
            try:
                validate(arguments)
            except Exception:
                # This SDK-only boundary fails closed: exception text/input_value may contain the proof.
                raise InvalidRequest(f"{name} arguments do not match the tool schema") from None
        except BatonError as error:
            refusal = self._refuse(name, call, {}, error.code, str(error))
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(refusal))], is_error=True)
        return await call_next(ctx)

    def _within_limit(self, role: str, name: str, per_hour: int) -> bool:
        now = time.monotonic()
        with self._lock:
            calls = self._calls.setdefault((role, name), deque())
            while calls and calls[0] < now - 3600:
                calls.popleft()
            if len(calls) >= per_hour:
                return False
            calls.append(now)
            return True

    def _refuse(
        self, name: str, call: dict[str, Any], kwargs: dict[str, Any], code: str, detail: str, proof: object = None
    ) -> dict:
        self._audit(name, call, kwargs, code, proof)
        return {"error": code, "detail": detail}

    def _audit(
        self, name: str, call: dict[str, Any], kwargs: dict[str, Any], outcome: str, proof: object = None
    ) -> None:
        """One line: the credential, the role the call ran as, any alias by id and code, and the ids it named."""
        role, identity = call["role"], call["identity"]
        proof = kwargs.get("token") if proof is None else proof  # a refusal before Guard sees the raw arguments
        ids = (
            {
                key: kwargs[key]
                for key in AUDITED_IDS
                if kwargs.get(key) is not None and not (key == "topic" and name in topics.AUDIT_TOOLS)
            }  # the topic tools' fields come below
            if name != "claim_alias" and not (name == "send_message" and role == "guest")
            else {}
        )
        ids.update(topics.audit_fields(name, kwargs, outcome, call.get("topic", {})))
        # Neither the binding token nor the grant key may reach the line, whichever argument carried it, topic
        # fields included.
        # A refusal before the guard has taken the key out audits the raw arguments: look there too, as for the token.
        for secret in (proof, call.get("grant_key") or kwargs.get("grant_token")):
            if isinstance(secret, str) and secret:
                ids = {key: value for key, value in ids.items() if secret not in json.dumps(value)}
        line = {
            "at": datetime.now(UTC).isoformat(),
            "role": role,
            "credential_role": call["credential_role"],
            "credential_id": call["credential_id"],
            "tool": name,
            **ids,
            "outcome": outcome,
            **({"alias_id": identity["alias_id"], "alias_code": identity["code"]} if identity else {}),
        }
        directory = self._audit_path.parent
        with self._lock, datadir.opened(directory) as dir_fd:
            fd = datadir.open_file(
                dir_fd, self._audit_path.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, directory=directory
            )
            try:
                os.write(fd, (json.dumps(line) + "\n").encode())
            finally:
                os.close(fd)


def _call_context(principal: Any) -> dict[str, Any]:
    """The credential's role and id (a bearer entry's name or an OAuth grant, never the token), before any proof."""
    role = auth.role_of(principal)
    authority = auth.credential_authority(principal) if principal is not None else {}
    return {
        "credential_role": role,
        "credential_id": authority.get("name") or authority.get("grant"),
        "authority": authority,
        "role": role,
        "identity": None,
        "grant_key": None,
    }


def _tool_signature(name: str, body: Callable[..., Any]) -> inspect.Signature:
    signature = inspect.signature(body)
    parameters = [p for p in signature.parameters.values() if p.name not in {"role", "identity", "call"}]
    if name != "claim_alias":
        parameters.append(
            inspect.Parameter("token", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=TOKEN_ARGUMENT)
        )
    return signature.replace(parameters=parameters)
