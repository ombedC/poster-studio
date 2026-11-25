"""Seedream 生图记录（generations）读写与可见性过滤。"""

from __future__ import annotations

import json
from typing import Any

from app.errors import NotFound
from app.services import db, workspace
from app.services.workspace import Workspace


def _title_of(*texts: str) -> str:
    for text in texts:
        cleaned = str(text or "").strip().replace("\n", " ")
        if cleaned:
            return cleaned[:24]
    return "未命名作品"


def _normalize_versions(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, version in enumerate(versions, start=1):
        item = dict(version)
        item.setdefault("version", item.get("index", index))
        item.setdefault("action", item.get("kind", "generate"))
        item.setdefault("instruction", item.get("prompt", ""))
        normalized.append(item)
    return normalized


def public_item(row: dict[str, Any], viewer: Workspace) -> dict[str, Any]:
    versions = json.loads(row.get("versions_json") or "[]")
    latest = versions[-1] if versions else {}
    return {
        "generation_id": row["id"],
        "title": row["title"],
        "prompt": row["prompt"],
        "raw_input": row["raw_input"],
        "channel_id": row["channel_id"],
        "intent_id": row["intent_id"],
        "style_id": row["style_id"],
        "size": row["size"],
        "scope": row["scope"],
        "owner_id": row["owner_id"],
        "owner_name": row["owner_name"],
        "team_id": row["team_id"],
        "cover_url": row["cover_url"],
        "versions": versions,
        "version_count": len(versions),
        "latest_version": latest.get("version"),
        "forked_from": row["forked_from"],
        "owned": row["owner_id"] == viewer.user_id,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_row(generation_id: str) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM generations WHERE id = ?", (generation_id,)).fetchone()
    return dict(row) if row else None


def get_visible(generation_id: str, ws: Workspace) -> dict[str, Any]:
    row = get_row(generation_id)
    if row is None:
        raise NotFound("记录不存在")
    workspace.require_view(
        scope=row["scope"], owner_id=row["owner_id"], team_id=row["team_id"] or "", ws=ws
    )
    return row


def record_project(
    *,
    generation_id: str,
    ws: Workspace,
    versions: list[dict[str, Any]],
    prompt: str = "",
    raw_input: str = "",
    channel_id: str = "",
    intent_id: str = "",
    style_id: str = "",
    size: str = "",
    forked_from: str = "",
    scope: str = "public",
    title: str = "",
) -> dict[str, Any]:
    """写入/更新一条生图记录；已有记录只更新版本链与封面，不覆盖作者。"""
    stamp = db.now()
    normalized = _normalize_versions(versions)
    cover = normalized[-1].get("url", "") if normalized else ""
    payload = json.dumps(normalized, ensure_ascii=False)
    existing = get_row(generation_id)
    with db.connect() as conn:
        if existing:
            conn.execute(
                "UPDATE generations SET versions_json = ?, cover_url = ?, prompt = COALESCE(NULLIF(?, ''), prompt),"
                " updated_at = ?, forked_from = COALESCE(NULLIF(?, ''), forked_from) WHERE id = ?",
                (payload, cover, prompt or "", stamp, forked_from, generation_id),
            )
        else:
            conn.execute(
                "INSERT INTO generations(id, owner_id, owner_name, team_id, scope, title, raw_input,"
                " prompt, channel_id, intent_id, style_id, size, versions_json, cover_url,"
                " forked_from, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    ws.user_id,
                    ws.name or ws.user_id,
                    ws.team_id,
                    scope if scope in workspace.SCOPES else "public",
                    title or _title_of(raw_input, prompt),
                    raw_input,
                    prompt,
                    channel_id,
                    intent_id,
                    style_id,
                    size,
                    payload,
                    cover,
                    forked_from,
                    stamp,
                    stamp,
                ),
            )
    row = get_row(generation_id)
    assert row is not None
    return public_item(row, ws)


def list_visible(
    ws: Workspace,
    *,
    view: str = "mine",
    query: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if view == "team":
        if not ws.team_id:
            return []
        where.append("team_id = ? AND scope IN ('team', 'public')")
        params.append(ws.team_id)
    elif view == "public":
        where.append("scope = 'public'")
    else:
        where.append("owner_id = ?")
        params.append(ws.user_id)
    if query.strip():
        like = f"%{query.strip()}%"
        where.append("(title LIKE ? OR prompt LIKE ? OR raw_input LIKE ?)")
        params.extend([like, like, like])
    sql = "SELECT * FROM generations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ? OFFSET ?"
    params.extend([max(1, min(200, int(limit))), max(0, int(offset))])
    with db.connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    return [public_item(row, ws) for row in rows]


def update(generation_id: str, ws: Workspace, *, title: str | None = None, scope: str | None = None) -> dict[str, Any]:
    row = get_visible(generation_id, ws)
    workspace.require_owner(row["owner_id"], ws)
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        cleaned = title.strip()
        if not cleaned:
            raise ValueError("标题不能为空")
        fields.append("title = ?")
        values.append(cleaned[:60])
    if scope is not None:
        if scope not in workspace.SCOPES:
            raise ValueError("可见范围只能是 public / team / private")
        fields.append("scope = ?")
        values.append(scope)
    if not fields:
        return public_item(row, ws)
    fields.append("updated_at = ?")
    values.append(db.now())
    values.append(generation_id)
    with db.connect() as conn:
        conn.execute(f"UPDATE generations SET {', '.join(fields)} WHERE id = ?", values)
    updated = get_row(generation_id)
    assert updated is not None
    return public_item(updated, ws)


def delete(generation_id: str, ws: Workspace) -> None:
    row = get_visible(generation_id, ws)
    workspace.require_owner(row["owner_id"], ws, "只有作者可以删除该作品")
    with db.connect() as conn:
        conn.execute("DELETE FROM generations WHERE id = ?", (generation_id,))
