"""会话与消息历史（sqlite）。

- 会话 ``Session`` 摘要（title/updatedAt/lastPreview）与消息 ``ChatMessage``。
- ``clientIds`` 表用于 ``chat.send`` 幂等：每会话保留最近 200 个。
- 消息用自增 ``seq`` 保证同一秒内也有稳定顺序；对外仍只用协议字段。
- WAL 模式 + ``check_same_thread=False`` + 进程内 RLock。
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .media import parse_refs
from .protocol.messages import ChatMessage, Session

logger = logging.getLogger("bridge.history")

__all__ = ["HistoryStore", "MAX_CLIENT_IDS_PER_SESSION"]

#: 每会话保留的 clientId 去重记录数（PROTOCOL §9.3）。
MAX_CLIENT_IDS_PER_SESSION = 200
#: session.resume 返回的最大消息条数（M1 限制，避免一次拉全量）。
MAX_RESUME_MESSAGES = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '',
    updated_at     INTEGER NOT NULL DEFAULT 0,
    last_preview   TEXT NOT NULL DEFAULT '',
    dsh_session_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'done',
    created_at INTEGER NOT NULL DEFAULT 0,
    client_id  TEXT,
    images     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_messages_session_seq
    ON messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_client
    ON messages (session_id, client_id);

CREATE TABLE IF NOT EXISTS client_ids (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    client_id  TEXT NOT NULL,
    message_id TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, client_id)
);
"""


def _new_session_id() -> str:
    return f"s-{secrets.token_hex(8)}"


def _new_message_id() -> str:
    return f"m-{secrets.token_hex(8)}"


def _preview(text: str, limit: int = 60) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit]


