"""vision.analyze: one-shot image analysis through a vision-capable provider.

``python -m runner.vision_analyze --image PATH [--prompt TEXT]`` sends the
image as an OpenAI ``image_url`` part and prints the model's answer as JSON.
The provider needs vision: ``AGENTS_VISION=1``, manifest ``vision: true``, or
``capabilities`` containing ``vision``. Without it the call exits non-zero
instead of silently describing a missing image.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from .attachments import IMAGE_TYPES, vision_enabled
from .providers import CompletionRequest, get_provider, find_provider, list_providers

_MAX_IMAGE_BYTES = 20_000_000
_DEFAULT_PROMPT = "Describe this image. Be precise and concrete."


def _err(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    return 2


def analyze(
    image: str,
    *,
    prompt: str = "",
    provider_name: str = "",
    model: str = "",
) -> dict[str, Any]:
    path = Path(image).expanduser().resolve(strict=False)
    mime = IMAGE_TYPES.get(path.suffix.lower())
    if mime is None:
        raise ValueError(f"not a supported image type: {path.suffix or path.name}")
    if not path.is_file():
        raise ValueError(f"no such image: {path}")
    payload = path.read_bytes()
    if not payload:
        raise ValueError(f"empty image: {path}")
    if len(payload) > _MAX_IMAGE_BYTES:
        raise ValueError(f"image over {_MAX_IMAGE_BYTES} bytes: {path}")

    provider = get_provider(provider_name)
    manifest = getattr(provider, "manifest", {}) or {}
    if not manifest and provider_name:
        manifest = find_provider(provider_name) or {}
    if not manifest:
        rows = list_providers()
        manifest = next(
            (r for r in rows if r.get("name") == getattr(provider, "name", "")), {}
        )
    if not vision_enabled(manifest):
        raise ValueError(
            f"provider {getattr(provider, 'name', '?')!r} has no vision "
            "(vision: true / capabilities: [vision] / AGENTS_VISION=1)"
        )

    content = [
        {"type": "text", "text": (prompt or "").strip() or _DEFAULT_PROMPT},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"},
        },
    ]
    req = CompletionRequest(
        messages=[{"role": "user", "content": content}],
        model=model,
        extra={"stream": False},
    )
    result = provider.complete(req)
    return {
        "ok": True,
        "text": (result.text or "").strip(),
        "image": str(path),
        "provider": getattr(provider, "name", ""),
    }


_HELP_JSON = {
    "name": "mcp.vision.analyze",
    "description": "Analyze an image file with a vision-capable provider.",
    "commands": {
        "analyze": {
            "usage": "--image PATH [--prompt TEXT] [--provider NAME] [--model ID]",
            "side_effects": "read-only",
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Image path (png/jpg/jpeg/gif/webp)"},
                    "prompt": {"type": "string", "description": "What to look for (default: describe)"},
                    "provider": {"type": "string", "description": "Provider name (default: workspace pick)"},
                    "model": {"type": "string", "description": "Model id override"},
                },
                "required": ["image"],
            },
        },
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner.vision_analyze")
    parser.add_argument("--help-json", action="store_true")
    sub = parser.add_subparsers(dest="action")
    an = sub.add_parser("analyze")
    an.add_argument("image", nargs="?", default="")
    an.add_argument("--image", dest="image_flag", default="")
    an.add_argument("--prompt", default="")
    an.add_argument("--provider", default="")
    an.add_argument("--model", default="")
    # bare-flag form also works: --image PATH ...
    parser.add_argument("--image", dest="image", default="")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    args = parser.parse_args(argv)
    if args.help_json:
        print(json.dumps(_HELP_JSON, indent=2))
        return 0
    image = getattr(args, "image", "") or getattr(args, "image_flag", "")
    if not image:
        return _err("--image required")
    try:
        out = analyze(
            image,
            prompt=args.prompt,
            provider_name=args.provider,
            model=args.model,
        )
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        return _err(str(exc))
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
