"""工作区身份、SQLite 持久化、生图历史与模板三级可见范围的回归。"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from app.config import settings
from app.errors import Forbidden
from app.services import db, generations, seedream, template_import, template_meta, workspace


def png(width: int = 120, height: int = 90) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def data_url(raw: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(raw).decode()


@pytest.fixture
def ark(monkeypatch):
    """让 generate/edit 走假的方舟：不联网、返回固定图片。"""
    async def fake_post(payload):
        return {"data": [{"url": "https://ark.example/out.png"}]}

    async def fake_download(url):
        return png()

    monkeypatch.setattr(seedream, "_post", fake_post)
    monkeypatch.setattr(seedream, "_download", fake_download)


def headers(user: str, team: str = "") -> dict[str, str]:
    data = {"X-User-Id": user}
    if team:
        data["X-Team-Id"] = team
    return data


# ---------------------------------------------------------------------------
# 1. SQLite 底座与团队
# ---------------------------------------------------------------------------
def test_sqlite_wal_and_schema(client):
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert mode.lower() == "wal"
    assert {"teams", "users", "generations", "template_meta", "schema_version"} <= tables


def test_team_create_and_duplicate_name(client):
    created = client.post("/api/workspace/teams", json={"name": " 秋季教研团队 "}, headers=headers("u_alpha"))
    assert created.status_code == 201
    body = created.json()
    assert body["team"]["name"] == "秋季教研团队"
    assert body["team_id"] == body["team"]["id"]

    duplicate = client.post("/api/workspace/teams", json={"name": "秋季教研团队"}, headers=headers("u_beta"))
    assert duplicate.status_code == 409

    listed = client.get("/api/workspace/teams").json()["teams"]
    assert any(team["name"] == "秋季教研团队" for team in listed)


def test_invalid_user_id_rejected(client):
    response = client.get("/api/workspace/me", headers={"X-User-Id": "ab"})
    assert response.status_code == 422
    # 含中文的口令在浏览器侧就会被拦截（httpx 连头都构造不出来），这里只验格式规则
    assert not workspace.USER_ID_RE.match("中文口令")


def test_profile_keeps_nickname_and_team(client):
    team = client.post("/api/workspace/teams", json={"name": "显示名团队"}, headers=headers("u_name")).json()
    updated = client.put(
        "/api/workspace/me",
        json={"name": "林老师", "team_id": team["team_id"]},
        headers=headers("u_name"),
    ).json()
    assert updated["name"] == "林老师" and updated["team_id"] == team["team_id"]
    reloaded = client.get("/api/workspace/me", headers=headers("u_name")).json()
    assert reloaded["name"] == "林老师" and reloaded["team_id"] == team["team_id"]


# ---------------------------------------------------------------------------
# 2. 生图记录：默认 public、三种视图、权限与 fork
# ---------------------------------------------------------------------------
def _generate(client, user: str, prompt: str = "秋季研学营") -> str:
    response = client.post(
        "/api/seedream/generate",
        json={"prompt": prompt, "size": "2K", "channel_id": "xhs_cover"},
        headers=headers(user),
    )
    assert response.status_code == 200, response.text
    return response.json()["generation_id"]


def test_generation_defaults_public_and_views_filter(client, ark):
    gen_a = _generate(client, "u_alpha", "甲的作品")
    _generate(client, "u_beta", "乙的作品")

    mine_a = client.get("/api/seedream/history?view=mine", headers=headers("u_alpha")).json()["items"]
    assert [item["generation_id"] for item in mine_a] == [gen_a]
    assert mine_a[0]["scope"] == "public" and mine_a[0]["owned"] is True

    public = client.get("/api/seedream/history?view=public", headers=headers("u_gamma")).json()["items"]
    # 同会话里可能有历史导入（u_legacy）或其它用例的数据，只断言本用例的两位作者都在
    assert {"u_alpha", "u_beta"} <= {item["owner_id"] for item in public}

    # 没有加入任何团队的用户，team 视图为空
    assert client.get("/api/seedream/history?view=team", headers=headers("u_teamless")).json()["items"] == []


def test_team_view_only_exposes_team_and_public(client, ark):
    team = client.post("/api/workspace/teams", json={"name": "团队视图"}, headers=headers("u_alpha")).json()
    team_id = team["team_id"]
    client.put("/api/workspace/me", json={"team_id": team_id}, headers=headers("u_alpha"))

    public_id = _generate(client, "u_alpha", "公开作品")
    private_id = _generate(client, "u_alpha", "私密作品")
    client.patch(
        f"/api/seedream/history/{private_id}", json={"scope": "private"}, headers=headers("u_alpha")
    )

    items = client.get("/api/seedream/history?view=team", headers=headers("u_alpha")).json()["items"]
    ids = {item["generation_id"] for item in items}
    assert public_id in ids and private_id not in ids


def test_non_owner_cannot_patch_or_delete(client, ark):
    gen = _generate(client, "u_alpha")
    patch = client.patch(
        f"/api/seedream/history/{gen}", json={"scope": "private"}, headers=headers("u_beta")
    )
    assert patch.status_code == 403
    delete = client.delete(f"/api/seedream/history/{gen}", headers=headers("u_beta"))
    assert delete.status_code == 403
    assert client.delete(f"/api/seedream/history/{gen}", headers=headers("u_alpha")).status_code == 204


def test_edit_on_foreign_public_record_forks(client, ark):
    original = _generate(client, "u_alpha", "原作者作品")
    before = generations.get_row(original)
    assert before is not None
    before_versions = json.loads(before["versions_json"])

    response = client.post(
        "/api/seedream/edit",
        json={
            "instruction": "改成篮球",
            "base_image": data_url(png()),
            "size": "auto",
            "project_id": original,
        },
        headers=headers("u_beta"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    forked_id = body["generation_id"]
    assert forked_id != original
    assert body["forked_from"] == original

    after = generations.get_row(original)
    assert after is not None
    assert after["owner_id"] == "u_alpha"
    assert json.loads(after["versions_json"]) == before_versions  # 原记录完全没被污染

    fork = generations.get_row(forked_id)
    assert fork is not None and fork["owner_id"] == "u_beta"
    assert len(json.loads(fork["versions_json"])) == len(before_versions) + 1
    assert fork["title"].endswith("复刻")


def test_history_detail_visibility(client, ark):
    gen = _generate(client, "u_alpha")
    client.patch(f"/api/seedream/history/{gen}", json={"scope": "private"}, headers=headers("u_alpha"))
    assert client.get(f"/api/seedream/history/{gen}", headers=headers("u_alpha")).status_code == 200
    assert client.get(f"/api/seedream/history/{gen}", headers=headers("u_beta")).status_code == 404


# ---------------------------------------------------------------------------
# 3. 模板三级可见范围
# ---------------------------------------------------------------------------
def _write_template(name: str) -> None:
    settings.template_dir.mkdir(parents=True, exist_ok=True)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
        '<rect width="200" height="100" fill="#eee"/>'
        '<text id="t" x="10" y="60" font-size="24">测试模板</text></svg>'
    )
    (settings.template_dir / f"{name}.fixed.svg").write_text(svg, encoding="utf-8")


def test_legacy_templates_backfilled_as_public(client):
    _write_template("存量模板")
    assert db.sync_templates() >= 1
    meta = template_meta.get("存量模板")
    assert meta is not None and meta["scope"] == "public"

    items = client.get("/api/v1/posters/templates?view=public", headers=headers("u_alpha")).json()
    entry = next(item for item in items if item["name"] == "存量模板")
    assert entry["scope"] == "public" and entry["owned"] is False
    assert client.get("/api/v1/posters/templates?view=mine", headers=headers("u_alpha")).json() == []


def test_new_template_defaults_public_and_scope_patch(client, monkeypatch):
    _write_template("新模板")
    with db.connect() as conn:  # 清掉可能由启动补录写入的旧行，确保归属确定
        conn.execute("DELETE FROM template_meta WHERE template_id = '新模板'")
    template_meta.ensure("新模板", owner_id="u_alpha")

    mine = client.get("/api/v1/posters/templates?view=mine", headers=headers("u_alpha")).json()
    assert [item["name"] for item in mine] == ["新模板"]
    assert mine[0]["scope"] == "public" and mine[0]["owned"] is True

    denied = client.patch(
        "/api/v1/posters/templates/新模板/visibility", json={"scope": "private"}, headers=headers("u_beta")
    )
    assert denied.status_code == 403

    changed = client.patch(
        "/api/v1/posters/templates/新模板/visibility", json={"scope": "team"}, headers=headers("u_alpha")
    )
    assert changed.status_code == 200 and changed.json()["scope"] == "team"

    public = client.get("/api/v1/posters/templates?view=public", headers=headers("u_beta")).json()
    assert all(item["name"] != "新模板" for item in public)


def test_template_upload_passes_owner_and_scope(client, monkeypatch):
    team = client.post("/api/workspace/teams", json={"name": "上传团队"}, headers=headers("u_alpha")).json()
    captured = {}

    def fake_import(raw, filename, **kwargs):
        captured.update(kwargs)
        return {"name": "上传模板", "analyzed": False}

    monkeypatch.setattr("app.api.posters.import_template", fake_import)
    response = client.post(
        "/api/v1/posters/templates/upload",
        files={"file": ("demo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")},
        data={"scope": "team"},
        headers=headers("u_alpha", team["team_id"]),
    )
    assert response.status_code == 200
    assert captured["owner_id"] == "u_alpha"
    assert captured["team_id"] == team["team_id"]
    assert captured["scope"] == "team"