class HistoryStore:
    """会话/消息持久化门面（线程安全）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """v1.1：旧库补 ``messages.images`` 列（向后兼容，D-033）。"""
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "images" not in columns:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN images TEXT NOT NULL DEFAULT ''"
            )
            logger.info("history.migrated", extra={"event": "history.migrated",
                                                   "fields": {"column": "messages.images"}})

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def create_session(
        self,
        title: str | None = None,
        *,
        session_id: str | None = None,
        dsh_session_id: str = "",
        now: int | None = None,
    ) -> Session:
        """新建会话。``title`` 为空时存空串（由 UI 显示默认标题）。"""
        sid = session_id or _new_session_id()
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (session_id, title, updated_at, last_preview, dsh_session_id) "
                "VALUES (?, ?, ?, '', ?)",
                (sid, (title or "").strip(), ts, dsh_session_id or ""),
            )
            self._conn.commit()
        logger.info(
            "session.created",
            extra={"event": "session.created", "fields": {"sessionId": sid}},
        )
        return Session(session_id=sid, title=(title or "").strip(), updated_at=ts, last_preview="")

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"],
            title=row["title"] or "",
            updated_at=int(row["updated_at"] or 0),
            last_preview=row["last_preview"] or "",
        )

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def session_exists(self, session_id: str) -> bool:
        return self.get_session(session_id) is not None

    def list_sessions(self) -> list[Session]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, session_id"
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        last_preview: str | None = None,
        dsh_session_id: str | None = None,
        now: int | None = None,
    ) -> Session | None:
        sets: list[str] = []
        values: list[object] = []
        if title is not None:
            sets.append("title = ?")
            values.append(title.strip())
        if last_preview is not None:
            sets.append("last_preview = ?")
            values.append(_preview(last_preview))
        if dsh_session_id is not None:
            sets.append("dsh_session_id = ?")
            values.append(dsh_session_id)
        if now is not None or sets:
            sets.append("updated_at = ?")
            values.append(int(time.time()) if now is None else int(now))
        if not sets:
            return self.get_session(session_id)
        values.append(session_id)
        with self._lock:
            self._conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?", values)
            self._conn.commit()
        return self.get_session(session_id)

    def touch_session(self, session_id: str, *, preview: str | None = None,
                      now: int | None = None) -> None:
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            if preview is None:
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (ts, session_id),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ?, last_preview = ? WHERE session_id = ?",
                    (ts, _preview(preview), session_id),
                )
            self._conn.commit()

    def get_dsh_session_id(self, session_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT dsh_session_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return (row["dsh_session_id"] if row else "") or ""

    def set_dsh_session_id(self, session_id: str, dsh_session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET dsh_session_id = ? WHERE session_id = ?",
                (dsh_session_id or "", session_id),
            )
            self._conn.commit()

    def delete_session(self, session_id: str) -> bool:
        """本地删除会话及其消息（协议无删除消息，纯服务端能力）。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM client_ids WHERE session_id = ?", (session_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------

    def _row_to_message(self, row: sqlite3.Row) -> ChatMessage:
        keys = row.keys()
        return ChatMessage(
            message_id=row["message_id"],
            session_id=row["session_id"],
            role=row["role"],
            text=row["text"] or "",
            status=row["status"] or "done",
            created_at=int(row["created_at"] or 0),
            images=parse_refs(row["images"]) if "images" in keys else None,
        )

    def save_message(
        self,
        session_id: str,
        role: str,
        text: str = "",
        status: str = "done",
        *,
        message_id: str | None = None,
        client_id: str | None = None,
        created_at: int | None = None,
        update_preview: bool = True,
        images: list[dict[str, Any]] | None = None,
    ) -> ChatMessage:
        """插入或更新一条消息（``message_id`` 已存在则更新文本/状态）。"""
        mid = message_id or _new_message_id()
        ts = int(time.time()) if created_at is None else int(created_at)
        images_json = (
            json.dumps(images, ensure_ascii=False, separators=(",", ":")) if images else ""
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO messages
                    (message_id, session_id, role, text, status, created_at, client_id, images)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    text = excluded.text,
                    status = excluded.status,
                    images = CASE
                        WHEN excluded.images != '' THEN excluded.images
                        ELSE messages.images
                    END
                """,
                (mid, session_id, role, text or "", status or "done", ts, client_id,
                 images_json),
            )
            if client_id:
                self._conn.execute(
                    "INSERT OR REPLACE INTO client_ids (session_id, client_id, message_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, client_id, mid, ts),
                )
                self._prune_client_ids(session_id)
            self._conn.commit()
        if update_preview:
            self.touch_session(session_id, preview=text, now=ts)
        return ChatMessage(
            message_id=mid,
            session_id=session_id,
            role=role,
            text=text or "",
            status=status or "done",
            created_at=ts,
            images=images or None,
        )

    def append_message(
        self,
        session_id: str,
        role: str,
        text: str = "",
        status: str = "done",
        *,
        message_id: str | None = None,
        client_id: str | None = None,
        created_at: int | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> ChatMessage:
        """追加一条消息（``save_message`` 的别名，语义更直观）。"""
        return self.save_message(
            session_id,
            role,
            text,
            status,
            message_id=message_id,
            client_id=client_id,
            created_at=created_at,
            images=images,
        )

    def update_message(
        self,
        message_id: str,
        *,
        text: str | None = None,
        status: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> ChatMessage | None:
        sets: list[str] = []
        values: list[object] = []
        if text is not None:
            sets.append("text = ?")
            values.append(text)
        if status is not None:
            sets.append("status = ?")
            values.append(status)
        if images is not None:
            sets.append("images = ?")
            values.append(
                json.dumps(images, ensure_ascii=False, separators=(",", ":"))
            )
        if not sets:
            return self.get_message(message_id)
        values.append(message_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE message_id = ?", values
            )
            self._conn.commit()
        return self.get_message(message_id)

    def get_message(self, message_id: str) -> ChatMessage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_message(row) if row else None

    def find_by_client_id(self, session_id: str, client_id: str) -> ChatMessage | None:
        """按 ``clientId`` 查已处理的消息（幂等去重）。"""
        if not client_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT m.* FROM client_ids c JOIN messages m ON m.message_id = c.message_id "
                "WHERE c.session_id = ? AND c.client_id = ?",
                (session_id, client_id),
            ).fetchone()
        return self._row_to_message(row) if row else None

    def _prune_client_ids(self, session_id: str) -> None:
        self._conn.execute(
            """
            DELETE FROM client_ids WHERE session_id = ? AND id NOT IN (
                SELECT id FROM client_ids WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (session_id, session_id, MAX_CLIENT_IDS_PER_SESSION),
        )

    def history_page(
        self,
        session_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
    ) -> tuple[list[ChatMessage], bool]:
        """按页读取历史消息（旧→新返回；``before`` 是消息 id 游标）。"""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if before:
                ref = self._conn.execute(
                    "SELECT seq FROM messages WHERE message_id = ? AND session_id = ?",
                    (before, session_id),
                ).fetchone()
                if ref is None:
                    rows: list[sqlite3.Row] = []
                else:
                    rows = self._conn.execute(
                        "SELECT * FROM messages WHERE session_id = ? AND seq < ? "
                        "ORDER BY seq DESC LIMIT ?",
                        (session_id, int(ref["seq"]), limit + 1),
                    ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
                    (session_id, limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        messages = [self._row_to_message(row) for row in reversed(rows)]
        return messages, has_more

    def resume(self, session_id: str) -> tuple[Session | None, list[ChatMessage]]:
        """恢复会话：返回摘要 + 最近 ``MAX_RESUME_MESSAGES`` 条消息（旧→新）。"""
        session = self.get_session(session_id)
        if session is None:
            return None, []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
                (session_id, MAX_RESUME_MESSAGES),
            ).fetchall()
        messages = [self._row_to_message(row) for row in reversed(rows)]
        return session, messages

    def count_sessions(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        return int(row["n"])

    def count_messages(self, session_id: str | None = None) -> int:
        with self._lock:
            if session_id is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
                ).fetchone()
        return int(row["n"])
