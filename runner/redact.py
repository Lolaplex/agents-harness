"""Redact secrets from Cordis job stdout before traces or the next complete."""

from __future__ import annotations

import os
import re
from pathlib import Path

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    re.compile(r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"TELEGRAM_BOT_TOKEN=\S+"),
    re.compile(r"GATEWAY_SECRET=\S+"),
]

_REDACTED = "[redacted]"


def _agents_home_paths() -> list[str]:
    """Path prefixes to scrub from tool output (longest first)."""
    candidates: list[str] = []
    home = os.environ.get("AGENTS_HOME", "").strip()
    if home:
        candidates.append(home)
    candidates.append(str(Path.home() / ".agents"))
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


def _redact_paths(text: str) -> str:
    if not text:
        return text
    for prefix in _agents_home_paths():
        if prefix and prefix in text:
            text = text.replace(prefix, _REDACTED)
    return text


def redact_tool_output(text: str) -> str:
    """One boundary for tool stdout before tool messages and traces."""
    if not text:
        return text
    out = text
    for pat in _PATTERNS:
        out = pat.sub(_REDACTED, out)
    return _redact_paths(out)
