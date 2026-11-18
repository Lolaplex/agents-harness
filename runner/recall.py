"""Pre-turn recall. Off unless a workspace or the environment turns it on.

One short instruction line is added to the system prompt when the switch is
on. Hits, if any, are a separate fenced message ahead of the user turn.
Nothing here imports a memory package. Hosts register a provider or set
``AGENTS_RECALL_CMD``. An optional bounded repo scan is a source, not a
dependency.

``AGENTS_RECALL=1`` enables. ``AGENTS_RECALL=0`` disables. When the variable
is unset, host ``agents.json`` and workspace ``config.json`` key ``recall.enabled``
are read (workspace wins). The default is off.

Sources (``recall.sources`` or ``AGENTS_RECALL_SOURCES``, comma-separated):

- ``registered`` — ``register_recall_provider`` callables (default)
- ``command`` — ``AGENTS_RECALL_CMD`` (default)
- ``repo`` — bounded text scan under ``AGENTS_RECALL_ROOT``, ``AGENTS_GIT_REPO``,
  or the workspace workdir (default)

A provider is ``fn(request: RecallRequest) -> hits``. A hit is a
``RecallHit`` or a mapping with ``text`` and optional ``source`` and
``title``. Failures are skipped.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .fence import fence_untrusted
from .workspace_config import env_flag, env_int, merged_section

RECALL_PROMPT_LINE = (
    "When retrieved notes appear before the user message, use a fact from them "
    "when it answers the question, and ask the user only when it does not."
)

DEFAULT_MAX_CHARS = 4000
DEFAULT_MAX_HITS = 5
DEFAULT_SOURCES = ("registered", "command", "repo")
_QUERY_MAX = 2000
_REPO_FILES = 200
_REPO_FILE_BYTES = 65536
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
}
_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".json",
    ".toml",
    ".yml",
    ".yaml",
    ".rst",
}
_TERM = re.compile(r"[A-Za-z0-9_]{4,}")


@dataclass(frozen=True)
class RecallRequest:
    query: str
    project: str = ""
    limit: int = DEFAULT_MAX_HITS


@dataclass(frozen=True)
class RecallHit:
    source: str
    text: str
    title: str = ""


RecallProvider = Callable[[RecallRequest], Any]
_providers: list[RecallProvider] = []


def register_recall_provider(fn: RecallProvider) -> RecallProvider:
    """Register a host provider. The callable receives a ``RecallRequest``."""
    _providers.append(fn)
    return fn


def clear_recall_providers() -> None:
    _providers.clear()


def _section(workspace=None) -> dict[str, Any]:
    return merged_section("recall", workspace)


def recall_enabled(workspace=None) -> bool:
    """Explicit env wins. Otherwise config. Default off."""
    flag = env_flag("AGENTS_RECALL")
    if flag is not None:
        return flag
    enabled = _section(workspace).get("enabled")
    if isinstance(enabled, bool):
        return enabled
    if isinstance(enabled, str):
        return enabled.strip().lower() in ("1", "true", "yes", "on")
    return False


def recall_sources(workspace=None) -> tuple[str, ...]:
    raw = os.environ.get("AGENTS_RECALL_SOURCES", "").strip()
    if not raw:
        configured = _section(workspace).get("sources")
        if isinstance(configured, str):
            raw = configured
        elif isinstance(configured, list):
            raw = ",".join(str(item) for item in configured)
    if not raw.strip():
        return DEFAULT_SOURCES
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _limits(workspace=None) -> tuple[int, int]:
    section = _section(workspace)
    max_chars = DEFAULT_MAX_CHARS
    max_hits = DEFAULT_MAX_HITS
    if section.get("max_chars") not in (None, ""):
        try:
            max_chars = int(section["max_chars"])
        except (TypeError, ValueError):
            max_chars = DEFAULT_MAX_CHARS
    if section.get("max_hits") not in (None, ""):
        try:
            max_hits = int(section["max_hits"])
        except (TypeError, ValueError):
            max_hits = DEFAULT_MAX_HITS
    max_chars = env_int("AGENTS_RECALL_MAX_CHARS", max_chars, minimum=200, maximum=100000)
    max_hits = env_int("AGENTS_RECALL_MAX_HITS", max_hits, minimum=1, maximum=50)
    max_chars = max(200, min(100000, max_chars))
    max_hits = max(1, min(50, max_hits))
    return max_chars, max_hits


def recall_prompt_line(workspace=None) -> str:
    """The single instruction line, or ``""`` when recall is off."""
    if not recall_enabled(workspace):
        return ""
    return RECALL_PROMPT_LINE


def _coerce_hit(item: Any) -> RecallHit | None:
    if isinstance(item, RecallHit):
        text = item.text.strip()
        if not text:
            return None
        return RecallHit(source=item.source or "recall", text=text, title=item.title or "")
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("body") or "").strip()
        if not text:
            return None
        return RecallHit(
            source=str(item.get("source") or "recall").strip() or "recall",
            text=text,
            title=str(item.get("title") or "").strip(),
        )
    if isinstance(item, str) and item.strip():
        return RecallHit(source="recall", text=item.strip())
    return None


def _call_provider(fn: RecallProvider, request: RecallRequest) -> list[RecallHit]:
    try:
        raw = fn(request)
    except Exception:
        return []
    if raw is None:
        return []
    if isinstance(raw, (RecallHit, dict, str)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    hits: list[RecallHit] = []
    for item in raw:
        hit = _coerce_hit(item)
        if hit is not None:
            hits.append(hit)
    return hits


def _command_provider(request: RecallRequest) -> list[RecallHit]:
    raw_cmd = os.environ.get("AGENTS_RECALL_CMD", "").strip()
    if not raw_cmd:
        return []
    timeout = env_int("AGENTS_RECALL_TIMEOUT", 2, minimum=1, maximum=30)
    try:
        argv = shlex.split(raw_cmd, posix=True)
    except ValueError:
        return []
    if not argv:
        return []
    payload = json.dumps(
        {"query": request.query, "project": request.project, "limit": request.limit},
        ensure_ascii=False,
    )
    try:
        proc = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return _parse_command_output(proc.stdout or "")


def _parse_command_output(text: str) -> list[RecallHit]:
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [RecallHit(source="command", text=raw)]
    items: Any
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("hits")
        if items is None:
            items = data.get("results")
        if items is None and data.get("text"):
            items = [data]
    else:
        return []
    if not isinstance(items, list):
        return []
    hits: list[RecallHit] = []
    for item in items:
        hit = _coerce_hit(item)
        if hit is not None:
            source = hit.source if hit.source != "recall" else "command"
            hits.append(RecallHit(source=source, text=hit.text, title=hit.title))
    return hits


def recall_root(workspace=None) -> Path | None:
    for name in ("AGENTS_RECALL_ROOT", "AGENTS_GIT_REPO"):
        raw = os.environ.get(name, "").strip()
        if raw:
            return Path(raw).expanduser()
    if workspace is not None:
        work = getattr(workspace, "workdir", None)
        if work:
            return Path(work)
    return None


def search_repo(request: RecallRequest, root: Path) -> list[RecallHit]:
    """Bounded lexical scan. Skips VCS and dependency directories."""
    if not root.is_dir():
        return []
    terms = []
    seen: set[str] = set()
    for match in _TERM.findall(request.query.lower()):
        if match not in seen:
            seen.add(match)
            terms.append(match)
        if len(terms) >= 12:
            break
    if not terms:
        return []
    scored: list[tuple[int, RecallHit]] = []
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _SKIP_DIRS and not name.startswith(".")
        ]
        for filename in filenames:
            if files >= _REPO_FILES:
                return _top(scored, request.limit)
            files += 1
            path = Path(dirpath) / filename
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                if path.stat().st_size > _REPO_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lowered = text.lower()
            score = sum(1 for term in terms if term in lowered)
            if score <= 0:
                continue
            window = _window(text, terms)
            try:
                title = str(path.relative_to(root))
            except ValueError:
                title = path.name
            scored.append(
                (score, RecallHit(source="repo", text=window, title=title))
            )
    return _top(scored, request.limit)


def _window(text: str, terms: list[str]) -> str:
    lowered = text.lower()
    index = -1
    for term in terms:
        found = lowered.find(term)
        if found >= 0 and (index < 0 or found < index):
            index = found
    if index < 0:
        snippet = text[:400]
    else:
        start = max(0, index - 80)
        snippet = text[start : start + 400]
    snippet = snippet.strip()
    if len(snippet) > 400:
        snippet = snippet[:400]
    return snippet


def _top(scored: list[tuple[int, RecallHit]], limit: int) -> list[RecallHit]:
    scored.sort(key=lambda item: item[0], reverse=True)
    return [hit for _score, hit in scored[:limit]]


def _repo_provider(request: RecallRequest, workspace=None) -> list[RecallHit]:
    root = recall_root(workspace)
    if root is None:
        return []
    return search_repo(request, root)


def collect_hits(
    query: str,
    *,
    workspace=None,
    project: str = "",
) -> list[RecallHit]:
    """Run the enabled sources. Empty when recall is off or nothing matches."""
    if not recall_enabled(workspace):
        return []
    text = (query or "").strip()
    if not text:
        return []
    if len(text) > _QUERY_MAX:
        text = text[:_QUERY_MAX]
    max_chars, max_hits = _limits(workspace)
    request = RecallRequest(query=text, project=project or "", limit=max_hits)
    sources = set(recall_sources(workspace))
    hits: list[RecallHit] = []
    if "registered" in sources:
        for provider in list(_providers):
            hits.extend(_call_provider(provider, request))
    if "command" in sources:
        hits.extend(_command_provider(request))
    if "repo" in sources:
        hits.extend(_repo_provider(request, workspace))
    deduped: list[RecallHit] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in hits:
        key = (hit.source, hit.title, hit.text)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
        if len(deduped) >= max_hits:
            break
    return _bound(deduped, max_chars)


def _bound(hits: list[RecallHit], max_chars: int) -> list[RecallHit]:
    kept: list[RecallHit] = []
    used = 0
    for hit in hits:
        overhead = len(hit.source) + len(hit.title) + 8
        room = max_chars - used - overhead
        if room < 40:
            break
        text = hit.text
        if len(text) > room:
            text = text[: room - 12].rstrip() + " [truncated]"
        kept.append(RecallHit(source=hit.source, text=text, title=hit.title))
        used += overhead + len(text)
    return kept


def format_recall(hits: list[RecallHit]) -> str:
    """Fenced notes. Empty when there are no hits."""
    if not hits:
        return ""
    blocks = []
    for hit in hits:
        title = f" {hit.title}" if hit.title else ""
        blocks.append(f"[{hit.source}]{title}\n{hit.text}")
    return fence_untrusted("recall", "\n\n".join(blocks))


def recall_message(
    query: str,
    *,
    workspace=None,
    project: str = "",
) -> str:
    """The context message body, or ``""`` when there is nothing to inject."""
    return format_recall(collect_hits(query, workspace=workspace, project=project))
