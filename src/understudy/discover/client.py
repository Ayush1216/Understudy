"""The model client. The only place in the repo that imports an LLM SDK.

Any OpenAI-compatible chat-completions endpoint with tool calling works; the provider swap is a
base URL + model name. Replay never touches this module (enforced by tests/test_invariants.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import openai

from understudy.schema import Observation

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-2.5-flash"
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelTurn:
    text: str | None
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    async def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


class ConfigurationError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    """KEY=VALUE lines, optional quotes, # comments. Never overrides an existing variable."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (s.strip() for s in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _retryable(e: Exception) -> bool:
    if isinstance(e, openai.APIConnectionError):
        return True
    return isinstance(e, openai.APIStatusError) and (e.status_code == 429 or e.status_code >= 500)


def _retry_after(e: Exception) -> float | None:
    if isinstance(e, openai.APIStatusError):
        try:
            return float(e.response.headers.get("retry-after", ""))
        except ValueError:
            return None
    return None


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, *, max_retries: int = 6, http_client: Any | None = None) -> None:
        # The SDK's own retry is disabled: ours logs each attempt and honours Retry-After.
        self._client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=120, http_client=http_client)
        self.model = model
        self.max_retries = max_retries
        # Some providers reject the parameter outright; remembered after the first rejection.
        self._serial_tool_calls = True

    @classmethod
    def from_env(cls) -> OpenAICompatClient:
        load_env_file(ENV_FILE)
        key = os.environ.get("LLM_API_KEY")
        if not key:
            raise ConfigurationError(
                "LLM_API_KEY is not set. Get a key at https://aistudio.google.com/apikey and put "
                f"LLM_API_KEY=<key> in {ENV_FILE} (or export it). Optional: LLM_BASE_URL, LLM_MODEL."
            )
        return cls(os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL, key, os.environ.get("LLM_MODEL") or DEFAULT_MODEL)

    async def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        kwargs: dict[str, Any] = dict(
            model=self.model, messages=[{"role": "system", "content": system}, *messages],
            tools=tools, tool_choice="auto",
        )
        if self._serial_tool_calls:
            kwargs["parallel_tool_calls"] = False
        attempt = 0
        while True:
            try:
                response = await self._client.chat.completions.create(**kwargs)
                break
            except openai.BadRequestError as e:
                if "parallel_tool_calls" in kwargs and "parallel_tool_calls" in str(e):
                    del kwargs["parallel_tool_calls"]
                    self._serial_tool_calls = False
                    continue
                raise
            except (openai.APIStatusError, openai.APIConnectionError) as e:
                if not _retryable(e) or attempt >= self.max_retries:
                    raise
                after = _retry_after(e)
                delay = (after if after is not None else min(2 ** attempt, 32)) + random.random()
                attempt += 1
                status = getattr(e, "status_code", "connection")
                print(f"llm: {type(e).__name__} ({status}); retry {attempt}/{self.max_retries} in {delay:.1f}s", file=sys.stderr)
                await asyncio.sleep(delay)
        return _parse(response)


def _parse(response: Any) -> ModelTurn:
    raw = response.model_dump(mode="json")
    usage = response.usage
    turn = ModelTurn(
        text=None, tool_calls=[],
        input_tokens=(usage.prompt_tokens or 0) if usage else 0,
        output_tokens=(usage.completion_tokens or 0) if usage else 0, raw=raw,
    )
    if not response.choices:  # a safety block returns no choice; the loop treats it as "no tool call"
        return turn
    message = response.choices[0].message
    turn.text = message.content
    for i, tc in enumerate(message.tool_calls or []):
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        raw_args = fn.arguments or "{}"
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                raise ValueError("not an object")
        except ValueError:
            args = {"_parse_error": raw_args}
        turn.tool_calls.append(ToolCall(id=tc.id or f"call_{i}", name=fn.name, arguments=args))
    return turn


Turn = str | list[ToolCall] | Callable[[Observation | None, list[dict]], "str | list[ToolCall]"]


class ScriptedModelClient:
    """A model with a script instead of a brain, for tests. Each turn is a text reply, a list of
    tool calls, or a callable(latest_observation, messages) that builds them from what the
    page actually shows (refs are minted per observation, so a script cannot know them ahead).
    The loop calls `observed(obs)` after every observation. Running past the script raises."""

    model = "scripted"

    def __init__(self, turns: list[Turn]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.latest_observation: Observation | None = None

    def observed(self, obs: Observation) -> None:
        self.latest_observation = obs

    async def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        if not self._turns:
            raise RuntimeError(f"scripted model exhausted after {len(self.requests) - 1} turns")
        turn = self._turns.pop(0)
        if callable(turn):
            turn = turn(self.latest_observation, messages)
        if isinstance(turn, str):
            return ModelTurn(text=turn, tool_calls=[])
        return ModelTurn(text=None, tool_calls=list(turn))
