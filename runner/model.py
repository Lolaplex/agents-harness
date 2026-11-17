"""Workspace model selection: a persistent provider/model switch.

``python -m runner.model use --provider openai.default [--model X]`` writes the
pick to ``<ws>/config.json`` under ``"model"``. ``runner.loop`` and
``get_provider`` read it when no explicit ``--provider``/``--model`` or
``LLM_PROVIDER``/``LLM_MODEL`` env applies. Resolution order:

    provider: --provider flag > LLM_PROVIDER env > workspace pick > openai.default
    model:    --model flag    > workspace pick   > LLM_MODEL env > manifest model

``list`` shows every provider manifest with the effective pick marked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _ws_config_path(workspace: Any = None) -> Path:
    if workspace is not None:
        return workspace.config
    from agents_workspace import Workspace

    return Workspace.from_env().config


def _read_config(workspace: Any = None) -> dict[str, Any]:
    path = _ws_config_path(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_config(data: dict[str, Any], workspace: Any = None) -> None:
    path = _ws_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def selected(workspace: Any = None) -> dict[str, str]:
    """Persisted workspace pick: {"provider": ..., "model": ...} (either may be "")."""
    block = _read_config(workspace).get("model")
    if not isinstance(block, dict):
        return {"provider": "", "model": ""}
    return {
        "provider": str(block.get("provider") or ""),
        "model": str(block.get("model") or ""),
    }


def selected_provider_name() -> str:
    return selected().get("provider", "")


def selected_model() -> str:
    return selected().get("model", "")


def use(provider: str, model: str = "", *, workspace: Any = None) -> dict[str, Any]:
    from .providers import find_provider

    if find_provider(provider) is None:
        raise ModelError(f"no provider named '{provider}'", code=2)
    data = _read_config(workspace)
    data["model"] = {"provider": provider, "model": model}
    _write_config(data, workspace)
    return {"provider": provider, "model": model, "source": "workspace"}


def clear(*, workspace: Any = None) -> dict[str, Any]:
    data = _read_config(workspace)
    data.pop("model", None)
    _write_config(data, workspace)
    return {"cleared": True}


class ModelError(Exception):
    def __init__(self, message: str, *, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def current(workspace: Any = None) -> dict[str, Any]:
    """Effective provider/model for the next turn, plus where each came from."""
    from .providers import find_provider, get_provider

    pick = selected(workspace)
    provider_name = (
        os.environ.get("LLM_PROVIDER", "").strip() or pick["provider"] or ""
    )
    try:
        provider = get_provider(provider_name)
    except KeyError as exc:
        raise ModelError(str(exc), code=2)
    manifest = find_provider(provider.name) or {}
    model = (
        pick["model"]
        or os.environ.get(manifest.get("model_env") or "LLM_MODEL", "").strip()
        or str(manifest.get("model") or "")
    )
    return {
        "provider": provider.name,
        "provider_source": "env" if os.environ.get("LLM_PROVIDER", "").strip() else ("workspace" if pick["provider"] else "default"),
        "model": model,
        "model_source": "workspace" if pick["model"] else ("env" if os.environ.get(manifest.get("model_env") or "LLM_MODEL", "").strip() else "manifest"),
    }


def list_providers_marked(workspace: Any = None) -> dict[str, Any]:
    from .providers import list_providers

    try:
        eff = current(workspace)
    except ModelError:
        eff = {"provider": "", "model": ""}
    rows = []
    for p in list_providers():
        rows.append(
            {
                "name": p["name"],
                "kind": p.get("kind"),
                "active": p["name"] == eff.get("provider"),
            }
        )
    return {
        "providers": rows,
        "effective": eff,
        "workspace_pick": selected(workspace),
    }


def _emit(payload: Any, as_json: bool, text: str) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.model")
    parser.add_argument("--help-json", action="store_true", help="Emit machine-readable spec.")
    sub = parser.add_subparsers(dest="action")

    ls = sub.add_parser("list", help="Providers with the effective pick marked")
    ls.add_argument("--json", action="store_true")

    cur = sub.add_parser("current", help="Effective provider and model for the next turn")
    cur.add_argument("--json", action="store_true")

    us = sub.add_parser("use", help="Persist a provider/model pick in the workspace")
    us.add_argument("--provider", required=True)
    us.add_argument("--model", default="")
    us.add_argument("--json", action="store_true")

    cl = sub.add_parser("clear", help="Remove the persisted pick")
    cl.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.help_json:
        print(
            json.dumps(
                {
                    "name": "mcp.model",
                    "description": "Show or switch the workspace LLM provider/model.",
                    "commands": {
                        "list": {"side_effects": "read-only"},
                        "current": {"side_effects": "read-only"},
                        "use": {"side_effects": "mutating"},
                        "clear": {"side_effects": "mutating"},
                    },
                },
                indent=2,
            )
        )
        return 0
    try:
        if args.action == "list":
            payload = list_providers_marked()
            lines = [f"{('*' if r['active'] else ' ')} {r['name']:<24} {r['kind']}" for r in payload["providers"]]
            eff = payload["effective"]
            lines.append(f"effective: {eff.get('provider')} / {eff.get('model')}")
            return _emit(payload, args.json, "\n".join(lines))
        if args.action == "current":
            payload = current()
            text = f"{payload['provider']} / {payload['model']} (provider: {payload['provider_source']}, model: {payload['model_source']})"
            return _emit(payload, args.json, text)
        if args.action == "use":
            payload = use(args.provider, model=args.model)
            return _emit(payload, args.json, f"using {payload['provider']} / {payload['model'] or '(manifest default)'}")
        if args.action == "clear":
            payload = clear()
            return _emit(payload, args.json, "model pick cleared")
    except ModelError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
