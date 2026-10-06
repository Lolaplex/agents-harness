"""``--attach`` files on a user turn.

Images become OpenAI-style image parts when vision is on
(``AGENTS_VISION=1`` or the provider manifest sets ``vision`` / ``capabilities``).
Otherwise every attachment is a file path in the user text.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MAX_IMAGE_BYTES = 20_000_000


def vision_enabled(manifest: dict[str, Any] | None) -> bool:
    flag = os.environ.get("AGENTS_VISION", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    if not manifest:
        return False
    if manifest.get("vision") is True:
        return True
    caps = manifest.get("capabilities") or []
    return isinstance(caps, list) and "vision" in {str(item) for item in caps}


def render_user_content(
    text: str,
    attachments: list[str] | None = None,
    *,
    vision: bool = False,
) -> str | list[dict[str, Any]]:
    paths = [str(item) for item in (attachments or []) if str(item).strip()]
    if not paths:
        return text
    if not vision:
        lines = [text] if text else []
        lines.append("Attached files:")
        lines.extend(f"- {path}" for path in paths)
        return "\n".join(lines)

    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for raw in paths:
        path = Path(raw).expanduser()
        mime = IMAGE_TYPES.get(path.suffix.lower())
        payload = b""
        if mime and path.is_file():
            try:
                payload = path.read_bytes()
            except OSError:
                payload = b""
        if mime and payload and len(payload) <= _MAX_IMAGE_BYTES:
            encoded = base64.b64encode(payload).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )
            continue
        parts.append({"type": "text", "text": f"Attached file: {path}"})
    return parts
