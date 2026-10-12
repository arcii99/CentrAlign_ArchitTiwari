"""Model access for the agent — free and local by default, any provider by configuration.

    NOVA_LLM_PROVIDER   ollama (default) | openai      ("openai" = any OpenAI-compatible endpoint)
    NOVA_LLM_MODEL      default qwen3.5:4b
    NOVA_LLM_BASE_URL   default http://127.0.0.1:11434 for ollama
    NOVA_LLM_API_KEY    only for hosted OpenAI-compatible endpoints
    NOVA_LLM_NUM_CTX    ollama context window, default 20480 (the most that keeps qwen3.5:4b fully on a 6 GB GPU)
    NOVA_LLM_KEEP_ALIVE how long ollama keeps the model loaded between calls, default 30m (humans answer slowly)
    NOVA_LLM_OPTIONS    extra ollama sampling options as JSON, e.g. {"presence_penalty": 0}

Messages use one internal shape: {"role", "content", "tool_calls"?: [ToolCall], "tool_name"?, "tool_call_id"?}.
Each provider class converts to its own wire format, so the agent never depends on a vendor.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field

import httpx


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall]
    thinking: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def as_message(self) -> dict:
        return {"role": "assistant", "content": self.content, "tool_calls": self.tool_calls}


class LLMError(RuntimeError):
    pass


def _post(client: httpx.Client, url: str, payload: dict, headers: dict | None = None, retries: int = 2) -> dict:
    for attempt in range(retries + 1):
        try:
            r = client.post(url, json=payload, headers=headers)
            if r.status_code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"{r.status_code} from model server: {r.text[:400]}")
            return r.json()
        except httpx.TransportError as e:
            if attempt == retries:
                raise LLMError(f"model server unreachable at {url}: {e}") from e
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


class Ollama:
    """Ollama's native /api/chat: tool calls, a `think` switch, JSON-schema output and timing stats."""

    def __init__(self, model: str, base_url: str, num_ctx: int = 16384, keep_alive: str = "30m",
                 options: dict | None = None):
        self.model, self.base_url, self.num_ctx = model, base_url.rstrip("/"), num_ctx
        self.keep_alive, self.options = keep_alive, options or {}
        self.client = httpx.Client(timeout=900)

    def ping(self) -> str | None:
        """None if the server answers and has the model; otherwise what is wrong, in words."""
        try:
            r = self.client.get(f"{self.base_url}/api/tags", timeout=5)
        except httpx.TransportError as e:
            return f"the model server (Ollama) is not reachable at {self.base_url} ({e.__class__.__name__}). Is Ollama running?"
        names = {m.get("name") for m in r.json().get("models", [])} if r.status_code == 200 else set()
        if self.model not in names and f"{self.model}:latest" not in names:
            return f"Ollama at {self.base_url} has no model {self.model!r}. Run: ollama pull {self.model}"
        return None

    def _wire(self, m: dict) -> dict:
        out = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in m["tool_calls"]]
        if m["role"] == "tool":
            out["tool_name"] = m.get("tool_name", "")
        if m.get("images"):
            out["images"] = m["images"]
        return out

    def chat(self, messages: list[dict], tools: list[dict] | None = None, schema: dict | None = None,
             think: bool = False, temperature: float = 0.2, max_tokens: int | None = None) -> Reply:
        payload = {"model": self.model, "messages": [self._wire(m) for m in messages], "stream": False,
                   "think": think, "keep_alive": self.keep_alive,
                   "options": {"num_ctx": self.num_ctx, "temperature": temperature, **self.options}}
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        if tools:
            payload["tools"] = tools
        if schema:
            payload["format"] = schema
        started = time.time()
        out = _post(self.client, f"{self.base_url}/api/chat", payload)
        msg = out.get("message", {})
        calls = [ToolCall(c["function"]["name"], c["function"].get("arguments") or {}) for c in msg.get("tool_calls") or []]
        return Reply(content=msg.get("content") or "", tool_calls=calls, thinking=msg.get("thinking") or "",
                     prompt_tokens=out.get("prompt_eval_count", 0), output_tokens=out.get("eval_count", 0),
                     seconds=time.time() - started)


class OpenAICompatible:
    """Any /v1/chat/completions endpoint (vLLM, llama.cpp server, or a hosted provider)."""

    def __init__(self, model: str, base_url: str, api_key: str | None):
        self.model, self.base_url = model, base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(timeout=900)

    def ping(self) -> str | None:
        try:
            self.client.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
        except httpx.TransportError as e:
            return f"the model server is not reachable at {self.base_url} ({e.__class__.__name__})"
        return None

    def _wire(self, m: dict) -> dict:
        out = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                 for c in m["tool_calls"]]
        if m["role"] == "tool":
            out["tool_call_id"] = m.get("tool_call_id", "")
        return out

    def chat(self, messages: list[dict], tools: list[dict] | None = None, schema: dict | None = None,
             think: bool = False, temperature: float = 0.2, max_tokens: int | None = None) -> Reply:
        payload = {"model": self.model, "messages": [self._wire(m) for m in messages], "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        if schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}
        started = time.time()
        out = _post(self.client, f"{self.base_url}/chat/completions", payload, self.headers)
        msg = out["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": c["function"].get("arguments")}
            calls.append(ToolCall(c["function"]["name"], args, c.get("id") or ToolCall("", {}).id))
        usage = out.get("usage") or {}
        return Reply(content=msg.get("content") or "", tool_calls=calls,
                     prompt_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                     seconds=time.time() - started)


def from_env() -> Ollama | OpenAICompatible:
    provider = os.environ.get("NOVA_LLM_PROVIDER", "ollama")
    model = os.environ.get("NOVA_LLM_MODEL", "qwen3.5:4b")
    if provider == "ollama":
        return Ollama(model, os.environ.get("NOVA_LLM_BASE_URL", "http://127.0.0.1:11434"),
                      num_ctx=int(os.environ.get("NOVA_LLM_NUM_CTX", 20480)),
                      keep_alive=os.environ.get("NOVA_LLM_KEEP_ALIVE", "30m"),
                      options=json.loads(os.environ.get("NOVA_LLM_OPTIONS") or "{}"))
    if provider == "openai":
        base = os.environ.get("NOVA_LLM_BASE_URL")
        if not base:
            raise LLMError("NOVA_LLM_BASE_URL is required for an OpenAI-compatible provider")
        return OpenAICompatible(model, base, os.environ.get("NOVA_LLM_API_KEY"))
    raise LLMError(f"unknown NOVA_LLM_PROVIDER {provider!r}")
