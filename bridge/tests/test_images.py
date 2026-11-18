"""v1.1 图片管线测试（PROTOCOL §9 / DECISIONS D-029）。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from bridge import media as media_module
from bridge.media import (
    MediaError,
    decode_attachments,
    detect_mime_type,
    load_image,
    save_images,
)
from bridge.tests.conftest import client, create_session, pair_and_auth

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
GIF_1X1 = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c"
    "00000000010001000002024401003b"
)
B64_PNG = base64.b64encode(PNG_1X1).decode("ascii")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class TestDetectMimeType:
    def test_png(self) -> None:
        assert detect_mime_type(PNG_1X1) == "image/png"

    def test_gif(self) -> None:
        assert detect_mime_type(GIF_1X1) == "image/gif"

    def test_jpeg_by_magic(self) -> None:
        assert detect_mime_type(b"\xff\xd8\xff\xe0" + b"0" * 16) == "image/jpeg"

    def test_webp_by_magic(self) -> None:
        raw = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8
        assert detect_mime_type(raw) == "image/webp"

    def test_unknown(self) -> None:
        assert detect_mime_type(b"not an image") is None


class TestDecodeAttachments:
    def test_single_png(self) -> None:
        payloads = decode_attachments([{"mimeType": "image/png", "data": B64_PNG}])
        assert len(payloads) == 1
        payload = payloads[0]
        assert len(payload.media_id) == 64
        assert payload.mime_type == "image/png"
        assert payload.data == PNG_1X1
        assert payload.width == 1 and payload.height == 1
        assert payload.size_bytes == len(PNG_1X1)
        assert payload.ref()["sizeBytes"] == len(PNG_1X1)
        assert payload.content_block()["type"] == "image"

    def test_data_uri_prefix(self) -> None:
        payloads = decode_attachments(
            [{"data": f"data:image/png;base64,{B64_PNG}"}]
        )
        assert payloads[0].mime_type == "image/png"

    def test_magic_bytes_win_over_mime_type(self) -> None:
        # 声明 jpeg 但内容是 png：按 magic bytes 判为 png
        payloads = decode_attachments(
            [{"mimeType": "image/jpeg", "data": B64_PNG}]
        )
        assert payloads[0].mime_type == "image/png"

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(MediaError, match="不是受支持的图片"):
            decode_attachments([{"data": _b64(b"not an image at all")}])

    def test_rejects_too_many(self) -> None:
        items = [{"data": B64_PNG} for _ in range(5)]
        with pytest.raises(MediaError, match="最多"):
            decode_attachments(items)

    def test_rejects_single_too_large(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(media_module, "MAX_IMAGE_BYTES", 10)
        with pytest.raises(MediaError, match="单张 2MiB"):
            decode_attachments([{"data": B64_PNG}])

    def test_rejects_total_too_large(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(media_module, "MAX_TOTAL_IMAGE_BYTES", 1)
        with pytest.raises(MediaError, match="合计"):
            decode_attachments([{"data": B64_PNG}])


class TestStorage:
    def test_save_and_load_dedupes(self, tmp_path: Path) -> None:
        media_dir = tmp_path / "media"
        payloads = decode_attachments([{"data": B64_PNG}])
        save_images(media_dir, payloads)
        files = list(media_dir.iterdir())
        assert len(files) == 1
        assert files[0].name == f"{payloads[0].media_id}.png"
        # 再次保存同一 hash 不新增文件
        save_images(media_dir, payloads)
        assert len(list(media_dir.iterdir())) == 1
        loaded = load_image(media_dir, payloads[0].media_id)
        assert loaded is not None
        mime_type, raw = loaded
        assert mime_type == "image/png"
        assert raw == PNG_1X1

    def test_load_rejects_bad_id(self, tmp_path: Path) -> None:
        assert load_image(tmp_path, "not-a-media-id") is None
        assert load_image(tmp_path, "0" * 64) is None


async def _send_image(
    tc: Any,
    session_id: str,
    *,
    client_id: str,
    text: str = "",
    images: list[dict[str, Any]] | None = None,
) -> None:
    await tc.send(
        "chat.send",
        clientId=client_id,
        sessionId=session_id,
        text=text,
        images=images if images is not None else [{"mimeType": "image/png", "data": B64_PNG}],
    )


@pytest.mark.asyncio
async def test_image_message_roundtrip(server: Any) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc)
        session_id = session["sessionId"]

        await _send_image(tc, session_id, client_id="img-1", text="看看这张图")
        ack = await tc.expect("chat.ack")
        assert ack["clientId"] == "img-1"
        done = await tc.expect("assistant.done", timeout=10)
        assert done["finishReason"] == "completed"

        # session.meta 在 turn 开始时补发；文本是 fake 的「收到 1 张图片。」
        assert "session.meta" in [item.get("type") for item in tc.events]
        deltas = [item for item in tc.events if item.get("type") == "assistant.delta"]
        assert deltas
        assert "1 张图片" in "".join(item["text"] for item in deltas)

        # 历史里只带元数据
        await tc.send("session.history", sessionId=session_id, limit=50, before=None)
        history = await tc.expect("session.history.ok")
        users = [item for item in history["messages"] if item["role"] == "user"]
        assert users and users[-1]["images"]
        image_ref = users[-1]["images"][0]
        assert image_ref["mimeType"] == "image/png"
        assert image_ref["sizeBytes"] == len(PNG_1X1)
        media_id = image_ref["mediaId"]

        # media.get 取回原图
        await tc.send("media.get", mediaId=media_id)
        data = await tc.expect("media.data")
        assert data["mediaId"] == media_id
        assert data["mimeType"] == "image/png"
        assert base64.b64decode(data["data"]) == PNG_1X1

        # 未知 mediaId → error 3001
        await tc.send("media.get", mediaId="0" * 64)
        error = await tc.expect("error")
        assert error["code"] == 3001


@pytest.mark.asyncio
async def test_image_only_message_allowed(server: Any) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc)
        await _send_image(tc, session["sessionId"], client_id="img-empty", text="")
        await tc.expect("chat.ack")
        done = await tc.expect("assistant.done", timeout=10)
        assert done["finishReason"] == "completed"


@pytest.mark.asyncio
async def test_invalid_image_rejected_with_4002(server: Any) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc)
        session_id = session["sessionId"]

        # 非图片 magic bytes → 4002，且不启动 turn
        await _send_image(
            tc,
            session_id,
            client_id="bad-1",
            text="坏图片",
            images=[{"mimeType": "image/png", "data": _b64(b"definitely not an image")}],
        )
        error = await tc.expect("error")
        assert error["code"] == 4002
        assert error["retryable"] is False

        # 连接仍可用：ping 正常
        ping_seq = await tc.send("ping", ts=1)
        pong = await tc.expect("pong")
        assert pong["replyTo"] == ping_seq

        # 该消息没有落库（turn 未启动、用户消息也不保存）
        await tc.send("session.history", sessionId=session_id, limit=50, before=None)
        history = await tc.expect("session.history.ok")
        assert history["messages"] == []


@pytest.mark.asyncio
async def test_too_many_images_rejected(server: Any) -> None:
    async with client(server) as tc:
        await pair_and_auth(tc, server)
        session = await create_session(tc)
        await tc.send(
            "chat.send",
            clientId="too-many",
            sessionId=session["sessionId"],
            text="很多图",
            images=[{"data": B64_PNG} for _ in range(5)],
        )
        error = await tc.expect("error")
        assert error["code"] == 4002
        assert error["retryable"] is False
