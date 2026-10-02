"""Completion providers. Same JSON shape as Cordis modules; not shell verbs.

The loop talks to `kind=openai_compat` (or `echo`) through one interface so
another LLM is another provider file, not a fork of loop.py.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .executor import load_manifest

PROVIDERS_DIR = Path(__file__).parent / "providers"


def _provider_dirs(primary: Path | None = None) -> list[Path]:
    """Package providers first; AGENTS_PROVIDERS_DIR overlays by name.

    Machine URLs and tokens live in the overlay (agents-sandbox), not in the
    clone. Extra dir wins on name collision.
    """
    dirs = [primary or PROVIDERS_DIR]
    extra = os.environ.get("AGENTS_PROVIDERS_DIR", "").strip()
    if extra:
        overlay = Path(extra)
        if overlay.resolve() not in {d.resolve() for d in dirs}:
            dirs.append(overlay)
    return dirs


@dataclass
class CompletionRequest:
    messages: list[dict[str, Any]]
    model: str = ""
    tools: list[dict[str, Any]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    on_status: Callable[[str], None] | None = None
    on_delta: Callable[[str], None] | None = None


@dataclass
class CompletionResult:
    text: str
    tool_calls: list[Any] | None = None
    raw: dict[str, Any] | None = None


class Provider(Protocol):
    name: str
    kind: str

    def complete(self, req: CompletionRequest) -> CompletionResult:
        ...


def load_provider(path: Path) -> dict[str, Any]:
    data = load_manifest(path)
    data.setdefault("kind", "openai_compat")
    data.setdefault("when", "on_request")
    data.setdefault("cadence", "on_request")
    return data


def list_providers(providers_dir: Path | None = None) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    dirs = [providers_dir] if providers_dir is not None else _provider_dirs()
    for folder in dirs:
        if folder is None or not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            row = load_provider(path)
            seen[str(row["name"])] = row
    return list(seen.values())


def find_provider(name: str, providers_dir: Path | None = None) -> dict[str, Any] | None:
    needle = name.strip().lower()
    for row in list_providers(providers_dir):
        if row["name"].lower() == needle:
            return row
    return None


class EchoProvider:
    """Test double. Same complete() shape; returns the last user text."""

    name = "echo"
    kind = "echo"

    def complete(self, req: CompletionRequest) -> CompletionResult:
        text = ""
        for msg in reversed(req.messages):
            if msg.get("role") == "user":
                content = msg.get("content") or ""
                text = content if isinstance(content, str) else str(content)
                break
        text = text or "(echo)"
        if req.on_status:
            req.on_status("thinking...")
        if req.on_delta:
            req.on_delta(text)
        return CompletionResult(text=text)


class ScriptedToolProvider:
    """First complete() is a tool call. After a tool result, echo that stdout.

    Used by agents-sandbox to prove a skill/tool turn through the loop without
    an LLM. Overlay JSON may set tool_name and tool_arguments.query.
    """

    kind = "scripted"

    def __init__(self, manifest: dict[str, Any]):
        self.name = str(manifest.get("name") or "scripted.tool")
        self.tool_name = str(manifest.get("tool_name") or "mcp.memory.search")
        self.tool_arguments = dict(manifest.get("tool_arguments") or {})

    def complete(self, req: CompletionRequest) -> CompletionResult:
        if req.on_status:
            req.on_status("thinking...")
        for msg in reversed(req.messages):
            if msg.get("role") == "tool":
                text = str(msg.get("content") or "")
                if req.on_delta:
                    req.on_delta(text)
                return CompletionResult(text=text)
        query = str(self.tool_arguments.get("query") or "").strip()
        if not query:
            for msg in reversed(req.messages):
                if msg.get("role") == "user":
                    content = msg.get("content") or ""
                    query = content if isinstance(content, str) else str(content)
                    break
        job_name = str(self.tool_arguments.get("name") or "mcp.memory.search")
        job_args = dict(self.tool_arguments.get("arguments") or {})
        if "query" not in job_args:
            job_args["query"] = query.strip()
        payload = {"name": job_name, "arguments": job_args}
        return CompletionResult(
            text="",
            tool_calls=[
                {
                    "id": "scripted_1",
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "arguments": json.dumps(payload, ensure_ascii=False),
                    },
                }
            ],
        )


def _accumulate_tool_calls(tools: dict[int, dict[str, Any]], calls: Any) -> None:
    if not calls:
        return
    for i, tc in enumerate(calls):
        if not isinstance(tc, dict):
            continue
        idx = int(tc["index"]) if tc.get("index") is not None else i
        slot = tools.setdefault(
            idx,
            {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if "arguments" in fn and fn["arguments"] is not None:
            args = fn["arguments"]
            slot["function"]["arguments"] += (
                args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            )


def consume_sse(
    lines: Iterable[bytes | str],
    *,
    on_delta: Callable[[str], None] | None = None,
) -> tuple[str, list[Any] | None, dict[str, Any]]:
    """Fold an OpenAI-compat SSE stream into text + tool_calls.

    Hang detection is the caller's socket idle timeout between lines, not a
    wall-clock budget for the whole generation.
    """
    text_parts: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    n = 0
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        n += 1
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        piece = delta.get("content")
        if piece:
            text_parts.append(str(piece))
            if on_delta:
                on_delta(str(piece))
        _accumulate_tool_calls(tools, delta.get("tool_calls"))
        msg = choice.get("message") or {}
        if msg.get("content") and not text_parts:
            text_parts.append(str(msg["content"]))
        _accumulate_tool_calls(tools, msg.get("tool_calls"))
    tool_list = [tools[i] for i in sorted(tools)] or None
    return "".join(text_parts).strip(), tool_list, {"sse_chunks": n}


def _set_socket_timeout(resp: Any, seconds: float) -> None:
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None) if fp is not None else None
    sock = getattr(raw, "_sock", None) if raw is not None else None
    if sock is not None:
        sock.settimeout(seconds)


def _iter_sse_lines(resp: Any, idle_sec: float):
    first = True
    while True:
        line = resp.readline()
        if not line:
            break
        if first:
            first = False
            _set_socket_timeout(resp, idle_sec)
        yield line


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(self, manifest: dict[str, Any]):
        self.name = str(manifest.get("name") or "openai.default")
        self.manifest = manifest

    def complete(self, req: CompletionRequest) -> CompletionResult:
        url = (
            os.environ.get(self.manifest.get("url_env") or "LLM_BASE_URL", "")
            or str(self.manifest.get("url") or "")
        ).rstrip("/")
        if url and not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        key = os.environ.get(self.manifest.get("key_env") or "LLM_API_KEY", "") or str(
            self.manifest.get("api_key") or ""
        )
        model = req.model or os.environ.get(
            self.manifest.get("model_env") or "LLM_MODEL", ""
        ) or str(self.manifest.get("model") or "")
        if not url:
            raise RuntimeError("LLM_BASE_URL required (or provider manifest url)")
        if not model:
            raise RuntimeError("LLM_MODEL required (or provider manifest model)")
        stream = True
        if req.extra and "stream" in req.extra:
            stream = bool(req.extra["stream"])
        body: dict[str, Any] = {"model": model, "messages": req.messages, "stream": stream}
        extra = dict(req.extra or {})
        extra.pop("stream", None)

        # Set default temperature to prevent runaway hallucination / token collapse
        if "temperature" not in extra:
            temp_env = os.environ.get("LLM_TEMPERATURE")
            if temp_env is not None:
                try:
                    body["temperature"] = float(temp_env)
                except ValueError:
                    pass
            elif "temperature" in self.manifest:
                body["temperature"] = float(self.manifest["temperature"])
            else:
                body["temperature"] = 0.2

        # Set default max_tokens to prevent infinite generation loops
        if "max_tokens" not in extra:
            max_env = os.environ.get("LLM_MAX_TOKENS")
            if max_env is not None:
                try:
                    body["max_tokens"] = int(max_env)
                except ValueError:
                    pass
            elif "max_tokens" in self.manifest:
                body["max_tokens"] = int(self.manifest["max_tokens"])
            else:
                body["max_tokens"] = 4096

        if req.tools:
            body["tools"] = req.tools
            if "tool_choice" not in extra:
                choice = self.manifest.get("tool_choice")
                body["tool_choice"] = "auto" if choice is None else choice
        if extra:
            body.update(extra)
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        http_req = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )
        first_byte = float(
            self.manifest.get("first_byte_sec") or self.manifest.get("timeout_sec") or 90
        )
        idle = float(self.manifest.get("idle_sec") or 20)
        if req.on_status:
            req.on_status("thinking...")
        try:
            with urllib.request.urlopen(http_req, timeout=first_byte) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if stream or "text/event-stream" in ctype:
                    text, tool_calls, meta = consume_sse(
                        _iter_sse_lines(resp, idle),
                        on_delta=req.on_delta,
                    )
                    return CompletionResult(text=text, tool_calls=tool_calls, raw=meta)
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"chat completions HTTP {e.code}: {err}") from e
        except (TimeoutError, urllib.error.URLError) as e:
            reason = getattr(e, "reason", e)
            if not isinstance(reason, TimeoutError) and "timed out" not in str(e).lower():
                raise
            raise RuntimeError(
                f"LLM idle/first-byte timeout ({idle}s idle, {first_byte}s first byte); "
                "generation is not wall-clock capped"
            ) from e
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return CompletionResult(
            text=str(msg.get("content") or "").strip(),
            tool_calls=msg.get("tool_calls") or None,
            raw=data,
        )


class AcpProvider:
    """Stub ACP peer completer; full Cursor/Antigravity wiring lands later."""

    kind = "acp"

    def __init__(self, manifest: dict[str, Any]):
        self.name = str(manifest.get("name") or "acp.stub")
        self.manifest = manifest
        self.agent = str(manifest.get("agent") or "peer")

    def complete(self, req: CompletionRequest) -> CompletionResult:
        if req.on_status:
            req.on_status("thinking...")
        text = (
            f"[acp stub: {self.name}] peer completer '{self.agent}' is not wired; "
            "configure ACP endpoint or use openai_compat provider."
        )
        if req.on_delta:
            req.on_delta(text)
        return CompletionResult(text=text)


_KINDS = {
    "echo": lambda _m: EchoProvider(),
    "scripted": ScriptedToolProvider,
    "openai_compat": OpenAICompatProvider,
    "acp": AcpProvider,
}


def get_provider(name: str = "", providers_dir: Path | None = None) -> Provider:
    wanted = (name or os.environ.get("LLM_PROVIDER") or "").strip()
    rows = list_providers(providers_dir)
    manifest = None
    if wanted:
        manifest = find_provider(wanted, providers_dir)
        if manifest is None:
            raise KeyError(f"no provider named '{wanted}'")
    else:
        manifest = find_provider("openai.default", providers_dir)
        if manifest is None:
            manifest = next((r for r in rows if r.get("kind") == "openai_compat"), None)
        if manifest is None and rows:
            manifest = rows[0]
        if manifest is None:
            manifest = {
                "name": "openai.default",
                "kind": "openai_compat",
                "url_env": "LLM_BASE_URL",
                "key_env": "LLM_API_KEY",
                "model_env": "LLM_MODEL",
            }
    kind = str(manifest.get("kind") or "openai_compat")
    factory = _KINDS.get(kind)
    if factory is None:
        raise KeyError(f"unknown provider kind '{kind}'")
    return factory(manifest)
