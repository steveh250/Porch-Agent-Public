"""Shared test helpers: a config, and a chat client that follows a script."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)

from porch_light.config import load_config

AGENT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def config(tmp_path, monkeypatch):
    """The shipped example config, with logs and caches under tmp_path."""
    for name in ("OPENROUTER_API_KEY", "OWM_API_KEY", "MQTT_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    shutil.copy(AGENT_DIR / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(AGENT_DIR / "policy.md", tmp_path / "policy.md")
    return load_config(tmp_path / "config.yaml", env_file=tmp_path / "missing.env")


def call(name: str, **arguments: Any) -> Content:
    call.counter += 1  # type: ignore[attr-defined]
    return Content.from_function_call(f"call_{call.counter}", name, arguments=arguments)  # type: ignore[attr-defined]


call.counter = 0  # type: ignore[attr-defined]


class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """Returns one scripted assistant turn per model round trip.

    Each script entry is a Content (tool call or text) or a list of them. The
    real function-invocation loop runs, so tools and middleware execute for real.
    """

    def __init__(self, script: list[Any]) -> None:
        super().__init__()
        self.script = list(script)
        self.seen: list[list[Message]] = []
        self.offered_tools: list[list[str]] = []

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.seen.append(list(messages))
        self.offered_tools.append([getattr(t, "name", str(t)) for t in options.get("tools") or []])
        if not self.script:
            raise RuntimeError("script exhausted")
        turn = self.script.pop(0)
        contents = turn if isinstance(turn, list) else [turn]
        return ChatResponse(messages=Message("assistant", contents))

    def _inner_get_streaming_response(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def tool_results(client: ScriptedChatClient) -> list[str]:
    """Text of every tool result the model was shown, in order."""
    out: list[str] = []
    for message in client.seen[-1]:
        for content in message.contents:
            if content.type == "function_result":
                out.append(str(content.result))
    return out
