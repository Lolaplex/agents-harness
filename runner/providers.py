"""Completion providers. Same JSON shape as Cordis modules; not shell verbs.

The loop talks to `kind=openai_compat` (or `echo`) through one interface so
another LLM is another provider file, not a fork of loop.py.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from agents_workspace import resolve_secret

from .executor import load_manifest

PROVIDERS_DIR = Path(__file__).parent / "providers"


def _provider_dirs(primary: Path | None = None) -> list[Path]:
    """Package providers first; host dir and AGENTS_PROVIDERS_DIR overlay by name.

    Machine URLs and tokens live in overlays (``<host>/providers``,
    agents-sandbox), not in the clone. Later dirs win on name collision;
    an explicit AGENTS_PROVIDERS_DIR is the strongest pick.
    """
    dirs = [primary or PROVIDERS_DIR]
    overlays = []
    try:
        from agents_workspace import Host

        host = Host.from_env()
        overlays.append(host.dir / "providers")
        try:
            from .mcp_client import _resolve_ws_no_create

            ws = _resolve_ws_no_create()
        except Exception:
            ws = None
        if ws is not None:
            overlays.append(host.dir / "providers" / f"ws-{ws.id}")
    except Exception:
        pass
    extra = os.environ.get("AGENTS_PROVIDERS_DIR", "").strip()
    if extra:
        overlays.append(Path(extra))
    seen = {d.resolve() for d in dirs}
    for overlay in overlays:
        if overlay.resolve() not in seen:
            seen.add(overlay.resolve())
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
    on_thought: Callable[[str], None] | None = None


@dataclass
class CompletionResult:
    text: str
    tool_calls: list[Any] | None = None
    raw: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


def _non_neg_int(val: Any) -> int | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, int) and val >= 0:
        return val
    return None


def normalize_usage(raw_usage: Any, *, model: str = "") -> dict[str, Any] | None:
    """Normalize OpenAI / Anthropic usage dicts into a unified shape."""
    if not isinstance(raw_usage, dict):
        return None
    tokens_in = _non_neg_int(raw_usage.get("prompt_tokens"))
    if tokens_in is None:
        tokens_in = _non_neg_int(raw_usage.get("input_tokens"))
    tokens_out = _non_neg_int(raw_usage.get("completion_tokens"))
    if tokens_out is None:
        tokens_out = _non_neg_int(raw_usage.get("output_tokens"))
    if tokens_in is None and tokens_out is None:
        return None
    raw_cost = raw_usage.get("est_cost_usd")
    if raw_cost is None:
        raw_cost = raw_usage.get("cost")
    est_cost: float | None = None
    if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) and raw_cost >= 0:
        est_cost = float(raw_cost)
    return {
        "tokens_in": tokens_in or 0,
        "tokens_out": tokens_out or 0,
        "est_cost_usd": est_cost,
        "model": model or str(raw_usage.get("model") or ""),
    }


# Transient upstream statuses retried before the first byte. Nothing has
# streamed to the user yet, so a retry cannot duplicate output.
RETRY_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_RETRIES = 2
RETRY_BASE_SEC = 1.0
RETRY_MAX_SEC = 10.0
_sleep = time.sleep  # patched in tests


def _retry_delay(err: urllib.error.HTTPError, attempt: int) -> float:
    """Retry-After (delta-seconds or HTTP-date) when sane, else
    exponential backoff; capped."""
    delay = RETRY_BASE_SEC * (2 ** attempt)
    try:
        hdr = (err.headers or {}).get("Retry-After") if err.headers else None
        raw = str(hdr).strip() if hdr is not None else ""
        if raw:
            try:
                delay = float(raw)
            except ValueError:
                from email.utils import parsedate_to_datetime

                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    return max(0.0, min(RETRY_MAX_SEC, delay))


def _open_with_retry(http_req, timeout: float, retries: int,
                     on_status: Callable[[str], None] | None = None):
    """urlopen with up to ``retries`` retries on 429/502/503/504.

    Only the request open is retried (status line not yet consumed), never
    a response that already started streaming.
    """
    attempt = 0
    while True:
        try:
            return urllib.request.urlopen(http_req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES or attempt >= retries:
                raise
            delay = _retry_delay(e, attempt)
            try:
                e.close()
            except Exception:
                pass
            attempt += 1
            if on_status:
                on_status(f"provider HTTP {e.code}, retry {attempt}/{retries} in {delay:.0f}s")
            _sleep(delay)


class Provider(Protocol):
    name: str
    kind: str

    def complete(self, req: CompletionRequest) -> CompletionResult:
        ...


def _warn_plaintext_api_key(
    manifest: dict[str, Any], *, path: Path | None = None, stacklevel: int = 3
) -> None:
    raw = str(manifest.get("api_key") or "").strip()
    if not raw:
        return
    name = str(manifest.get("name") or (path.stem if path is not None else "unknown"))
    loc = f"{name!r} ({path})" if path is not None else f"{name!r}"
    warnings.warn(
        f"Provider manifest {loc} contains plaintext 'api_key'; "
        "migrate to 'key_env' and store the secret in the host secret vault "
        "(plaintext 'api_key' is deprecated and will be removed after one release).",
        UserWarning,
        stacklevel=stacklevel,
    )


def load_provider(path: Path) -> dict[str, Any]:
    data = load_manifest(path)
    data.setdefault("kind", "openai_compat")
    data.setdefault("when", "on_request")
    data.setdefault("cadence", "on_request")
    _warn_plaintext_api_key(data, path=path)
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


def message_text(content: Any) -> str:
    """Plain text from a string message or a vision content-part list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for part in content:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                bits.append(str(part.get("text") or ""))
        return "\n".join(bit for bit in bits if bit)
    return str(content or "")


