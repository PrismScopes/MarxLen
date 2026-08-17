import os
import json
import uuid
import sqlite3
import logging
import threading
from datetime import datetime
from typing import List, Optional

from .models import (
    Conversation, ConversationDetail, Message, MessageVariants, SourceItem,
)

logger = logging.getLogger(__name__)

# 数据库路径，放在 api 目录下
_DB_DIR = os.path.dirname(os.path.abspath(__file__))
CONVERSATIONS_DB_PATH = os.path.join(_DB_DIR, "conversations.db")


class ConversationStore:
    """SQLite 对话记录存储

    消息以树的形式组织，而不是一条线性列表。

    为什么要树：用户修改提问或重新生成回答时，旧内容不能丢——它是同一个
    问题的另一个版本，应该能用「< 2/2 >」这样的方式切回去看。若只有线性
    结构，前端为了不覆盖旧内容就只能新建一整个对话，于是同一个话题散落成
    好几条历史记录，既冗余又找不回。

    树的结构：
      - 每条消息有 parent_id，指向它回复的那条消息（根消息为 NULL）
      - 同一个 parent 下的多个子消息互为「版本」，用 variant_index 排序
      - 每个对话有一条 active_path：从根到叶，决定当前展示哪一串消息

    active_leaf_id 记在 conversations 表上，指向当前激活分支的最末端消息。
    从它沿 parent_id 往上回溯即可还原整条展示路径。
    """

    def __init__(self, db_path: str = CONVERSATIONS_DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        # 写锁：串行化 add_message 的「计数 + 插入」事务。
        # SQLite 单连接上并发执行显式 BEGIN IMMEDIATE 会互相冲突
        # （cannot start a transaction within a transaction），
        # 用进程内锁把写路径串起来最稳妥。
        self._write_lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
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
                thinking_content TEXT DEFAULT '',
                stage_detail     TEXT DEFAULT '',
                created_at      TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conv
            ON messages(conversation_id)
        """)
        self._conn.commit()
        self._migrate_to_tree()

    def _migrate_to_tree(self):
        """给既有库补上树结构所需的列，并把老数据串成一条链

        老库里的消息是纯线性的，按 id 升序首尾相连就是它的唯一分支，
        迁移后行为与原来完全一致，只是从此可以长出新分支。
        """
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(messages)")}

        if "parent_id" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN parent_id INTEGER")
        if "variant_index" not in cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN variant_index INTEGER NOT NULL DEFAULT 0"
            )
        # 思考过程 / 问题解构卡片随消息落库（老库补列，空字符串表示无内容）
        if "thinking_content" not in cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN thinking_content TEXT DEFAULT ''")
        if "stage_detail" not in cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN stage_detail TEXT DEFAULT ''")

        conv_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(conversations)")}
        if "active_leaf_id" not in conv_cols:
            self._conn.execute("ALTER TABLE conversations ADD COLUMN active_leaf_id INTEGER")

        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_parent
            ON messages(conversation_id, parent_id)
        """)

        # 已经迁移过就不再重复串链：判断依据是存在任何非 NULL 的 parent_id，
        # 或者所有对话都已经有 active_leaf_id
        need_link = self._conn.execute("""
            SELECT COUNT(*) FROM conversations c
            WHERE c.active_leaf_id IS NULL
              AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
        """).fetchone()[0]

        if need_link:
            for (conv_id,) in self._conn.execute(
                "SELECT id FROM conversations"
            ).fetchall():
                rows = self._conn.execute(
                    "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                    (conv_id,)
                ).fetchall()
                if not rows:
                    continue
                prev = None
                for (mid,) in rows:
                    self._conn.execute(
                        "UPDATE messages SET parent_id = ? WHERE id = ?", (prev, mid)
                    )
                    prev = mid
                self._conn.execute(
                    "UPDATE conversations SET active_leaf_id = ? WHERE id = ?",
                    (prev, conv_id)
                )
            logger.info(f"消息树迁移完成：{need_link} 个对话的历史消息已串成单分支")

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
        """获取对话历史列表

        message_count 只统计当前激活分支上的消息数。用全表 COUNT 会把
        被切走的旧版本也算进去，列表里显示的条数就跟实际看到的对不上。
        """
        rows = self._conn.execute("""
            SELECT c.id, c.title, c.created_at, c.updated_at, c.active_leaf_id
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
                message_count=len(self._active_path_rows(row[0], row[4])),
            )
            for row in rows
        ]

    def get_conversation(self, conv_id: str) -> Optional[ConversationDetail]:
        """获取对话详情

        只返回当前激活分支上的消息。被切走的旧版本仍在库里，
        通过每条消息的 variant_count / variant_index 告诉前端"这里有几个版本"，
        前端再调 /messages/{id}/siblings 或 switch 去取。
        """
        row = self._conn.execute(
            "SELECT id, title, created_at, updated_at, active_leaf_id "
            "FROM conversations WHERE id = ?",
            (conv_id,)
        ).fetchone()
        if row is None:
            return None

        messages = [
            self._to_message(m) for m in self._active_path_rows(conv_id, row[4])
        ]

        return ConversationDetail(
            id=row[0],
            title=row[1],
            messages=messages,
            created_at=row[2],
            updated_at=row[3],
        )

    # ── 树结构：路径与版本 ──────────────────────────────────

    def _active_path_rows(self, conv_id: str, leaf_id: Optional[int]) -> List[tuple]:
        """还原从根到 leaf 的整条消息链

        从叶子沿 parent_id 回溯再反转。回溯时带上 conversation_id 条件，
        避免脏数据把别的对话的消息串进来。
        """
        if leaf_id is None:
            return []

        chain = []
        seen = set()
        cur = leaf_id
        while cur is not None:
            # 环形 parent 会让这里死循环。数据正常时不会出现，
            # 但一旦出现就是整个接口挂死，代价太大，加个兜底。
            if cur in seen:
                logger.error(f"消息链出现环，已截断: conv={conv_id} at={cur}")
                break
            seen.add(cur)

            r = self._conn.execute(
                "SELECT id, role, content, sources, created_at, parent_id, "
                "variant_index, thinking_content, stage_detail "
                "FROM messages WHERE id = ? AND conversation_id = ?",
                (cur, conv_id)
            ).fetchone()
            if r is None:
                break
            chain.append(r)
            cur = r[5]

        chain.reverse()
        return chain

    def _variant_info(self, conv_id: str, parent_id: Optional[int],
                      msg_id: int) -> tuple:
        """返回 (同级版本数, 当前是第几个)，序号从 0 起"""
        if parent_id is None:
            rows = self._conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND parent_id IS NULL "
                "ORDER BY variant_index ASC, id ASC",
                (conv_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? "
                "ORDER BY variant_index ASC, id ASC",
                (conv_id, parent_id)
            ).fetchall()

        ids = [r[0] for r in rows]
        if msg_id not in ids:
            # 消息不属于该层（数据异常）：返回占位值而不是抛错，
            # 前端切换器按 variant_count<=1 隐藏，不会显示错误序号
            return max(1, len(ids)), 0
        return len(ids), ids.index(msg_id)

    def _to_message(self, row: tuple) -> Message:
        """把数据库行转成 Message，并补上版本信息"""
        (msg_id, role, content, sources_raw, _created,
         parent_id, _vi, thinking_raw, stage_raw) = row

        sources_data = []
        try:
            raw = json.loads(sources_raw) if sources_raw else []
            for s in raw:
                if isinstance(s, dict):
                    sources_data.append(SourceItem(**s))
        except (json.JSONDecodeError, TypeError):
            pass

        # conversation_id 从消息自身查一次即可，_variant_info 需要它做隔离
        conv_id = self._conn.execute(
            "SELECT conversation_id FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()[0]
        total, index = self._variant_info(conv_id, parent_id, msg_id)

        # 解构卡片：损坏的 JSON 不影响正文返回，按无卡片处理
        stage_detail = None
        if stage_raw:
            try:
                sd = json.loads(stage_raw)
                if isinstance(sd, dict):
                    stage_detail = sd
            except (json.JSONDecodeError, TypeError):
                pass

        return Message(
            id=msg_id,
            role=role,
            content=content,
            sources=sources_data if sources_data else None,
            parent_id=parent_id,
            variant_count=total,
            variant_index=index,
            thinking_content=thinking_raw or "",
            stage_detail=stage_detail,
        )

    def add_message(self, conv_id: str, role: str, content: str,
                    sources: Optional[List[SourceItem]] = None,
                    parent_id: Optional[int] = None,
                    branch: bool = False,
                    thinking_content: str = "",
                    stage_detail: Optional[dict] = None) -> Message:
        """添加消息

        参数:
            parent_id: 挂到哪条消息下。branch=False 且不传时接在激活分支末尾
            branch:    True 表示在 parent 下新开一个版本（修改提问/重新生成），
                       此时 parent_id 必须显式指定（根层版本传 None 也可以，
                       靠 branch 标志区分「接在末尾」和「根层新版本」）
            thinking_content: 思考过程文本（思考档生成时非空，随消息落库）
            stage_detail:     问题解构卡片数据（检索前置分析结果）

        写入后这条消息即成为新的激活叶子，所以发送方无需再手动切分支。

        并发安全：计数 + 插入在同一个写事务内完成。若不这样做，
        两个请求并发落库时可能读到相同的 variant_index，版本切换器错乱。
        """
        now = self._now()
        sources_json = json.dumps(
            [s.model_dump() for s in sources], ensure_ascii=False
        ) if sources else "[]"
        stage_json = json.dumps(stage_detail, ensure_ascii=False) \
            if stage_detail else ""

        if not branch and parent_id is None:
            parent_id = self._active_leaf(conv_id)

        # 同层版本计数与插入必须在同一事务内，避免并发时序号重复。
        # 写锁串行化整个事务：单连接上并发 BEGIN IMMEDIATE 会冲突，
        # 锁内保证同一时刻只有一个写事务在推进。
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if parent_id is None:
                    cnt = self._conn.execute(
                        "SELECT COUNT(*) FROM messages "
                        "WHERE conversation_id = ? AND parent_id IS NULL",
                        (conv_id,)
                    ).fetchone()[0]
                else:
                    cnt = self._conn.execute(
                        "SELECT COUNT(*) FROM messages "
                        "WHERE conversation_id = ? AND parent_id = ?",
                        (conv_id, parent_id)
                    ).fetchone()[0]

                cur = self._conn.execute(
                    "INSERT INTO messages "
                    "(conversation_id, role, content, sources, thinking_content, "
                    " stage_detail, created_at, parent_id, variant_index) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (conv_id, role, content, sources_json,
                     thinking_content or "", stage_json,
                     now, parent_id, cnt)
                )
                msg_id = cur.lastrowid

                self._conn.execute(
                    "UPDATE conversations SET updated_at = ?, active_leaf_id = ? "
                    "WHERE id = ?",
                    (now, msg_id, conv_id)
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return Message(
            id=msg_id,
            role=role,
            content=content,
            sources=sources,
            parent_id=parent_id,
            variant_count=cnt + 1,
            variant_index=cnt,
            thinking_content=thinking_content or "",
            stage_detail=stage_detail,
        )

    def _active_leaf(self, conv_id: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT active_leaf_id FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        return row[0] if row else None

    def update_message(self, conv_id: str, msg_id: int, content: str,
                       sources: Optional[List[SourceItem]] = None,
                       thinking_content: str = "",
                       stage_detail: Optional[dict] = None) -> bool:
        """更新消息内容与附件字段（生成中快照 / 结束时最终落库）

        单条 UPDATE 天然原子，无需显式事务。
        返回是否真的更新了行。
        """
        sources_json = json.dumps(
            [s.model_dump() for s in sources], ensure_ascii=False
        ) if sources else "[]"
        stage_json = json.dumps(stage_detail, ensure_ascii=False) \
            if stage_detail else ""
        cur = self._conn.execute(
            "UPDATE messages SET content = ?, sources = ?, "
            "thinking_content = ?, stage_detail = ? "
            "WHERE id = ? AND conversation_id = ?",
            (content, sources_json, thinking_content or "", stage_json,
             msg_id, conv_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_message(self, conv_id: str, msg_id: int) -> Optional[tuple]:
        """按 id 取一条消息的原始行，顺带校验它属于该对话"""
        return self._conn.execute(
            "SELECT id, role, content, sources, created_at, parent_id, "
            "variant_index, thinking_content, stage_detail "
            "FROM messages WHERE id = ? AND conversation_id = ?",
            (msg_id, conv_id)
        ).fetchone()

    def get_variants(self, conv_id: str, msg_id: int) -> Optional[MessageVariants]:
        """取某条消息的全部同级版本，供前端渲染切换器"""
        row = self.get_message(conv_id, msg_id)
        if row is None:
            return None

        parent_id = row[5]
        if parent_id is None:
            rows = self._conn.execute(
                "SELECT id, role, content, sources, created_at, parent_id, "
                "variant_index, thinking_content, stage_detail "
                "FROM messages WHERE conversation_id = ? AND parent_id IS NULL "
                "ORDER BY variant_index ASC, id ASC",
                (conv_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, role, content, sources, created_at, parent_id, "
                "variant_index, thinking_content, stage_detail "
                "FROM messages WHERE conversation_id = ? AND parent_id = ? "
                "ORDER BY variant_index ASC, id ASC",
                (conv_id, parent_id)
            ).fetchall()

        variants = [self._to_message(r) for r in rows]
        index = next((i for i, v in enumerate(variants) if v.id == msg_id), 0)

        return MessageVariants(
            message_id=msg_id,
            parent_id=parent_id,
            variant_index=index,
            variants=variants,
        )

    def switch_variant(self, conv_id: str, msg_id: int) -> Optional[ConversationDetail]:
        """切换到指定版本

        把激活分支改成「经过 msg_id」的那条。msg_id 自己可能不是叶子
        （它下面还挂着后续问答），此时要沿着每层的第一个子节点一路走到底，
        否则切过去只能看到半截对话。
        """
        row = self.get_message(conv_id, msg_id)
        if row is None:
            return None

        leaf = msg_id
        seen = {leaf}
        while True:
            child = self._conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? "
                "ORDER BY variant_index ASC, id ASC LIMIT 1",
                (conv_id, leaf)
            ).fetchone()
            if child is None or child[0] in seen:
                break
            leaf = child[0]
            seen.add(leaf)

        self._conn.execute(
            "UPDATE conversations SET active_leaf_id = ? WHERE id = ?",
            (leaf, conv_id)
        )
        self._conn.commit()
        return self.get_conversation(conv_id)

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
