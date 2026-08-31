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
from typing import Any, Protocol

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
        return CompletionResult(text=text or "(echo)")


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
        body: dict[str, Any] = {"model": model, "messages": req.messages}
        if req.tools:
            body["tools"] = req.tools
        if req.extra:
            body.update(req.extra)
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        http_req = urllib.request.Request(
            url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(http_req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"chat completions HTTP {e.code}: {err}") from e
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return CompletionResult(
            text=str(msg.get("content") or "").strip(),
            tool_calls=msg.get("tool_calls") or None,
            raw=data,
        )


_KINDS = {
    "echo": lambda _m: EchoProvider(),
    "openai_compat": OpenAICompatProvider,
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
