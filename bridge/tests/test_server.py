"""M1 端到端测试：WSS 握手、配对、认证、会话、对话、重放、语音、审批、取消。"""

from __future__ import annotations

import asyncio
import struct

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from bridge.protocol.audio import build_audio_frame

from conftest import (
    DEVICE_ID,
    TestClient,
    auth,
    client,
    client_ssl_context,
    create_session,
    hello,
    pair,
    pair_and_auth,
    raw_connection,
)


def _pcm_frame(aseq: int, ts_ms: int, amplitude: int = 1200) -> bytes:
    samples = struct.pack("<" + "h" * 320, *([amplitude] * 320))
    return build_audio_frame(aseq, ts_ms, samples)


async def _expect_error(tc: TestClient, code: int) -> dict:
    msg = await tc.expect("error")
    assert msg["code"] == code, msg
    return msg


# ---------------------------------------------------------------------------
# 握手 / 版本
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_ok(server) -> None:
    async with client(server) as tc:
        msg = await hello(tc)
        assert msg["replyTo"] == 1
        assert msg["serverId"] == server.server_id
        assert msg["bridge"] == "1.1.0"
        assert msg["needPair"] is True
        assert msg["authRequired"] is True
        assert isinstance(msg["ts"], int)


@pytest.mark.asyncio
async def test_hello_version_mismatch_closes_4402(server) -> None:
    async with client(server) as tc:
        await tc.send(
            "hello",
            v=2,
            deviceId=DEVICE_ID,
            app="x",
            os="pytest",
            ts=0,
        )
        await _expect_error(tc, 1003)
        with pytest.raises(Exception) as excinfo:
            await asyncio.wait_for(tc.ws.recv(), 3)
        assert excinfo.value.__class__.__name__ in {
            "ConnectionClosed",
            "ConnectionClosedOK",
            "ConnectionClosedError",
        }
        assert getattr(excinfo.value, "code", None) == 4402


@pytest.mark.asyncio
async def test_unauth_business_message_rejected(server) -> None:
    async with client(server) as tc:
        await hello(tc)
        await tc.send("session.list")
        await _expect_error(tc, 1001)
        with pytest.raises(Exception) as excinfo:
            await asyncio.wait_for(tc.ws.recv(), 3)
        assert getattr(excinfo.value, "code", None) == 4401


# ---------------------------------------------------------------------------
# 配对 / 认证
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_then_auth(server) -> None:
    async with client(server) as tc:
        token = await pair(tc, server)
        msg = await auth(tc, token)
        assert msg["serverId"] == server.server_id
        assert msg["sessions"] == []
        await tc.send("resync", lastSeq=0)
        resync = await tc.expect("resync.ok")
        assert resync["fromSeq"] >= 1
        assert resync["uptoSeq"] >= resync["fromSeq"] - 1
        assert resync["truncated"] is False


@pytest.mark.asyncio
async def test_pair_wrong_code(server) -> None:
    async with client(server) as tc:
        await hello(tc)
        await tc.send(
            "pair.request",
            deviceId=DEVICE_ID,
            code="000000",
            deviceName="phone",
            pubKey=None,
        )
        msg = await tc.expect("pair.error")
        assert msg["code"] == 1002
        assert msg["attemptsLeft"] == 4
        assert msg["retryable"] is True


@pytest.mark.asyncio
async def test_auth_bad_hmac_closes_4401(server) -> None:
    async with client(server) as tc:
        token = await pair(tc, server)
        await tc.send(
            "auth",
            deviceId=DEVICE_ID,
            ts=int(__import__("time").time()),
            nonce="AAAA",
            hmac="AAAA",
            v=1,
        )
        msg = await tc.expect("auth.error")
        assert msg["code"] == 1001
        assert msg["needPair"] is True
        with pytest.raises(Exception) as excinfo:
            await asyncio.wait_for(tc.ws.recv(), 3)
        assert getattr(excinfo.value, "code", None) == 4401