class EchoProvider:
    """Test double. Same complete() shape; returns the last user text."""

    name = "echo"
    kind = "echo"

    def complete(self, req: CompletionRequest) -> CompletionResult:
        text = ""
        for msg in reversed(req.messages):
            if msg.get("role") == "user":
                text = message_text(msg.get("content") or "")
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
                    query = message_text(msg.get("content") or "")
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
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if "arguments" in fn and fn["arguments"] is not None:
            args = fn["arguments"]
            slot["function"]["arguments"] += (
                args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            )
        # Vendor extras must round-trip verbatim: Gemini attaches
        # extra_content.google.thought_signature to functionCall parts and
        # 400s on the next request if it is missing.
        for key, value in tc.items():
            if key not in ("index", "id", "type", "function") and value is not None:
                slot[key] = value
        for key, value in fn.items():
            if key not in ("name", "arguments") and value is not None:
                slot["function"][key] = value


def consume_sse(
    lines: Iterable[bytes | str],
    *,
    on_delta: Callable[[str], None] | None = None,
    on_thought: Callable[[str], None] | None = None,
) -> tuple[str, list[Any] | None, dict[str, Any]]:
    """Fold an OpenAI-compat SSE stream into text + tool_calls.

    Hang detection is the caller's socket idle timeout between lines, not a
    wall-clock budget for the whole generation.
    """
    text_parts: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    usage_raw: dict[str, Any] | None = None
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
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            usage_raw = chunk["usage"]
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning and on_thought:
            on_thought(reasoning)
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
    meta: dict[str, Any] = {"sse_chunks": n}
    if usage_raw is not None:
        meta["usage"] = usage_raw
    return "".join(text_parts).strip(), tool_list, meta


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

    def __init__(self, manifest: dict[str, Any], *, _warn_plaintext: bool = True):
        self.name = str(manifest.get("name") or "openai.default")
        self.manifest = manifest
        if _warn_plaintext:
            _warn_plaintext_api_key(manifest, stacklevel=3)

    def _retries(self) -> int:
        raw = self.manifest.get("retries")
        if raw is None:
            raw = os.environ.get("LLM_RETRIES", "")
        try:
            return max(0, min(5, int(raw))) if str(raw).strip() != "" else DEFAULT_RETRIES
        except (TypeError, ValueError):
            return DEFAULT_RETRIES

    def complete(self, req: CompletionRequest) -> CompletionResult:
        url = (
            os.environ.get(self.manifest.get("url_env") or "LLM_BASE_URL", "")
            or str(self.manifest.get("url") or "")
        ).rstrip("/")
        if url and not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        key = resolve_secret(self.manifest.get("key_env") or "LLM_API_KEY") or str(
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
        if "reasoning" not in extra and "reasoning" in self.manifest:
            body["reasoning"] = self.manifest["reasoning"]
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
            with _open_with_retry(
                http_req, first_byte, self._retries(), req.on_status
            ) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if stream or "text/event-stream" in ctype:
                    text, tool_calls, meta = consume_sse(
                        _iter_sse_lines(resp, idle),
                        on_delta=req.on_delta,
                        on_thought=req.on_thought,
                    )
                    return CompletionResult(
                        text=text,
                        tool_calls=tool_calls,
                        raw=meta,
                        usage=normalize_usage(meta.get("usage"), model=model),
                    )
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
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning and req.on_thought:
            req.on_thought(reasoning)
        return CompletionResult(
            text=str(msg.get("content") or "").strip(),
            tool_calls=msg.get("tool_calls") or None,
            raw=data,
            usage=normalize_usage(data.get("usage"), model=model),
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
    if not wanted:
        try:
            from .model import selected_provider_name

            wanted = selected_provider_name()
        except Exception:
            wanted = ""
    rows = list_providers(providers_dir)
    manifest = None
    from_disk = False
    if wanted:
        needle = wanted.lower()
        manifest = next(
            (r for r in rows if str(r.get("name", "")).lower() == needle),
            None,
        )
        if manifest is None:
            raise KeyError(f"no provider named '{wanted}'")
        from_disk = True
    else:
        manifest = next(
            (r for r in rows if str(r.get("name", "")).lower() == "openai.default"),
            None,
        )
        if manifest is None:
            manifest = next((r for r in rows if r.get("kind") == "openai_compat"), None)
        if manifest is None and rows:
            manifest = rows[0]
        if manifest is not None:
            from_disk = True
        else:
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
    if factory is OpenAICompatProvider and from_disk:
        return OpenAICompatProvider(manifest, _warn_plaintext=False)
    return factory(manifest)
