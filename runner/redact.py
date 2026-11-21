"""Redact secrets from Cordis job output before traces or the next complete."""

from __future__ import annotations

import os
import re
from pathlib import Path

from agents_workspace import Host, Workspace

_SECRET_ENV_KEYS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "RELAY_SECRET",
    "GATEWAY_SECRET",
    "RELAY_WEB_TOKEN",
    "WEBHOOK_SECRET",
    "AGENTS_TERMINAL_NOTIFY_SECRET",
    "AGENTS_APPROVAL_TOKEN",
    "AGENTS_SYNC_TOKEN",
    "AGENTS_SYNC_SERVER_SECRET",
)

_MIN_ENV_SECRET_LEN = 6

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    re.compile(r"(?i)(?:" + "|".join(_SECRET_ENV_KEYS) + r")\s*[:=]\s*\S+"),
    re.compile(r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
]

_REDACTED = "[redacted]"


def _agents_home_paths() -> list[str]:
    """Path prefixes to scrub from tool output (longest first)."""
    candidates: list[str] = []
    try:
        ws = Workspace.from_env()
    except ValueError:
        candidates.append(str(Host.from_env().dir))
        raw = str(os.environ.get("AGENTS_WORKSPACE_ROOT") or "").strip()
        if raw:
            candidates.append(raw)
    else:
        raw = str(
            ws.source_env.get("AGENTS_WORKSPACE_ROOT") or ""
        ).strip()
        if raw:
            candidates.append(raw)
        candidates.append(str(ws.dir))
        candidates.append(str(ws.host.dir))
    legacy = os.path.expanduser("~/.agents")
    if legacy not in candidates:
        candidates.append(legacy)
    prefixes: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        for variant in (raw, raw.replace("\\", "/")):
            if variant and variant not in seen:
                seen.add(variant)
                prefixes.append(variant)
            try:
                resolved = str(Path(raw).expanduser().resolve())
            except OSError:
                continue
            for resolved_variant in (resolved, resolved.replace("\\", "/")):
                if resolved_variant and resolved_variant not in seen:
                    seen.add(resolved_variant)
                    prefixes.append(resolved_variant)
    return sorted(prefixes, key=len, reverse=True)


def _redact_env_values(text: str) -> str:
    if not text:
        return text
    values: list[str] = []
    for key in _SECRET_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if len(val) >= _MIN_ENV_SECRET_LEN and val not in values:
            values.append(val)
    for val in sorted(values, key=len, reverse=True):
        if val in text:
            text = text.replace(val, _REDACTED)
    return text


def _redact_paths(text: str) -> str:
    if not text:
        return text
    for prefix in _agents_home_paths():
        if prefix and prefix in text:
            text = text.replace(prefix, _REDACTED)
    return text


def redact_tool_output(text: str) -> str:
    """One boundary for tool output before tool messages and traces."""
    if not text:
        return text
    out = text
    for pat in _PATTERNS:
        out = pat.sub(_REDACTED, out)
    out = _redact_env_values(out)
    return _redact_paths(out)
