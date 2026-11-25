"""轻量身份与自建团队。

身份完全由请求头驱动：``X-User-Id``（ASCII 口令）与 ``X-Team-Id``。
没有头时落到 ``u_guest``，保证旧书签与既有自动化脚本零破坏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.services import db

USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
TEAM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
SCOPES = ("public", "team", "private")


@dataclass(frozen=True)
class Workspace:
    user_id: str
    team_id: str = ""
    name: str = ""

    @property
    def is_guest(self) -> bool:
        return self.user_id == db.GUEST_USER


def validate_user_id(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return db.GUEST_USER
    if not USER_ID_RE.match(text):
        raise ValidationFailed("用户口令只能包含字母、数字、下划线和短横线（4-40 位）")
    return text


def resolve(x_user_id: str | None, x_team_id: str | None) -> Workspace:
    user_id = validate_user_id(x_user_id)
    team_id = (x_team_id or "").strip()
    if team_id and not TEAM_ID_RE.match(team_id):
        raise ValidationFailed("团队标识不合法")
    ensure_user(user_id)
    profile = get_user(user_id) or {}
    if not team_id:
        # 没带头时沿用用户上次选择的团队，前端刷新/换设备都能接上
        team_id = str(profile.get("team_id") or "")
    if team_id and not team_exists(team_id):
        team_id = ""  # 团队被删或来自旧缓存：静默回落到无团队，不阻断主流程
    return Workspace(user_id=user_id, team_id=team_id, name=str(profile.get("name") or ""))


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------
def ensure_user(user_id: str) -> None:
    stamp = db.now()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id, name, team_id, created_at, updated_at)"
            " VALUES (?, '', '', ?, ?)",
            (user_id, stamp, stamp),
        )


def get_user(user_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def update_profile(user_id: str, *, name: str | None = None, team_id: str | None = None) -> dict[str, Any]:
    ensure_user(user_id)
    fields: list[str] = []
    values: list[Any] = []
    if name is not None:
        cleaned = name.strip()
        if len(cleaned) > 40:
            raise ValidationFailed("昵称最长 40 个字符")
        fields.append("name = ?")
        values.append(cleaned)
    if team_id is not None:
        clean_team = team_id.strip()
        if clean_team and not team_exists(clean_team):
            raise ValidationFailed("团队不存在")
        fields.append("team_id = ?")
        values.append(clean_team)
    if not fields:
        return get_user(user_id) or {}
    fields.append("updated_at = ?")
    values.append(db.now())
    values.append(user_id)
    with db.connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    return get_user(user_id) or {}


# ---------------------------------------------------------------------------
# 团队
# ---------------------------------------------------------------------------
@lru_cache(maxsize=200)
def _team_cached(team_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute("SELECT 1 FROM teams WHERE id = ?", (team_id,)).fetchone()
    return row is not None


def team_exists(team_id: str) -> bool:
    return _team_cached(team_id)


def clear_team_cache() -> None:
    _team_cached.cache_clear()


def list_teams() -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name, t.created_by, t.created_at,"
            " (SELECT COUNT(*) FROM generations g WHERE g.team_id = t.id) AS generations,"
            " (SELECT COUNT(DISTINCT g.owner_id) FROM generations g WHERE g.team_id = t.id) AS members"
            " FROM teams t ORDER BY t.created_at ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def create_team(name: str, created_by: str) -> dict[str, Any]:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationFailed("团队名称不能为空")
    if len(cleaned) > 40:
        raise ValidationFailed("团队名称最长 40 个字符")
    team_id = "t_" + __import__("uuid").uuid4().hex[:8]
    stamp = db.now()
    with db.connect() as conn:
        exists = conn.execute("SELECT 1 FROM teams WHERE lower(name) = lower(?)", (cleaned,)).fetchone()
        if exists:
            raise Conflict(f"团队「{cleaned}」已存在，直接加入即可")
        conn.execute(
            "INSERT INTO teams(id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (team_id, cleaned, created_by, stamp),
        )
    clear_team_cache()
    return {"id": team_id, "name": cleaned, "created_by": created_by, "created_at": stamp}


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------
def can_view(*, scope: str, owner_id: str, team_id: str, ws: Workspace) -> bool:
    if owner_id == ws.user_id:
        return True
    if scope == "public":
        return True
    if scope == "team":
        return bool(ws.team_id) and team_id == ws.team_id
    return False


def require_view(*, scope: str, owner_id: str, team_id: str, ws: Workspace) -> None:
    if not can_view(scope=scope, owner_id=owner_id, team_id=team_id, ws=ws):
        raise NotFound("记录不存在")


def require_owner(owner_id: str, ws: Workspace, message: str = "只有作者可以执行该操作") -> None:
    if owner_id != ws.user_id:
        raise Forbidden(message)
