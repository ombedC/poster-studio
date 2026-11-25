"""LLM 客户端：空内容重试与 JSON 提取。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import llm_client


class _Message:
    def __init__(self, content):
        self.content = content


class _Client:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        content = self.contents.pop(0) if self.contents else ""
        return SimpleNamespace(choices=[SimpleNamespace(message=_Message(content))])


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "llm_model", "test-model", raising=False)
    monkeypatch.setattr(settings, "llm_json_mode", False, raising=False)
    monkeypatch.setattr(llm_client, "llm_configured", lambda: True)


def test_empty_content_is_retried_once(monkeypatch, configured):
    client = _Client(["", '{"prompt": "恢复成功"}'])
    monkeypatch.setattr(llm_client, "_client", lambda: client)
    data = llm_client.chat_json("sys", "user", max_tokens=100)
    assert data == {"prompt": "恢复成功"}
    assert client.calls == 2


def test_blank_twice_reports_empty_content(monkeypatch, configured):
    client = _Client(["   ", ""])
    monkeypatch.setattr(llm_client, "_client", lambda: client)
    with pytest.raises(llm_client.LLMUnavailable) as exc:
        llm_client.chat_json("sys", "user", max_tokens=100)
    assert "空内容" in str(exc.value)
