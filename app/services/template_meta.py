"""SVG 模板归属与可见范围（template_meta）。"""

from __future__ import annotations

from typing import Any

from app.errors import Forbidden, NotFound
from app.services import db, workspace
from app.services.workspace import Workspace


def ensure(template_id: str, *, owner_id: str = "", team_id: str = "", scope: str = "public") -> None:
    stamp = db.now()
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO template_meta(template_id, owner_id, team_id, scope, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (template_id, owner_id, team_id, scope if scope in workspace.SCOPES else "public", stamp, stamp),
        )


def get(template_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM template_meta WHERE template_id = ?", (template_id,)).fetchone()
    return dict(row) if row else None


def metadata_map() -> dict[str, dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM template_meta").fetchall()
    return {row["template_id"]: dict(row) for row in rows}


def visible(meta: dict[str, Any] | None, ws: Workspace) -> bool:
    if not meta:
        return True  # 没入表的按 public 处理，绝不因缺数据而隐藏模板
    return workspace.can_view(
        scope=meta.get("scope") or "public",
        owner_id=meta.get("owner_id") or "",
        team_id=meta.get("team_id") or "",
        ws=ws,
    )


def update_scope(template_id: str, ws: Workspace, scope: str) -> dict[str, Any]:
    if scope not in workspace.SCOPES:
        raise ValueError("可见范围只能是 public / team / private")
    meta = get(template_id)
    if meta is None:
        raise NotFound("模板不存在")
    if (meta.get("owner_id") or "") != ws.user_id:
        raise Forbidden("只有模板作者可以修改可见范围")
    with db.connect() as conn:
        conn.execute(
            "UPDATE template_meta SET scope = ?, updated_at = ? WHERE template_id = ?",
            (scope, db.now(), template_id),
        )
    return get(template_id) or {}


def require_owner(template_id: str, ws: Workspace, message: str = "只有模板作者可以执行该操作") -> None:
    meta = get(template_id)
    if meta is not None and (meta.get("owner_id") or "") != ws.user_id:
        raise Forbidden(message)
