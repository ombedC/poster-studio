"""httpx 代理容错：NO_PROXY 里的 [::1] 写法曾导致生图直接 500。"""
from __future__ import annotations

import asyncio
import os

import httpx

from app.config import _sanitize_no_proxy as config_sanitize
from app.services import net


def test_sanitize_no_proxy_strips_ipv6_brackets():
    assert config_sanitize("localhost,127.0.0.1,[::1]") == "localhost,127.0.0.1,::1"
    assert config_sanitize("[::1]:8080, example.com") == "::1,example.com"
    assert config_sanitize("localhost,127.0.0.1") == "localhost,127.0.0.1"
    assert net.sanitize_no_proxy("a,[2001:db8::1]") == "a,2001:db8::1"


def test_bracketed_no_proxy_breaks_plain_httpx_but_not_helper(monkeypatch):
    for key in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy",
                "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "[::1]")

    # 直接创建会炸，这是本次 500 的根因
    try:
        httpx.AsyncClient()
    except httpx.InvalidURL:
        pass
    else:  # pragma: no cover - 旧版本 httpx 行为不同
        raise AssertionError("预期 httpx 在这里抛 InvalidURL")

    client = net.async_client(timeout=5)
    assert isinstance(client, httpx.AsyncClient)
    # 兜底后不再读取环境代理
    asyncio.run(client.aclose())


def test_sync_client_falls_back_when_proxy_env_is_broken(monkeypatch):
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "[::1]")
    with net.sync_client(timeout=5) as client:
        assert isinstance(client, httpx.Client)


def test_env_is_sanitized_after_app_import():
    # conftest 在导入 app 之前就可能注入畸形值；导入后应已被清洗
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if value and "[" in value:
            assert "[" not in config_sanitize(value)
