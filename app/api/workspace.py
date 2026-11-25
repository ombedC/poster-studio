"""工作区：轻量身份（口令 / 昵称）与自建团队。"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.errors import AppError, Conflict, ValidationFailed
from app.services import workspace
from app.services.workspace import Workspace

router = APIRouter(prefix="/api/workspace", tags=["Workspace"])

_Name = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=40)
    team_id: str | None = Field(None, max_length=40)


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: _Name



def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_url = urlsplit(origin)
    if (origin_url.scheme, origin_url.netloc) != (request.url.scheme, request.url.netloc):
        raise HTTPException(403, detail="工作区接口仅接受同源页面请求")


def current_workspace(request: Request) -> Workspace:
    """所有接口共用的身份解析：缺省落到 u_guest，非法口令直接 422。"""
    try:
        return workspace.resolve(
            request.headers.get("X-User-Id"), request.headers.get("X-Team-Id")
        )
    except ValidationFailed as exc:
        raise HTTPException(422, detail=exc.message) from None


async def _body(request: Request, model):
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise HTTPException(422, detail="请求参数不合法") from None


def _payload(ws: Workspace) -> dict:
    user = workspace.get_user(ws.user_id) or {}
    return {
        "user_id": ws.user_id,
        "name": user.get("name") or "",
        "team_id": user.get("team_id") or "",
        "teams": workspace.list_teams(),
        "is_guest": ws.is_guest,
    }


@router.get("/me")
async def me(request: Request):
    _same_origin(request)
    return _payload(current_workspace(request))


@router.put("/me")
async def update_me(request: Request):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, ProfileUpdate)
    try:
        workspace.update_profile(ws.user_id, name=payload.name, team_id=payload.team_id)
    except (ValidationFailed, Conflict) as exc:
        raise HTTPException(exc.status_code, detail=exc.message) from None
    user = workspace.get_user(ws.user_id) or {}
    return _payload(Workspace(ws.user_id, user.get("team_id") or "", user.get("name") or ""))


@router.get("/teams")
async def teams(request: Request):
    _same_origin(request)
    return {"teams": workspace.list_teams()}


@router.post("/teams", status_code=201)
async def create_team(request: Request):
    _same_origin(request)
    ws = current_workspace(request)
    payload = await _body(request, TeamCreate)
    try:
        team = workspace.create_team(payload.name, ws.user_id)
    except (ValidationFailed, Conflict) as exc:
        raise HTTPException(exc.status_code, detail=exc.message) from None
    workspace.update_profile(ws.user_id, team_id=team["id"])
    data = _payload(Workspace(ws.user_id, team["id"], ""))
    data["team"] = team
    return data
