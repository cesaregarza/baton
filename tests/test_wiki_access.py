"""Who may call the wiki tools and what each call leaves behind: bindings, scopes, post quota, the audit line, the
tool list and the `[wiki]` section.

What this pins:

* a guest without a live binding is refused on every wiki tool with the existing binding text, and nothing changes;
* reads change nothing and spend no quota; a guest's every edit and restore spends its post quota, refused, stale or
  a retry, and a core role's spend none;
* a credential needs write scope to edit or restore, and read scope to read;
* the audit line of an edit or a restore names the page, never its text, and never a token sent as a path;
* the tools are offered only with `[wiki] enabled = true`, and the section takes no other key.
"""

from __future__ import annotations

import json
import secrets
import time
from types import SimpleNamespace

import pytest
import wiki_support as wiki
from test_aliases import rpc
from test_reference_surface import only_scopes
from topic_grant_tools_support import play, refused, succeeded
from wiki_support import Caller, anonymous, bound

from baton import aliases, auth, config

PAGE = "ops/deploys"


@pytest.fixture
def host(tmp_path):
    return wiki.wiki_host(tmp_path)


def every_call(path=PAGE):
    """One well-formed call of each wiki tool, against a page at revision 1."""
    return [
        ("read_wiki", {"path": path}),
        ("list_wiki", {}),
        ("search_wiki", {"query": "notes"}),
        ("wiki_history", {"path": path}),
        ("edit_wiki", {"path": path, "body": "Guessed notes.", "expected_revision": 1}),
        ("restore_wiki", {"path": path, "revision": 1, "expected_revision": 1}),
    ]


def test_a_guest_without_a_live_binding_is_refused_on_every_wiki_tool_and_changes_nothing(host, monkeypatch):
    real = time.time

    async def scenario(client):
        succeeded(await Caller(host).edit(client, PAGE, "Private notes.", 0))
        expiring = await bound(client, host)
        monkeypatch.setattr(aliases, "time", SimpleNamespace(time=lambda: real() + 25 * 3600))
        before = wiki.counts(host)
        calls = every_call()
        assert [name for name, _ in calls] == list(wiki.TOOLS)
        for name, arguments in calls:
            for who in (anonymous(host), expiring):
                result = await who.call(client, name, **arguments)
                assert result == {"error": "forbidden", "detail": wiki.BINDING_GONE}, (name, who.token, result)
            for token in ("", "not a token", "x" * 300, "\ud800", secrets.token_urlsafe(32)):
                result = await Caller(host, "guest", token).call(client, name, **arguments)
                assert result == {"error": "forbidden", "detail": wiki.BINDING_GONE}, (name, token[:12], result)
                assert "Private" not in json.dumps(result)
        assert wiki.counts(host) == before, "a refused guest wrote something, or spent quota"

    play(host, scenario)


def test_reads_change_nothing_and_spend_no_quota(host):
    async def scenario(client):
        planner = Caller(host)
        succeeded(await planner.edit(client, PAGE, "Deploy notes.", 0))
        succeeded(await planner.edit(client, PAGE, "Deploy notes, revised.", 1))
        readers = [planner, Caller(host, "worker"), await bound(client, host)]
        before = wiki.counts(host)
        for reader in readers:
            for name, arguments in every_call()[:4]:
                succeeded(await reader.call(client, name, **arguments))
            succeeded(await reader.call(client, "read_wiki", path=PAGE, revision=1))
        assert wiki.counts(host) == before

    play(host, scenario)


def test_a_guests_edits_and_restores_spend_its_post_quota_whether_written_refused_stale_or_retried(tmp_path):
    host = wiki.wiki_host(tmp_path, posts_per_alias=7)

    async def scenario(client):
        guest, planner = await bound(client, host), Caller(host)
        rid = wiki.request_id()
        succeeded(await guest.edit(client, PAGE, "One.", 0, request_id=rid))
        assert succeeded(await guest.edit(client, PAGE, "One.", 0, request_id=rid))["created"] is False
        refused(await guest.edit(client, PAGE, "Stale.", 0), "stale_revision")
        refused(await guest.edit(client, "Not/A/Path", "x", 1), "invalid_request")
        refused(await guest.edit(client, PAGE, "", 1), "invalid_request")
        succeeded(await guest.restore(client, PAGE, 1, 1))
        refused(await guest.restore(client, PAGE, 1, 1), "stale_revision")
        assert wiki.posts_counted(host) == 7

        before = wiki.counts(host)
        over = refused(await guest.edit(client, PAGE, "Over.", 2), "limit_exceeded", wiki.POSTS_OVER)
        assert over["limit"] == "posts_per_alias"
        refused(await guest.restore(client, PAGE, 1, 2), "limit_exceeded", wiki.POSTS_OVER)
        assert wiki.counts(host) == before, "an edit over the quota wrote something"
        assert succeeded(await guest.call(client, "read_wiki", path=PAGE))["current_revision"] == 2
        succeeded(await planner.edit(client, PAGE, "The planner's.", 2))
        succeeded(await planner.restore(client, PAGE, 1, 3))
        assert wiki.posts_counted(host) == 7, "a core role's edits spend no guest quota"

    play(host, scenario)


