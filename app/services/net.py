"""httpx 客户端容错封装。

Windows 上 ``NO_PROXY`` 常被写成 ``localhost,127.0.0.1,[::1]``，httpx 解析带方括号的
IPv6 会直接抛 ``InvalidURL: Invalid port``，连客户端都创建不出来。这里统一兜底：
先按环境变量创建，失败就退回不读环境代理，保证业务请求不会因为一个畸形变量而 500。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BRACKETED = re.compile(r"^\[([0-9A-Fa-f:.]+)\](?::\d+)?$")


def sanitize_no_proxy(value: str) -> str:
    """把 ``[::1]`` / ``[::1]:8080`` 改写成 httpx 能接受的 ``::1``。"""
    tokens: list[str] = []
    for raw in re.split(r"[,\s]+", (value or "").strip()):
        if not raw:
            continue
        match = _BRACKETED.fullmatch(raw)
        tokens.append(match.group(1) if match else raw)
    return ",".join(tokens)


def _fallback(exc: Exception) -> None:
    logger.warning("环境代理配置不可用（%s），本次请求改为直连", exc)


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    try:
        return httpx.AsyncClient(**kwargs)
    except httpx.InvalidURL as exc:
        _fallback(exc)
        kwargs.pop("trust_env", None)
        return httpx.AsyncClient(trust_env=False, **kwargs)


def sync_client(**kwargs: Any) -> httpx.Client:
    try:
        return httpx.Client(**kwargs)
    except httpx.InvalidURL as exc:
        _fallback(exc)
        kwargs.pop("trust_env", None)
        return httpx.Client(trust_env=False, **kwargs)
