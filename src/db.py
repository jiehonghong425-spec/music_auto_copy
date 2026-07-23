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

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    scraper_type TEXT DEFAULT 'audiojungle',
    enabled INTEGER DEFAULT 1,
    config TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_type TEXT DEFAULT 'audio',
    size INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    item_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (item_id) REFERENCES items(id)
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
        # 加速查询的索引
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_title ON items(title)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_uploaded_status ON uploaded_files(status)")
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

    def insert_uploaded_item(self, title: str, author: str = "", file_path: str = "") -> int:
        """插入上传的文件项目（自动生成负数 ID，状态为 pending 加入队列）"""
        row = self.conn.execute(
            "SELECT MIN(id) as min_id FROM items WHERE id < 0"
        ).fetchone()
        new_id = (row["min_id"] or 0) - 1

        self.conn.execute(
            """INSERT INTO items (id, title, author, download_path, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (new_id, title, author, file_path),
        )
        self.conn.commit()
        return new_id

    def get_item(self, item_id: int) -> dict | None:
        """获取单个项目"""
        row = self.conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None

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

    # ── 站点管理 ──────────────────────────────────────

    def add_site(self, name: str, url: str, scraper_type: str = "audiojungle", config: dict | None = None) -> int:
        """添加爬虫站点，返回新 ID"""
        cur = self.conn.execute(
            """INSERT INTO sites (name, url, scraper_type, config)
               VALUES (?, ?, ?, ?)""",
            (name, url, scraper_type, json.dumps(config or {})),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_sites(self) -> list[dict]:
        """列出所有站点"""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM sites ORDER BY id"
            ).fetchall()
        ]

    def delete_site(self, site_id: int) -> bool:
        """删除站点"""
        self.conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        self.conn.commit()
        return self.conn.total_changes > 0

    def update_site(self, site_id: int, **kwargs):
        """更新站点字段"""
        allowed = {"name", "url", "scraper_type", "enabled", "config"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if "config" in updates and isinstance(updates["config"], dict):
            updates["config"] = json.dumps(updates["config"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [site_id]
        self.conn.execute(f"UPDATE sites SET {set_clause} WHERE id = ?", values)
        self.conn.commit()

    # ── 上传文件管理 ──────────────────────────────────

    def add_uploaded_file(self, original_name: str, stored_path: str, file_type: str = "audio", size: int = 0, item_id: int | None = None) -> int:
        """记录上传文件，返回 ID"""
        cur = self.conn.execute(
            """INSERT INTO uploaded_files (original_name, stored_path, file_type, size, item_id)
               VALUES (?, ?, ?, ?, ?)""",
            (original_name, stored_path, file_type, size, item_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_uploaded_files(self, limit: int = 50) -> list[dict]:
        """获取上传文件列表"""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM uploaded_files ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    def update_uploaded_status(self, upload_id: int, status: str):
        """更新上传文件状态"""
        self.conn.execute(
            "UPDATE uploaded_files SET status = ? WHERE id = ?",
            (status, upload_id),
        )
        self.conn.commit()

    # ── 搜索 ──────────────────────────────────────────

    def fuzzy_search(self, keyword: str, limit: int = 20) -> list[dict]:
        """模糊搜索 — 标题和作者，支持中英文部分匹配"""
        keyword = keyword.strip()
        if not keyword:
            return []

        # 分词：按空格拆分，中文逐字补充
        words = keyword.split()
        if len(words) == 1 and len(keyword) >= 2:
            words = [keyword] + list(keyword)

        # 构建评分表达式 (SELECT 部分)
        score_parts = []
        score_params = []
        for w in words:
            score_parts.append(
                "CASE WHEN title LIKE ? COLLATE NOCASE THEN 5 "
                "WHEN author LIKE ? COLLATE NOCASE THEN 2 ELSE 0 END"
            )
            score_params.extend([f"%{w}%", f"%{w}%"])
        score_expr = " + ".join(score_parts)

        # 构建 WHERE 子句
        where_parts = []
        where_params = []
        for w in words:
            where_parts.append("(title LIKE ? COLLATE NOCASE OR author LIKE ? COLLATE NOCASE)")
            where_params.extend([f"%{w}%", f"%{w}%"])
        where_str = " OR ".join(where_parts)

        # 参数顺序：先 SELECT 中的 ?，再 WHERE 中的 ?
        query = f"""
            SELECT *, ({score_expr}) as score
            FROM items
            WHERE ({where_str})
            ORDER BY score DESC, id ASC
            LIMIT ?
        """
        all_params = score_params + where_params + [limit]

        return [
            dict(row)
            for row in self.conn.execute(query, all_params).fetchall()
        ]

    def get_all_items(self, status_filter: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        """获取所有项目（全字段），可按状态筛选"""
        if status_filter:
            return [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM items WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (status_filter, limit, offset),
                ).fetchall()
            ]
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM items ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        ]

    def get_all_items_light(self, status_filter: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        """获取所有项目（含文件路径，供前端直接下载）"""
        fields = "id, title, author, status, error_message, download_path, instrumental_path, vocals_path, preview_url"
        if status_filter:
            return [
                dict(row)
                for row in self.conn.execute(
                    f"SELECT {fields} FROM items WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (status_filter, limit, offset),
                ).fetchall()
            ]
        return [
            dict(row)
            for row in self.conn.execute(
                f"SELECT {fields} FROM items ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        ]