@pytest.mark.asyncio
async def test_repair_after_pair(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
    # 第二次连接：不需要再配对
    async with client(server) as tc:
        msg = await hello(tc)
        assert msg["needPair"] is False


# ---------------------------------------------------------------------------
# 会话 / 对话
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_flow_and_chat(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc, title="第一个会话")
        sid = session["sessionId"]
        assert session["title"] == "第一个会话"

        await tc.send("session.list")
        listing = await tc.expect("session.list.ok")
        assert [s["sessionId"] for s in listing["sessions"]] == [sid]

        await tc.send(
            "chat.send", clientId="c-1", sessionId=sid, text="你好"
        )
        ack = await tc.expect("chat.ack")
        assert ack["clientId"] == "c-1"
        assert ack["messageId"]

        delta = await tc.expect("assistant.delta")
        assert delta["sessionId"] == sid
        # chat.ack.messageId 是 user 消息；assistant.delta/done 用 assistant 消息 id（PROTOCOL §9）
        assert delta["messageId"]
        assert delta["messageId"] != ack["messageId"]
        assert delta["text"]

        done = await tc.expect("assistant.done")
        assert done["finishReason"] == "completed"
        assert done["messageId"] == delta["messageId"]

        types = [m["type"] for m in tc.events]
        assert "tool.status" in types

        await tc.send("session.resume", sessionId=sid)
        resumed = await tc.expect("session.resume.ok")
        assert resumed["session"]["sessionId"] == sid
        roles = [m["role"] for m in resumed["messages"]]
        assert roles[0] == "user"
        assert any(m["role"] == "assistant" and m["status"] == "done" for m in resumed["messages"])

        await tc.send("session.history", sessionId=sid, limit=10, before=None)
        history = await tc.expect("session.history.ok")
        assert history["hasMore"] is False
        assert len(history["messages"]) >= 2


@pytest.mark.asyncio
async def test_chat_send_requires_existing_session(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        await tc.send("chat.send", clientId="c-x", sessionId="s-missing", text="hi")
        msg = await tc.expect("error")
        assert msg["code"] == 3001


@pytest.mark.asyncio
async def test_chat_send_dedup_by_client_id(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        sid = (await create_session(tc))["sessionId"]
        await tc.send("chat.send", clientId="dup-1", sessionId=sid, text="你好")
        first = await tc.expect("chat.ack")
        # 等第一轮结束
        await tc.expect("assistant.done")
        await tc.send("chat.send", clientId="dup-1", sessionId=sid, text="你好")
        second = await tc.expect("chat.ack")
        assert second["messageId"] == first["messageId"]
        # 不应再产生新的 assistant.done
        await tc.send("ping", ts=1)
        await tc.expect("pong")


@pytest.mark.asyncio
async def test_unknown_type_ignored_when_authed(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        await tc.send("no.such.type", foo=1)
        ping_seq = await tc.send("ping", ts=123)
        pong = await tc.expect("pong")
        assert pong["replyTo"] == ping_seq


# ---------------------------------------------------------------------------
# 重放 / resync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_replays_missed_events(server) -> None:
    async with client(server) as tc:
        token = await pair_and_auth(tc, server)
        sid = (await create_session(tc))["sessionId"]
        await tc.send(
            "chat.send",
            clientId="c-resync",
            sessionId=sid,
            text="慢 一点",
        )
        await tc.expect("chat.ack")
        first_event = await tc.expect("assistant.delta")
        last_seq = first_event["seq"]

    # 等待 turn 在服务端继续产生事件（客户端已断开）
    await asyncio.sleep(0.4)

    async with client(server) as tc:
        hello_msg = await hello(tc)
        assert hello_msg["needPair"] is False
        await auth(tc, token)
        await tc.send("resync", lastSeq=last_seq)
        resync_ok = await tc.expect("resync.ok")
        assert resync_ok["fromSeq"] > last_seq
        replayed = await tc.expect("assistant.delta")
        assert replayed["seq"] >= resync_ok["fromSeq"]
        assert resync_ok["uptoSeq"] >= replayed["seq"]
        # 继续收到 done（可能已在重放里）
        done = await tc.expect("assistant.done", timeout=8)
        assert done["finishReason"] in {"completed", "cancelled"}


# ---------------------------------------------------------------------------
# 音频 / ASR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_flow(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        await tc.send(
            "audio.start",
            sampleRate=16000,
            channels=1,
            bitDepth=16,
            lang="zh",
            mode="ptt",
        )
        ready = await tc.expect("audio.ready")
        assert ready["asrMode"] == "fake"
        assert ready["codecs"] == ["pcm16"]

        for index in range(1, 36):
            await tc.ws.send(_pcm_frame(index, (index - 1) * 20))

        partial = await tc.expect("asr.partial", timeout=8)
        assert partial["text"]
        assert 0.0 <= partial["stability"] <= 1.0

        await tc.send("audio.stop", aseq=35)
        final = await tc.expect("asr.final", timeout=8)
        assert final["text"] == "帮我查一下今天的待办事项"
        assert final["durationMs"] > 0
        assert final["lang"] == "zh"


@pytest.mark.asyncio
async def test_audio_busy(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        await tc.send(
            "audio.start",
            sampleRate=16000,
            channels=1,
            bitDepth=16,
            lang="zh",
            mode="ptt",
        )
        await tc.expect("audio.ready")
        await tc.send(
            "audio.start",
            sampleRate=16000,
            channels=1,
            bitDepth=16,
            lang="zh",
            mode="ptt",
        )
        msg = await tc.expect("error")
        assert msg["code"] == 2001
        await tc.send("audio.stop", aseq=1)
        await tc.expect("asr.final", timeout=8)


@pytest.mark.asyncio
async def test_audio_silence_vad_finalizes(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        await tc.send(
            "audio.start",
            sampleRate=16000,
            channels=1,
            bitDepth=16,
            lang="zh",
            mode="continuous",
        )
        await tc.expect("audio.ready")
        # 26 帧静音（≥500ms）触发 VAD 自动定稿
        for index in range(1, 27):
            await tc.ws.send(build_audio_frame(index, (index - 1) * 20, b"\x00" * 640))
        final = await tc.expect("asr.final", timeout=8)
        assert final["text"] == ""
        # 第 25 帧静音累计达到 500ms 即触发定稿；第 26 帧是否已被计入取决于竞争
        assert final["aseq"] in (25, 26)


# ---------------------------------------------------------------------------
# 审批 / 提问
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_flow(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        sid = (await create_session(tc))["sessionId"]
        await tc.send(
            "chat.send", clientId="c-appr", sessionId=sid, text="请审批删除缓存"
        )
        await tc.expect("chat.ack")
        request = await tc.expect("approval.request", timeout=8)
        assert request["tool"] == "delete_file"
        assert request["defaultOutcome"] == "denied"
        await tc.send(
            "approval.response", id=request["id"], outcome="approved"
        )
        delta = await tc.expect("assistant.delta", timeout=8)
        assert "批准" in delta["text"] or delta["text"]
        done = await tc.expect("assistant.done", timeout=8)
        assert done["finishReason"] == "completed"


@pytest.mark.asyncio
async def test_question_flow(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        sid = (await create_session(tc))["sessionId"]
        await tc.send(
            "chat.send", clientId="c-q", sessionId=sid, text="我要提问你一个问题"
        )
        await tc.expect("chat.ack")
        request = await tc.expect("question.request", timeout=8)
        assert request["questions"][0]["id"] == "q1"
        await tc.send(
            "question.response",
            id=request["id"],
            cancelled=False,
            answers=[
                {
                    "questionId": "q1",
                    "selected": ["dark"],
                    "custom": None,
                }
            ],
        )
        done = await tc.expect("assistant.done", timeout=8)
        assert done["finishReason"] == "completed"


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_cancel(server) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        sid = (await create_session(tc))["sessionId"]
        await tc.send(
            "chat.send", clientId="c-cancel", sessionId=sid, text="慢 一点写长一点"
        )
        await tc.expect("chat.ack")
        await tc.expect("assistant.delta", timeout=8)
        await tc.send("chat.cancel", sessionId=sid)
        done = await tc.expect("assistant.done", timeout=8)
        assert done["finishReason"] == "cancelled"


# ---------------------------------------------------------------------------
# 子协议（D-015：dsphone.v1 严格校验）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("subprotocols", [None, ["bogus.v1"]])
async def test_handshake_requires_dsphone_v1(server, subprotocols) -> None:
    """未协商/协商非法子协议的客户端握手后立即被 4400 拒绝。"""
    uri = f"wss://127.0.0.1:{server.port}/ws"
    kwargs = {} if subprotocols is None else {"subprotocols": subprotocols}
    async with connect(
        uri,
        ssl=client_ssl_context(),
        open_timeout=5,
        **kwargs,
    ) as ws:
        assert ws.subprotocol is None
        with pytest.raises(ConnectionClosed) as excinfo:
            await asyncio.wait_for(ws.recv(), 5)
    assert excinfo.value.rcvd is not None
    assert excinfo.value.rcvd.code == 4400

