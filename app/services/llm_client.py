"""OpenAI 兼容的大模型客户端。

只做三件事：判断是否配置、发多模态/纯文本请求、把返回值稳妥地解析成 JSON。

配置来源（见 ``config.py``）：``LLM_API_KEY`` / ``LLM_BASE_URL`` / ``LLM_MODEL``，
缺省时兼容 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``。未配置时调用方应降级到
启发式解析，而不是直接报错。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import uuid
from typing import Any

from app.config import settings
from app.errors import LLMUnavailable

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_SESSION_ID = os.getenv("LLM_SESSION_ID") or f"poster-workbench-{uuid.uuid4().hex}"


def llm_configured() -> bool:
    return bool(settings.llm_api_key and settings.llm_model)


def _extract_json(content: str) -> dict[str, Any]:
    text = _FENCE_RE.sub("", (content or "").strip())
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise LLMUnavailable(f"大模型没有返回 JSON：{(content or '')[:200]}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"大模型返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMUnavailable("大模型返回的 JSON 不是对象")
    return data


def _client():
    from openai import OpenAI

    return OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
        max_retries=1,
        default_headers={
            # OpenCode Go 要求稳定的会话标识，否则返回 400
            "x-opencode-session": _SESSION_ID,
            "User-Agent": "jiaopei-poster-workbench/0.4",
        },
    )


def chat_json(
    system: str,
    user: str,
    *,
    image_png: bytes | None = None,
    image_detail: str = "low",
    temperature: float = 0.1,
    max_tokens: int = 3000,
    model: str | None = None,
) -> dict[str, Any]:
    """发一次 chat 请求并返回 JSON 对象。

    * 配了 ``image_png`` 且 ``LLM_VISION=true`` 时按多模态消息发送；
    * 服务不支持 ``response_format`` 或多模态时自动降级重试。
    """
    if not llm_configured():
        raise LLMUnavailable(
            "未配置大模型：请设置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（或 OPENAI_*）"
        )

    text_content = user
    image_content: list[dict[str, Any]] | None = None
    if image_png and settings.llm_vision:
        data_uri = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
        image_content = [
            {"type": "text", "text": user},
            {"type": "image_url", "image_url": {"url": data_uri, "detail": image_detail or "low"}},
        ]

    attempts: list[tuple[bool, bool]] = []
    if image_content is not None:
        attempts.append((True, settings.llm_json_mode))
        attempts.append((False, settings.llm_json_mode))
    attempts.append((False, False))
    # 去重且保序
    seen: set[tuple[bool, bool]] = set()
    ordered = [item for item in attempts if not (item in seen or seen.add(item))]

    last_error: Exception | None = None
    for use_image, use_json_mode in ordered:
        content: Any = image_content if use_image else text_content
        kwargs: dict[str, Any] = {
            "model": model or settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # 网关偶尔返回空 content（尤其推理模型），同一组参数重试一次即可恢复
        for attempt in range(2):
            try:
                response = _client().chat.completions.create(**kwargs)
                message = response.choices[0].message
                raw = (message.content or "").strip()
                if not raw:
                    last_error = LLMUnavailable("大模型返回空内容（网关偶发），请稍后重试")
                    logger.warning(
                        "LLM 返回空内容（vision=%s json_mode=%s attempt=%s）",
                        use_image, use_json_mode, attempt + 1,
                    )
                    continue
                return _extract_json(raw)
            except LLMUnavailable as exc:
                # 某些兼容服务在 json_mode 下会返回空 content，继续尝试不带 json_mode
                last_error = exc
                logger.warning("LLM 返回无法解析（vision=%s json_mode=%s）：%s", use_image, use_json_mode, exc)
                break
            except Exception as exc:  # 网络、鉴权、模型能力等统一降级重试
                last_error = exc
                logger.warning(
                    "LLM 调用失败（vision=%s json_mode=%s）：%s", use_image, use_json_mode, exc
                )
                break

    raise LLMUnavailable(f"大模型调用失败：{last_error}") from last_error
