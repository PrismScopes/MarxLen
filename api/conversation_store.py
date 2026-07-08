import os
import json
import uuid
import sqlite3
import logging
from datetime import datetime
from typing import List, Optional

from .models import Conversation, ConversationDetail, Message, SourceItem

logger = logging.getLogger(__name__)

# 数据库路径，放在 api 目录下
_DB_DIR = os.path.dirname(os.path.abspath(__file__))
CONVERSATIONS_DB_PATH = os.path.join(_DB_DIR, "conversations.db")


class ConversationStore:
    """SQLite 对话记录存储"""

    def __init__(self, db_path: str = CONVERSATIONS_DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                sources         TEXT DEFAULT '[]',
                created_at      TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conv
            ON messages(conversation_id)
        """)
        self._conn.commit()

    def _now(self) -> str:
        return datetime.now().isoformat()

    def create_conversation(self, title: str = "新对话") -> Conversation:
        """创建新对话"""
        conv_id = str(uuid.uuid4())
        now = self._now()
        self._conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, title, now, now)
        )
        self._conn.commit()
        logger.info(f"创建对话: {conv_id}")
        return Conversation(id=conv_id, title=title, created_at=now, updated_at=now, message_count=0)

    def get_conversations(self, limit: int = 50) -> List[Conversation]:
        """获取对话历史列表"""
        rows = self._conn.execute("""
            SELECT c.id, c.title, c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id) as msg_count
            FROM conversations c
            ORDER BY c.updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [
            Conversation(
                id=row[0],
                title=row[1],
                created_at=row[2],
                updated_at=row[3],
                message_count=row[4],
            )
            for row in rows
        ]

    def get_conversation(self, conv_id: str) -> Optional[ConversationDetail]:
        """获取对话详情（含消息列表）"""
        row = self._conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (conv_id,)
        ).fetchone()
        if row is None:
            return None

        msg_rows = self._conn.execute(
            "SELECT role, content, sources, created_at FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conv_id,)
        ).fetchall()

        messages = []
        for msg in msg_rows:
            sources_data = []
            try:
                raw = json.loads(msg[2]) if msg[2] else []
                for s in raw:
                    if isinstance(s, dict):
                        sources_data.append(SourceItem(**s))
            except (json.JSONDecodeError, TypeError):
                pass

            messages.append(Message(
                role=msg[0],
                content=msg[1],
                sources=sources_data if sources_data else None,
            ))

        return ConversationDetail(
            id=row[0],
            title=row[1],
            messages=messages,
            created_at=row[2],
            updated_at=row[3],
        )

    def add_message(self, conv_id: str, role: str, content: str, sources: Optional[List[SourceItem]] = None) -> Message:
        """添加消息到对话"""
        now = self._now()
        sources_json = json.dumps([s.model_dump() for s in sources], ensure_ascii=False) if sources else "[]"

        self._conn.execute(
            "INSERT INTO messages (conversation_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, ?)",
            (conv_id, role, content, sources_json, now)
        )
        # 更新对话的 updated_at
        self._conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id)
        )
        self._conn.commit()

        return Message(role=role, content=content, sources=sources)

    def delete_conversation(self, conv_id: str):
        """删除对话及其所有消息"""
        self._conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        self._conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        self._conn.commit()
        logger.info(f"删除对话: {conv_id}")

    def update_title(self, conv_id: str, title: str):
        """更新对话标题"""
        now = self._now()
        self._conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conv_id)
        )
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
