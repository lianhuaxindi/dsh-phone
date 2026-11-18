"""图片附件管线（协议 v1.1，PROTOCOL §9 / DECISIONS D-029）。

职责：

- 按 **magic bytes** 判定真实图片类型（不信任客户端 ``mimeType``/文件名）；
- 校验数量（≤4 张）、单张（解码后 ≤2MiB）与合计（≤6MiB）限制；
- 以 ``sha256`` 为 ``mediaId`` 落盘到 ``state/media/<sha256>.<ext>`` 并自然去重；
- 只把 ``{mediaId, mimeType, width, height, sizeBytes}`` 元数据写进历史/协议；
  原图通过 ``media.get`` 按 ``mediaId`` 拉取（仅限本 Bridge 的 ``state/media/``）。

所有校验失败都抛 :class:`MediaError`，由 connection 层映射为
``error{code:4002, retryable:false}`` 且 **不启动** turn。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger("bridge.media")

#: 单条消息最多图片数（PROTOCOL §9）。
MAX_IMAGES_PER_MESSAGE = 4
#: 单张解码后最大字节数（2 MiB）。
MAX_IMAGE_BYTES = 2 * 1024 * 1024
#: 单条消息图片合计最大字节数（6 MiB）。
MAX_TOTAL_IMAGE_BYTES = 6 * 1024 * 1024
#: ``mediaId`` 形态：sha256 小写 hex。
MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}$")

#: magic bytes → (mimeType, 扩展名)
_MAGIC_TYPES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)
_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
_EXT_MIME = {ext: mime for mime, ext in _MIME_EXT.items()}


class MediaError(ValueError):
    """图片附件不合法（→ 协议 error 4002）。"""


@dataclass(frozen=True)
class ImagePayload:
    """一张已解码/校验的图片。"""

    media_id: str
    mime_type: str
    data: bytes
    width: int
    height: int

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def ref(self) -> dict[str, Any]:
        """协议 ``Message.images`` 里的元数据。"""
        return {
            "mediaId": self.media_id,
            "mimeType": self.mime_type,
            "width": self.width,
            "height": self.height,
            "sizeBytes": self.size_bytes,
        }

    def content_block(self) -> dict[str, str]:
        """SDK ``Session.run`` 内容块（data 为 base64）。"""
        return {
            "type": "image",
            "data": base64.b64encode(self.data).decode("ascii"),
            "mimeType": self.mime_type,
        }


def detect_mime_type(data: bytes) -> str | None:
    """按 magic bytes 判定图片类型；不支持返回 ``None``。"""
    for magic, mime, _ext in _MAGIC_TYPES:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def extension_for(mime_type: str) -> str:
    return _MIME_EXT.get(mime_type, "bin")


def mime_for_extension(ext: str) -> str:
    return _EXT_MIME.get(ext.lower(), "application/octet-stream")


def _decode_base64(value: str) -> bytes:
    raw = value.strip()
    if raw.startswith("data:"):
        _prefix, _sep, payload = raw.partition(",")
        raw = payload
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:  # pragma: no cover - base64 极宽松
        raise MediaError(f"图片 base64 解码失败: {exc}") from exc


def _image_size(data: bytes, mime_type: str) -> tuple[int, int]:
    """解析宽高；无法解析时回退 (0, 0)（Pillow 可选）。"""
    try:
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(io.BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except Exception:  # pragma: no cover - Pillow 缺失或数据异常
        pass
    try:
        if mime_type == "image/png" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if mime_type == "image/gif" and len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    except Exception:  # pragma: no cover - 防御
        pass
    return 0, 0


def decode_attachments(items: Any) -> list[ImagePayload]:
    """把 ``chat.send.images`` 原始数组校验并解码为 :class:`ImagePayload`。

    Raises:
        MediaError: 数量/大小/类型不合法。
    """
    if items is None:
        return []
    if not isinstance(items, list):
        raise MediaError("images 必须是数组")
    if len(items) > MAX_IMAGES_PER_MESSAGE:
        raise MediaError(f"图片最多 {MAX_IMAGES_PER_MESSAGE} 张，收到 {len(items)} 张")
    payloads: list[ImagePayload] = []
    total = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise MediaError(f"images[{index}] 必须是对象")
        data_value = item.get("data")
        if not isinstance(data_value, str) or not data_value:
            raise MediaError(f"images[{index}].data 必须是非空 base64 字符串")
        data = _decode_base64(data_value)
        if not data:
            raise MediaError(f"images[{index}] 解码后为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise MediaError(
                f"images[{index}] 超过单张 2MiB 限制（{len(data)} 字节）"
            )
        total += len(data)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise MediaError("图片合计超过 6MiB 限制")
        mime_type = detect_mime_type(data)
        if mime_type is None:
            raise MediaError(
                f"images[{index}] 不是受支持的图片（仅 JPEG/PNG/WEBP/GIF）"
            )
        width, height = _image_size(data, mime_type)
        payloads.append(
            ImagePayload(
                media_id=hashlib.sha256(data).hexdigest(),
                mime_type=mime_type,
                data=data,
                width=width,
                height=height,
            )
        )
    return payloads


def save_images(media_dir: Path, payloads: Sequence[ImagePayload]) -> list[ImagePayload]:
    """把图片写入 ``media_dir/<sha256>.<ext>``（同 hash 去重）。"""
    if not payloads:
        return []
    media_dir.mkdir(parents=True, exist_ok=True)
    for payload in payloads:
        target = media_dir / f"{payload.media_id}.{extension_for(payload.mime_type)}"
        if target.is_file():
            continue
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(payload.data)
        tmp.replace(target)
    return list(payloads)


def load_image(media_dir: Path, media_id: str) -> tuple[str, bytes] | None:
    """按 ``mediaId`` 读取原图；非法 id 或文件不存在返回 ``None``。"""
    media_id = (media_id or "").strip()
    if not MEDIA_ID_RE.match(media_id):
        return None
    media_dir = Path(media_dir)
    for ext, mime_type in _EXT_MIME.items():
        candidate = media_dir / f"{media_id}.{ext}"
        if candidate.is_file():
            try:
                return mime_type, candidate.read_bytes()
            except OSError:  # pragma: no cover - IO 异常
                return None
    return None


def refs_from_images(payloads: Iterable[ImagePayload]) -> list[dict[str, Any]]:
    return [payload.ref() for payload in payloads]


def parse_refs(raw: Any) -> list[dict[str, Any]] | None:
    """历史库 ``images`` JSON → 协议数组（无图片返回 ``None``）。"""
    if not raw:
        return None
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:  # pragma: no cover - 历史脏数据
            return None
    if not isinstance(raw, list) or not raw:
        return None
    return [dict(item) for item in raw if isinstance(item, dict)]
