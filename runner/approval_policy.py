"""Per-action approval rules layered on the off / ask / strict tiers.

Rules live in host ``agents.json`` and workspace ``config.json`` under
``approval.rules``, in a file named by ``AGENTS_APPROVAL_POLICY``, and in
``AGENTS_APPROVAL_RULES`` (a JSON list). Every layer is kept. When several
rules match, the strictest decision wins: ``deny`` over ``ask`` over
``allow``. ``allow`` never removes a prompt the tier already requires.

An action key is ``tool:<module>``, ``cmd:<command>`` when a command spec
matches, the spec's ``action`` (for example ``fs.delete``), keys the git
tool derives (``git.push:<branch>``), and keys from
``register_action_classifier``. Classifiers add keys. They cannot remove
them. A rule pattern uses ``fnmatch`` (``git.push:*`` also matches
``git.push``).

``ask`` runs the existing approval command. With no command there is nothing
to ask, so the call is unchanged. ``deny`` blocks even when the mode is
``off`` and no command is set.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .helpjson import select_command, unwrap_arguments
from .workspace_config import config_documents

DECISIONS = ("allow", "ask", "deny")
_RANK = {"allow": 0, "ask": 1, "deny": 2}

ActionClassifier = Callable[[dict[str, Any], Any], list[str]]
_classifiers: list[ActionClassifier] = []


class PolicyError(Exception):
    """The policy document is present and cannot be read."""


@dataclass(frozen=True)
class Rule:
    action: str
    decision: str


def register_action_classifier(fn: ActionClassifier) -> ActionClassifier:
    """Host hook. ``fn(module, args) -> action keys``."""
    _classifiers.append(fn)
    return fn


def clear_action_classifiers() -> None:
    _classifiers.clear()


def _as_actions(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def action_keys(mod: dict[str, Any], args: Any = None) -> list[str]:
    """Keys a rule can match. Order is stable; duplicates are dropped."""
    keys: list[str] = []
    name = str(mod.get("name") or "").strip()
    if name:
        keys.append(f"tool:{name}")
    keys.extend(_as_actions(mod.get("action")))
    selected = select_command(mod, args)
    if selected is not None:
        canonical, spec = selected
        keys.append(f"cmd:{canonical}")
        keys.extend(_as_actions(spec.get("action")))
    inner = unwrap_arguments(args)
    if isinstance(inner, dict):
        keys.extend(_as_actions(inner.get("action")))
    if name == "git":
        from .git_tool import actions_for_git

        keys.extend(actions_for_git(inner if inner is not None else args))
    for classifier in list(_classifiers):
        try:
            extra = classifier(mod, args)
        except Exception:
            continue
        if extra:
            keys.extend(str(item).strip() for item in extra)
    return _dedupe(keys)


def _parse_rules(data: Any, *, source: str) -> list[Rule]:
    if data is None:
        return []
    if isinstance(data, dict):
        if "rules" in data:
            data = data.get("rules")
        elif "approval" in data and isinstance(data.get("approval"), dict):
            data = data["approval"].get("rules")
        else:
            raise PolicyError(f"approval policy in {source} has no rules")
    if not isinstance(data, list):
        raise PolicyError(f"approval policy in {source} must be a list of rules")
    rules: list[Rule] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        decision = str(item.get("decision") or "").strip().lower()
        if not action or decision not in DECISIONS:
            continue
        rules.append(Rule(action=action, decision=decision))
    return rules


def _document_rules(doc: dict[str, Any]) -> list[Rule]:
    if not isinstance(doc, dict) or "approval" not in doc:
        return []
    section = doc.get("approval")
    if section is None:
        return []
    if not isinstance(section, dict):
        raise PolicyError("approval config must be an object")
    return _parse_rules(section.get("rules") or [], source="config")


def load_rules(workspace=None) -> list[Rule]:
    """All rules from config and the environment. Invalid JSON is an error."""
    rules: list[Rule] = []
    for doc in config_documents(workspace):
        rules.extend(_document_rules(doc))
    path = os.environ.get("AGENTS_APPROVAL_POLICY", "").strip()
    if path:
        file_doc = read_json_strict(path)
        rules.extend(_parse_rules(file_doc, source=path))
    raw = os.environ.get("AGENTS_APPROVAL_RULES")
    if raw is not None and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolicyError("AGENTS_APPROVAL_RULES is not valid JSON") from exc
        rules.extend(_parse_rules(parsed, source="AGENTS_APPROVAL_RULES"))
    return rules


def read_json_strict(path: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"approval policy file cannot be read: {path}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"approval policy file is not valid JSON: {path}") from exc


def rule_matches(pattern: str, key: str) -> bool:
    """``git.push:*`` matches ``git.push:main`` and the bare key ``git.push``."""
    pattern = pattern.strip()
    key = key.strip()
    if not pattern or not key:
        return False
    if fnmatch.fnmatchcase(key, pattern):
        return True
    if pattern.endswith(":*") and fnmatch.fnmatchcase(key, pattern[:-2]):
        return True
    return False


def strongest_rule(rules: list[Rule], keys: list[str]) -> tuple[Rule | None, str]:
    """Return the strictest matching rule and the key that matched it."""
    best: Rule | None = None
    best_key = ""
    for rule in rules:
        for key in keys:
            if not rule_matches(rule.action, key):
                continue
            if best is None or _RANK[rule.decision] > _RANK[best.decision]:
                best = rule
                best_key = key
    return best, best_key
