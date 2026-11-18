"""v1.1 会话元信息与 token 用量测试（PROTOCOL §8.1/§9，D-032）。"""

from __future__ import annotations

from typing import Any

import pytest

from bridge.tests.conftest import client, create_session, pair_and_auth

FAKE_PROVIDER = "deepseek-official"
FAKE_MODEL = "deepseek-flash"
FAKE_EFFORT = "high"
FAKE_CONTEXT_WINDOW = 1_000_000


@pytest.mark.asyncio
async def test_session_create_sends_meta(server: Any) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        await tc.send("session.create", title="meta 测试")
        created = await tc.expect("session.create.ok")
        session_id = created["session"]["sessionId"]

        meta = await tc.expect("session.meta")
        assert meta["sessionId"] == session_id
        assert meta["provider"] == FAKE_PROVIDER
        assert meta["model"] == FAKE_MODEL
        assert meta["reasoningEffort"] == FAKE_EFFORT
        assert meta["contextWindow"] == FAKE_CONTEXT_WINDOW
        # session.meta 是事件（无 replyTo）
        assert "replyTo" not in meta


@pytest.mark.asyncio
async def test_session_resume_sends_meta(server: Any) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc)
        session_id = session["sessionId"]
        await tc.expect("session.meta")

        await tc.send("session.resume", sessionId=session_id)
        resumed = await tc.expect("session.resume.ok")
        assert resumed["session"]["sessionId"] == session_id
        meta = await tc.expect("session.meta")
        assert meta["sessionId"] == session_id


@pytest.mark.asyncio
async def test_assistant_done_usage(server: Any) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc)
        session_id = session["sessionId"]
        await tc.send(
            "chat.send",
            clientId="usage-1",
            sessionId=session_id,
            text="统计一下 token 用量",
        )
        ack = await tc.expect("chat.ack")
        assert ack["clientId"] == "usage-1"
        done = await tc.expect("assistant.done", timeout=10)
        usage = done["usage"]
        assert isinstance(usage, dict)
        for key in ("inputTokens", "outputTokens", "totalTokens", "contextWindow", "usedTokens"):
            assert isinstance(usage[key], int)
        assert usage["inputTokens"] > 0
        assert usage["outputTokens"] > 0
        assert usage["totalTokens"] == usage["inputTokens"] + usage["outputTokens"]
        assert usage["usedTokens"] == usage["totalTokens"]
        assert usage["contextWindow"] == FAKE_CONTEXT_WINDOW


@pytest.mark.asyncio
async def test_turn_emits_meta_when_not_yet_sent(server: Any) -> None:
    """create 后 meta 已发过；turn 内 DshMeta 相同则去重，不重复发送。"""
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc)
        await tc.expect("session.meta")
        await tc.send(
            "chat.send",
            clientId="meta-dedup",
            sessionId=session["sessionId"],
            text="你好",
        )
        await tc.expect("chat.ack")
        await tc.expect("assistant.done", timeout=10)
        metas = [item for item in tc.events if item.get("type") == "session.meta"]
        assert metas == [], f"session.meta 不应重复发送: {metas}"
