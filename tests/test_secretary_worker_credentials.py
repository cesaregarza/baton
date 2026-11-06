"""Worker startup corrects stale missing-key health when a credential appears."""

from __future__ import annotations

import asyncio
import contextlib
import json

from secretary_support import make_world
from test_secretary_delivery import _worker

from baton.agents.secretary.attention.health import ModelHealth
from baton.agents.secretary.attention.state import AttentionLog
from baton.config.secretary import load_policy


def test_startup_clears_only_current_missing_credential_failure(tmp_path, monkeypatch) -> None:
    with make_world(tmp_path) as world:
        worker, _ = _worker(world, tmp_path)
        policy = json.loads(world.policy_path.read_text())
        policy["summary_bytes"] = 4000
        policy["model"] = {
            "provider": "openai",
            "model": "test-model",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key_env": "SECRETARY_TEST_KEY",
            "reasoning_effort": "low",
            "max_output_tokens": 1024,
            "cache_mode": "implicit",
            "timeout_seconds": 1,
        }
        world.policy_path.write_text(json.dumps(policy))
        monkeypatch.setenv("SECRETARY_TEST_KEY", "present")
        active_policy = load_policy(world.config, world.policy_path)
        attention = AttentionLog(world.state_path, world.config.data_dir, active_policy.principal)
        seeded = ModelHealth(attention, active_policy.digest).failed("missing_credential")

        async def exercise() -> dict:
            task = asyncio.create_task(worker.run())
            try:
                async with asyncio.timeout(10):
                    while True:
                        health = worker.instance.receipts.get("health")
                        if health and health.get("model", {}).get("status") == "not_used":
                            return health["model"]
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        cleared = asyncio.run(exercise())

        assert cleared["last_failure"] == "missing_credential"
        assert cleared["last_failure_at"] == seeded["last_failure_at"]
        assert cleared["successes"] == 0
        assert cleared["checked_at"] >= seeded["checked_at"]
