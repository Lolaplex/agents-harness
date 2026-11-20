"""a2a.peer: POST to the relay ``/v1/a2a/mail`` proxy (cross-host board DID mail).

This is for *cross-host* agents-board mailbox traffic, not intra-workspace
persona chat — those use ``persona.*`` modules.

URL resolution follows the relay env chain: ``RELAY_URL``, else
``http://127.0.0.1:$RELAY_PORT``, else ``GATEWAY_URL`` (deprecated fallback),
default port 8787. Secret: ``RELAY_SECRET`` then ``GATEWAY_SECRET`` sent as
``X-Relay-Secret``. Body comes from argv JSON or ``A2A_BODY`` (default
``{"verb": "mail.get"}``). Transport failure exits non-zero with
``{"ok": false, ...}`` so receipts never record a fake delivery.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def relay_url() -> str:
    base = (os.environ.get("RELAY_URL") or "").strip().rstrip("/")
    if not base:
        port = (os.environ.get("RELAY_PORT") or "").strip()
        if port:
            base = f"http://127.0.0.1:{port}"
        else:
            base = (os.environ.get("GATEWAY_URL") or "http://127.0.0.1:8787").strip().rstrip("/")
    return base


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    raw = args[0] if args else os.environ.get("A2A_BODY", "")
    try:
        body = json.loads(raw) if raw and raw.strip() else {"verb": "mail.get"}
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except (ValueError, TypeError):
        print(json.dumps({"ok": False, "error": "invalid JSON body"}))
        return 2

    headers = {"Content-Type": "application/json"}
    secret = (os.environ.get("RELAY_SECRET") or os.environ.get("GATEWAY_SECRET") or "").strip()
    if secret:
        headers["X-Relay-Secret"] = secret
    url = relay_url() + "/v1/a2a/mail"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        print(json.dumps({"ok": False, "error": f"http {exc.code}", "detail": detail}))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
