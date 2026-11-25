"""SQLite 持久化底座：WAL、建表、存量数据补录。

设计：
* 单文件 ``data/app.db``，每次操作短连接，靠 WAL 支持并发读；
* 建表与迁移全部幂等，升级不破坏已有数据；
* 启动时把磁盘上已有的 SVG 模板与 Seedream 项目补录进库（scope=public）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
GUEST_USER = "u_guest"
LEGACY_USER = "u_legacy"


def db_path() -> Path:
    path = settings.data_dir / "app.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path()), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    team_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_name TEXT NOT NULL DEFAULT '',
    team_id TEXT DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public', 'team', 'private')),
    title TEXT NOT NULL DEFAULT '',
    raw_input TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    channel_id TEXT DEFAULT '',
    intent_id TEXT DEFAULT '',
    style_id TEXT DEFAULT '',
    size TEXT DEFAULT '',
    versions_json TEXT NOT NULL DEFAULT '[]',
    cover_url TEXT NOT NULL DEFAULT '',
    forked_from TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template_meta (
    template_id TEXT PRIMARY KEY,
    owner_id TEXT DEFAULT '',
    team_id TEXT DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'public' CHECK (scope IN ('public', 'team', 'private')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gen_owner ON generations(owner_id);
CREATE INDEX IF NOT EXISTS idx_gen_team ON generations(team_id);
CREATE INDEX IF NOT EXISTS idx_gen_scope ON generations(scope, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tpl_owner ON template_meta(owner_id);
CREATE INDEX IF NOT EXISTS idx_tpl_scope ON template_meta(scope, created_at DESC);
"""


def init() -> None:
    """建库建表 + 存量数据补录（幂等，可反复调用）。"""
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] != SCHEMA_VERSION:
            logger.info("数据库结构版本 %s → %s", row["version"], SCHEMA_VERSION)
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    sync_templates()
    import_seedream_projects()


# ---------------------------------------------------------------------------
# 存量数据补录
# ---------------------------------------------------------------------------
def sync_templates() -> int:
    """把 data/templates 里还没有归属的模板补录为 public。"""
    directory = settings.template_dir
    if not directory.is_dir():
        return 0
    names = [path.name[: -len(".fixed.svg")] for path in directory.glob("*.fixed.svg")]
    added = 0
    stamp = now()
    with connect() as conn:
        for name in names:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO template_meta(template_id, owner_id, team_id, scope, created_at, updated_at)"
                " VALUES (?, '', '', 'public', ?, ?)",
                (name, stamp, stamp),
            )
            added += cursor.rowcount
    if added:
        logger.info("已补录 %d 个存量模板可见范围（public）", added)
    return added


def import_seedream_projects() -> int:
    """把 data/outputs/seedream/<id>/meta.json 导入 generations（u_legacy / public）。"""
    root = settings.seedream_project_dir
    if not root.is_dir():
        return 0
    added = 0
    stamp = now()
    with connect() as conn:
        for meta_path in root.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            project_id = str(meta.get("id") or meta_path.parent.name)
            versions = meta.get("versions") or []
            prompt = str((versions[0].get("prompt") if versions else "") or "")
            cover = str((versions[-1].get("url") if versions else "") or "")
            title = (prompt or "历史作品").strip()[:24] or "历史作品"
            cursor = conn.execute(
                "INSERT OR IGNORE INTO generations(id, owner_id, owner_name, team_id, scope, title,"
                " raw_input, prompt, versions_json, cover_url, created_at, updated_at)"
                " VALUES (?, ?, ?, '', 'public', ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    LEGACY_USER,
                    "历史数据",
                    title,
                    prompt,
                    prompt,
                    json.dumps(versions, ensure_ascii=False),
                    cover,
                    str(meta.get("created_at") or stamp),
                    stamp,
                ),
            )
            added += cursor.rowcount
    if added:
        logger.info("已导入 %d 个历史 Seedream 项目（u_legacy / public）", added)
    return added