def test_writing_needs_write_scope_and_reading_needs_read_scope(host, monkeypatch):
    async def scenario(client):
        planner = Caller(host)
        succeeded(await planner.edit(client, PAGE, "Notes.", 0))
        only_scopes(monkeypatch, auth.SCOPE_READ)
        for name in wiki.WRITES:
            arguments = dict(every_call())[name]
            assert await planner.call(client, name, **arguments) == {
                "error": "insufficient_scope",
                "detail": f"{name} needs {auth.SCOPE_WRITE}",
            }
        for name, arguments in every_call()[:4]:
            succeeded(await planner.call(client, name, **arguments))
        only_scopes(monkeypatch, auth.SCOPE_WRITE)
        assert await planner.call(client, "read_wiki", path=PAGE) == {
            "error": "insufficient_scope",
            "detail": f"read_wiki needs {auth.SCOPE_READ}",
        }
        assert succeeded(await planner.edit(client, PAGE, "More notes.", 1))["revision"] == 2

    play(host, scenario)


def test_an_edit_and_a_restore_audit_their_page_and_never_their_text(host):
    marker = "marker-" + secrets.token_hex(6)

    def last_line():
        line = json.loads(host.config.audit_path.read_text().splitlines()[-1])
        return {key: value for key, value in line.items() if key != "at"}

    async def scenario(client):
        planner, guest = Caller(host), await bound(client, host)
        rid = wiki.request_id()
        succeeded(await planner.edit(client, PAGE, f"Notes {marker}.", 0, request_id=rid))
        assert last_line() == {
            "role": "planner",
            "credential_role": "planner",
            "credential_id": "planner-token",
            "tool": "edit_wiki",
            "page": PAGE,
            "request_id": rid,
            "expected_revision": 0,
            "outcome": "ok",
        }
        succeeded(await guest.restore(client, PAGE, 1, 1, request_id=rid + "-r"))
        line = last_line()
        assert (line["tool"], line["page"], line["outcome"]) == ("restore_wiki", PAGE, "ok")
        assert (line["alias_id"], line["alias_code"], line["expected_revision"]) == (guest.alias_id, guest.code, 1)
        refused(await guest.edit(client, PAGE, f"Late {marker}.", 1), "stale_revision")
        assert (last_line()["page"], last_line()["outcome"]) == (PAGE, "stale_revision")
        refused(await guest.edit(client, guest.token, "x", 0), "invalid_request")
        assert "page" not in last_line(), "a path that is not one is never written"
        audit = host.config.audit_path.read_text()
        assert marker not in audit and guest.token not in audit

    play(host, scenario)


def tool_names(listed):
    return {entry["name"] for entry in listed}


def test_the_wiki_tools_are_offered_only_when_the_wiki_is_enabled(tmp_path):
    off, on = tmp_path / "off", tmp_path / "on"
    off.mkdir(mode=0o700)
    on.mkdir(mode=0o700)
    hosts = {False: wiki.wiki_host(off, enabled=False), True: wiki.wiki_host(on)}
    schemas = {
        "read_wiki": ({"path", "revision", "token"}, {"path"}),
        "list_wiki": ({"prefix", "after", "limit", "token"}, set()),
        "search_wiki": ({"query", "prefix", "limit", "token"}, {"query"}),
        "wiki_history": ({"path", "after_revision", "limit", "token"}, {"path"}),
        "edit_wiki": (
            {"path", "body", "expected_revision", "request_id", "token"},
            {"path", "body", "expected_revision"},
        ),
        "restore_wiki": (
            {"path", "revision", "expected_revision", "request_id", "token"},
            {"path", "revision", "expected_revision"},
        ),
    }
    assert set(schemas) == set(wiki.TOOLS)
    for enabled, host in hosts.items():

        async def scenario(client, host=host, enabled=enabled):
            for role in ("planner", "guest"):
                listed = (await rpc(client, host, "tools/list", {}, role=role))["result"]["tools"]
                if not enabled:
                    assert not tool_names(listed) & set(wiki.TOOLS), role
                    continue
                catalog = {entry["name"]: entry for entry in listed if entry["name"] in wiki.TOOLS}
                assert set(catalog) == set(wiki.TOOLS), role
                for name, (properties, required) in schemas.items():
                    schema = catalog[name]["inputSchema"]
                    assert (set(schema["properties"]), set(schema.get("required", []))) == (properties, required)
                    hints = catalog[name]["annotations"]
                    assert hints["readOnlyHint"] is (name not in wiki.WRITES), name
                    assert hints["destructiveHint"] is False, name
            result = await Caller(host).edit(client, PAGE, "Notes.", 0)
            assert ("error" in result or "unparsed" in result) is not enabled, result

        play(host, scenario)


BASE = {"host": {"public_url": "https://baton.example.test"}, "roles": {"planner": {}}}


def test_the_wiki_section_is_off_by_default_and_takes_only_enabled():
    parsed = config.parse(BASE)
    assert getattr(getattr(parsed, "wiki", None), "enabled", None) is False, "the config has no wiki section"
    assert config.parse({**BASE, "wiki": {"enabled": True}}).wiki.enabled is True
    assert config.parse({**BASE, "wiki": {}}).wiki.enabled is False
    for section, detail in (
        ({"enabled": True, "enable": True}, "wiki.enable is not a setting: [wiki] takes only enabled"),
        ({"enabled": False, "max_pages": 10}, "wiki.max_pages is not a setting: [wiki] takes only enabled"),
        ({"enabled": "true"}, "wiki.enabled must be a boolean (true or false, unquoted)"),
    ):
        with pytest.raises(config.ConfigError) as raised:
            config.parse({**BASE, "wiki": section})
        assert str(raised.value) == detail
    with pytest.raises(config.ConfigError, match="^wiki must be a table$"):
        config.parse({**BASE, "wiki": True})
