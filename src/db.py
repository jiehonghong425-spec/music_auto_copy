"""SQLite 数据库模块 — 进度跟踪与断点续传"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    preview_url TEXT,
    price_cents INTEGER,
    category TEXT,
    tags TEXT,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    download_path TEXT,
    instrumental_path TEXT,
    vocals_path TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    downloaded_at TEXT,
    separated_at TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    """进度跟踪数据库"""

    def __init__(self, db_path: Path | str = "./progress.db"):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        return self._conn

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Item CRUD ──────────────────────────────────────

    def insert_item(self, item_data: dict) -> bool:
        """插入新项目，已存在则跳过。返回 True 表示是新插入"""
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO items (id, title, author, preview_url,
                   price_cents, category, tags)
                   VALUES (:id, :title, :author, :preview_url,
                   :price_cents, :category, :tags)""",
                {
                    "id": item_data["id"],
                    "title": item_data["title"],
                    "author": item_data.get("author", ""),
                    "preview_url": item_data.get("preview_url", ""),
                    "price_cents": item_data.get("price_cents", 0),
                    "category": item_data.get("category", ""),
                    "tags": json.dumps(item_data.get("tags", [])),
                },
            )
            self.conn.commit()
            return self.conn.total_changes > 0
        except Exception:
            return False

    def get_pending_downloads(self, limit: int = 0) -> list[dict]:
        """获取待下载的项目"""
        query = "SELECT * FROM items WHERE status = 'pending' ORDER BY id"
        if limit > 0:
            query += f" LIMIT {limit}"
        return [dict(row) for row in self.conn.execute(query).fetchall()]

    def get_items_by_status(self, status: str) -> list[dict]:
        """按状态获取项目"""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM items WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        ]

    def update_status(
        self, item_id: int, status: str, error_message: str | None = None
    ):
        """更新项目状态"""
        updates = {"status": status}
        if error_message:
            updates["error_message"] = error_message

        if status == "downloaded":
            updates["downloaded_at"] = datetime.now().isoformat()
        elif status == "separated":
            updates["separated_at"] = datetime.now().isoformat()

        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        self.conn.execute(
            f"UPDATE items SET {set_clause} WHERE id = :id",
            {**updates, "id": item_id},
        )
        self.conn.commit()

    def update_download_path(self, item_id: int, path: str):
        """记录下载路径"""
        self.conn.execute(
            "UPDATE items SET download_path = ? WHERE id = ?", (path, item_id)
        )
        self.conn.commit()

    def update_separation_paths(
        self, item_id: int, instrumental_path: str, vocals_path: str
    ):
        """记录分离后的路径"""
        self.conn.execute(
            "UPDATE items SET instrumental_path = ?, vocals_path = ? WHERE id = ?",
            (instrumental_path, vocals_path, item_id),
        )
        self.conn.commit()

    def increment_retry(self, item_id: int):
        """增加重试计数"""
        self.conn.execute(
            "UPDATE items SET retry_count = retry_count + 1 WHERE id = ?",
            (item_id,),
        )
        self.conn.commit()

    def get_retry_count(self, item_id: int) -> int:
        row = self.conn.execute(
            "SELECT retry_count FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return row["retry_count"] if row else 0

    def item_exists(self, item_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return row is not None

    # ── 统计 ───────────────────────────────────────────

    def get_stats(self) -> dict:
        """获取统计信息"""
        stats = {}
        for status in ["pending", "downloaded", "separated", "failed"]:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM items WHERE status = ?", (status,)
            ).fetchone()
            stats[status] = row["cnt"] if row else 0
        stats["total"] = sum(stats.values())
        return stats

    # ── Pipeline State ──────────────────────────────────

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM pipeline_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO pipeline_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def reset_stale(self):
        """将卡在 downloading/separating 状态的项目重置为上一状态"""
        self.conn.execute(
            """UPDATE items SET status = 'pending'
               WHERE status IN ('downloading', 'separating')"""
        )
        self.conn.commit()
