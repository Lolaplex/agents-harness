"""Guarded git tool. The executor stays a verb runner; the guards live here.

Neutral defaults, with no branch name and no author baked in:

- The repository's default branch is protected when git records it
  (``origin/HEAD``). A repository with exactly one local branch uses that
  branch. A name such as ``main`` is never assumed. Push and delete of a
  protected branch are refused.
- Force-push (``--force``, ``-f``, ``--force-with-lease``,
  ``--force-if-includes``, a ``+`` refspec, ``--mirror``) is refused.
- Commit author and committer are checked only when a list is configured.

``$default`` in the protected list means the detected default branch.
``AGENTS_GIT_PROTECTED=none`` protects nothing. An explicit list replaces
the default list. Force-push to a protected branch stays refused when
``AGENTS_GIT_FORCE_PUSH=allow``.

Config, field by field, most specific wins: host ``agents.json`` key
``git``, workspace ``config.json`` key ``git``, then the environment.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .helpjson import unwrap_arguments
from .workspace_config import merged_section

_IDENT = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^>]+)>$")
_IDENT_LINE = re.compile(
    r"^(?P<name>.*?)\s*<(?P<email>[^>]*)>\s+\d+\s+[+-]\d+\s*$"
)
_DEFAULT = "$default"
_PUSH_VALUE = {
    "--repo",
    "--remote",
    "--branch",
}
_PUSH_BOOL = {
    "--force",
    "-f",
    "--force-with-lease",
    "--force-if-includes",
    "--delete",
    "--mirror",
    "--dry-run",
    "--set-upstream",
    "-u",
    "--tags",
    "--porcelain",
    "--progress",
    "--verbose",
    "-v",
    "--quiet",
    "-q",
}
_LOG_MAX = 500
_LOG_DEFAULT = 50

GUARD_EXIT = 2


class GitToolError(Exception):
    """A guard refusal or a bad invocation. ``exit_code`` is 2."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message if str(message).startswith("refused:") else f"refused: {message}"


@dataclass(frozen=True)
class Identity:
    name: str
    email: str

    def render(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass
class GitPolicy:
    protected_tokens: list[str] = field(default_factory=lambda: [_DEFAULT])
    force_push: str = "deny"
    authors: list[Identity] = field(default_factory=list)
    author_mode: str = "off"

    def protected_names(self, default_branch: str) -> set[str]:
        names: set[str] = set()
        for token in self.protected_tokens:
            if token == _DEFAULT:
                if default_branch:
                    names.add(default_branch)
                continue
            if token:
                names.add(token)
        return names


@dataclass
class PushIntent:
    remote: str = ""
    destinations: list[str] = field(default_factory=list)
    force: bool = False
    delete: bool = False
    mirror: bool = False
    dry_run: bool = False
    repo: str = ""
    git_args: list[str] = field(default_factory=list)


def parse_identity(value: str) -> Identity | None:
    text = (value or "").strip()
    match = _IDENT.match(text)
    if not match:
        return None
    name = match.group("name").strip()
    email = match.group("email").strip()
    if not name or not email or any(ch in name or ch in email for ch in "\r\n"):
        return None
    return Identity(name=name, email=email)


def _parse_identity_value(value: Any) -> Identity | None:
    if isinstance(value, str):
        return parse_identity(value)
    if isinstance(value, dict):
        name = str(value.get("name") or "").strip()
        email = str(value.get("email") or "").strip()
        if name and email:
            return parse_identity(f"{name} <{email}>")
    return None


def _split_authors(raw: str) -> list[Identity]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitToolError("author list is not valid JSON") from exc
        if not isinstance(data, list):
            raise GitToolError("author list must be a JSON array")
        found = [_parse_identity_value(item) for item in data]
        if any(item is None for item in found):
            raise GitToolError("author entry must be 'Name <email>'")
        return [item for item in found if item is not None]
    found = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        ident = parse_identity(part)
        if ident is None:
            raise GitToolError("author entry must be 'Name <email>'")
        found.append(ident)
    return found


def _protected_from_value(value: Any) -> list[str]:
    if value is None:
        return [_DEFAULT]
    if isinstance(value, str):
        token = value.strip()
        if not token or token.lower() == "none":
            return []
        return [part.strip() for part in token.split(",") if part.strip()]
    if isinstance(value, list):
        tokens: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                tokens.append(text)
        if len(tokens) == 1 and tokens[0].lower() == "none":
            return []
        return tokens
    raise GitToolError("protected_branches must be a list or a string")


def load_git_policy(workspace=None) -> GitPolicy:
    """Resolve git guards. Environment overrides one field at a time."""
    section = merged_section("git", workspace)
    protected = _protected_from_value(section.get("protected_branches", [_DEFAULT]))
    force = str(section.get("force_push") or "deny").strip().lower()
    author_mode = str(section.get("author_mode") or "").strip().lower()
    authors: list[Identity] = []
    if "authors" in section and section.get("authors") not in (None, ""):
        raw_authors = section.get("authors")
        if isinstance(raw_authors, str):
            authors = _split_authors(raw_authors)
        elif isinstance(raw_authors, list):
            parsed = [_parse_identity_value(item) for item in raw_authors]
            if any(item is None for item in parsed):
                raise GitToolError("author entry must be 'Name <email>'")
            authors = [item for item in parsed if item is not None]
        else:
            raise GitToolError("authors must be a list of 'Name <email>'")
    if "AGENTS_GIT_PROTECTED" in os.environ:
        protected = _protected_from_value(os.environ.get("AGENTS_GIT_PROTECTED", ""))
    if os.environ.get("AGENTS_GIT_FORCE_PUSH", "").strip():
        force = os.environ["AGENTS_GIT_FORCE_PUSH"].strip().lower()
    if "AGENTS_GIT_AUTHORS" in os.environ:
        authors = _split_authors(os.environ.get("AGENTS_GIT_AUTHORS", ""))
    if os.environ.get("AGENTS_GIT_AUTHOR_MODE", "").strip():
        author_mode = os.environ["AGENTS_GIT_AUTHOR_MODE"].strip().lower()
    if force not in ("deny", "allow"):
        force = "deny"
    if author_mode not in ("off", "allow", "require"):
        author_mode = "allow" if authors else "off"
    if not author_mode:
        author_mode = "allow" if authors else "off"
    if author_mode == "allow" and not authors:
        author_mode = "off"
    return GitPolicy(
        protected_tokens=protected,
        force_push=force,
        authors=authors,
        author_mode=author_mode,
    )


def _short_branch(ref: str) -> str:
    text = (ref or "").strip()
    if text.startswith("refs/heads/"):
        return text[len("refs/heads/") :]
    return text


def default_branch(repo: Path) -> str:
    """The repo default branch, or ``""`` when it cannot be known.

    ``origin/HEAD`` wins. Otherwise a repository with a single local branch
    uses that branch. No fallback name is guessed.
    """
    code, out, _err = _git_text(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
    if code == 0 and out.strip():
        name = out.strip()
        if name.startswith("origin/"):
            name = name[len("origin/") :]
        return _short_branch(name)
    code, out, _err = _git_text(["for-each-ref", "--format=%(refname:short)", "refs/heads"], repo)
    if code != 0:
        return ""
    names = [line.strip() for line in out.splitlines() if line.strip()]
    if len(names) == 1:
        return names[0]
    return ""


def current_branch(repo: Path) -> str:
    code, out, _err = _git_text(["symbolic-ref", "--short", "HEAD"], repo)
    if code != 0:
        return ""
    return out.strip()


def _parse_refspec(spec: str) -> tuple[bool, str, bool]:
    """Return ``(force, destination, delete)`` for one refspec."""
    force = spec.startswith("+")
    body = spec[1:] if force else spec
    if ":" not in body:
        return force, _short_branch(body), False
    src, dst = body.split(":", 1)
    if src == "":
        return force, _short_branch(dst), True
    return force, _short_branch(dst), False


def parse_push(argv: list[str]) -> PushIntent:
    """Parse push argv (without the leading ``push`` token)."""
    intent = PushIntent()
    positionals: list[str] = []
    unknown: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positionals.extend(argv[index + 1 :])
            break
        if token.startswith("--force-with-lease"):
            intent.force = True
            index += 1
            continue
        if token in _PUSH_VALUE:
            if index + 1 >= len(argv):
                raise GitToolError(f"push flag {token} needs a value")
            value = argv[index + 1]
            if token == "--repo":
                intent.repo = value
            elif token == "--remote":
                intent.remote = value
            elif token == "--branch":
                intent.destinations.append(_short_branch(value))
            index += 2
            continue
        if token in _PUSH_BOOL:
            if token in ("--force", "-f", "--force-if-includes"):
                intent.force = True
            elif token == "--delete":
                intent.delete = True
            elif token == "--mirror":
                intent.mirror = True
                intent.force = True
            elif token == "--dry-run":
                intent.dry_run = True
            index += 1
            continue
        if token.startswith("-"):
            unknown.append(token)
            index += 1
            continue
        positionals.append(token)
        index += 1
    if unknown:
        raise GitToolError(f"unsupported push flag {unknown[0]}")
    if positionals:
        first = positionals[0]
        if intent.remote or first.startswith("+") or ":" in first:
            specs = positionals
        else:
            intent.remote = first
            specs = positionals[1:]
        for spec in specs:
            force, dest, delete = _parse_refspec(spec)
            if force:
                intent.force = True
            if delete:
                intent.delete = True
            if dest:
                intent.destinations.append(dest)
    intent.git_args = list(argv)
    return intent


def resolve_push_destinations(intent: PushIntent, *, current: str) -> list[str]:
    if intent.mirror:
        return []
    dests = []
    for dest in intent.destinations:
        if dest == "HEAD":
            if current:
                dests.append(current)
            continue
        dests.append(dest)
    if not dests and not intent.mirror:
        if current:
            dests.append(current)
    return dests


def guard_push(
    intent: PushIntent,
    policy: GitPolicy,
    *,
    current: str,
    default: str,
) -> None:
    """Refuse a push that breaks the guards. Dry-run is not a mutation."""
    if intent.dry_run:
        return
    if intent.mirror:
        raise GitToolError("mirror push is blocked")
    destinations = resolve_push_destinations(intent, current=current)
    protected = policy.protected_names(default)
    unresolved = _DEFAULT in policy.protected_tokens and not default
    if unresolved:
        raise GitToolError(
            "default branch could not be detected; push blocked until "
            "origin/HEAD is set or AGENTS_GIT_PROTECTED names branches"
        )
    for dest in destinations:
        if dest in protected:
            raise GitToolError(f"push to protected branch {dest}")
    if intent.delete:
        for dest in destinations:
            if dest in protected:
                raise GitToolError(f"delete of protected branch {dest}")
    if intent.force and policy.force_push != "allow":
        target = destinations[0] if destinations else "the remote"
        raise GitToolError(f"force-push is blocked ({target})")
    if intent.force:
        for dest in destinations:
            if dest in protected:
                raise GitToolError(f"force-push to protected branch {dest}")


def guard_branch_delete(name: str, policy: GitPolicy, *, default: str) -> None:
    protected = policy.protected_names(default)
    if _DEFAULT in policy.protected_tokens and not default:
        raise GitToolError(
            "default branch could not be detected; branch delete blocked"
        )
    if name in protected:
        raise GitToolError(f"delete of protected branch {name}")


def identity_allowed(ident: Identity, allowed: list[Identity]) -> bool:
    for item in allowed:
        if item.name == ident.name and item.email.lower() == ident.email.lower():
            return True
    return False


def guard_commit(
    policy: GitPolicy,
    *,
    author: Identity | None,
    committer: Identity | None,
    requested_author: Identity | None,
) -> Identity | None:
    """Return the identity to inject, or None when git's own identity is used.

    ``author`` and ``committer`` are the identities git would write.
    ``requested_author`` is an explicit ``--author``. A return value means
    require-mode: set both author and committer to that identity for the
    subprocess only.
    """
    mode = policy.author_mode
    if mode == "off":
        return None
    if mode == "require":
        if len(policy.authors) != 1:
            raise GitToolError("require author mode needs exactly one 'Name <email>'")
        required = policy.authors[0]
        if requested_author is not None and not identity_allowed(requested_author, [required]):
            raise GitToolError(
                f"commit author {requested_author.render()} is not {required.render()}"
            )
        return required
    if requested_author is not None and not identity_allowed(requested_author, policy.authors):
        raise GitToolError(
            f"commit author {requested_author.render()} is not in the allowed list"
        )
    if author is None or committer is None:
        raise GitToolError("git author or committer is not set")
    if not identity_allowed(author, policy.authors):
        raise GitToolError(f"commit author {author.render()} is not in the allowed list")
    if not identity_allowed(committer, policy.authors):
        raise GitToolError(
            f"commit committer {committer.render()} is not in the allowed list"
        )
    return None


def _git_env(identity: Identity | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if identity is not None:
        env["GIT_AUTHOR_NAME"] = identity.name
        env["GIT_AUTHOR_EMAIL"] = identity.email
        env["GIT_COMMITTER_NAME"] = identity.name
        env["GIT_COMMITTER_EMAIL"] = identity.email
    return env


def _git(
    args: list[str],
    repo: Path | None,
    *,
    identity: Identity | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if repo is not None:
        cmd.extend(["-C", str(repo)])
    cmd.extend(args)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(identity),
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitToolError(f"git timed out: {' '.join(args[:4])}") from exc
    except OSError as exc:
        raise GitToolError(f"git could not be started: {exc}") from exc


def _git_text(args: list[str], repo: Path | None) -> tuple[int, str, str]:
    try:
        proc = _git(args, repo, timeout=15)
    except GitToolError:
        return 1, "", ""
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _parse_var_ident(text: str) -> Identity | None:
    match = _IDENT_LINE.match((text or "").strip())
    if not match:
        return None
    name = match.group("name").strip()
    email = match.group("email").strip()
    if not name or not email:
        return None
    return Identity(name=name, email=email)


def _read_ident(repo: Path, which: str) -> Identity | None:
    code, out, _err = _git_text(["var", which], repo)
    if code != 0:
        return None
    return _parse_var_ident(out)


def _resolve_repo(explicit: str) -> Path:
    raw = (explicit or "").strip() or os.environ.get("AGENTS_GIT_REPO", "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        code, out, _err = _git_text(["rev-parse", "--show-toplevel"], None)
        if code != 0 or not out.strip():
            raise GitToolError("not a git repository (pass --repo or AGENTS_GIT_REPO)")
        path = Path(out.strip()).resolve()
    if not path.exists():
        raise GitToolError(f"repository does not exist: {path}")
    code, _out, _err = _git_text(["rev-parse", "--is-inside-work-tree"], path)
    if code != 0:
        raise GitToolError(f"not a git repository: {path}")
    return path


def _print_proc(proc: subprocess.CompletedProcess[str]) -> int:
    if proc.stdout:
        sys.stdout.write(proc.stdout if proc.stdout.endswith("\n") else proc.stdout + "\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr if proc.stderr.endswith("\n") else proc.stderr + "\n")
    return int(proc.returncode)


def _take(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Split known ``--flag value`` / boolean flags from positionals.

    Unknown flags are returned in ``options['__unknown__']``.
    """
    valued = {
        "--repo",
        "--message",
        "-m",
        "--author",
        "--remote",
        "--branch",
        "--max-count",
        "--rev",
        "--name",
    }
    boolean = {
        "--force",
        "-f",
        "--force-with-lease",
        "--force-if-includes",
        "--delete",
        "--force-delete",
        "-d",
        "-D",
        "--staged",
        "--short",
        "--dry-run",
        "--set-upstream",
        "-u",
        "--mirror",
        "--tags",
        "--all",
        "-A",
        "--update",
        "-u",
    }
    options: dict[str, Any] = {}
    positionals: list[str] = []
    unknown: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positionals.extend(argv[index + 1 :])
            break
        if token.startswith("--force-with-lease"):
            options["force"] = True
            options["force_with_lease"] = True
            index += 1
            continue
        if token in valued and token not in ("--force-with-lease",):
            if index + 1 >= len(argv):
                raise GitToolError(f"{token} needs a value")
            key = token.lstrip("-").replace("-", "_")
            if token == "-m":
                key = "message"
            options[key] = argv[index + 1]
            index += 2
            continue
        if token in boolean:
            if token in ("-d", "--delete"):
                options["delete"] = True
            elif token in ("-D", "--force-delete"):
                options["delete"] = True
                options["force_delete"] = True
            elif token in ("-f", "--force", "--force-if-includes"):
                options["force"] = True
            elif token == "-A":
                options["all"] = True
            elif token == "-u" and "set_upstream" not in options and "update" not in options:
                options.setdefault("update", True)
            else:
                options[token.lstrip("-").replace("-", "_")] = True
            index += 1
            continue
        if token.startswith("-"):
            unknown.append(token)
            index += 1
            continue
        positionals.append(token)
        index += 1
    if unknown:
        options["__unknown__"] = unknown
    return options, positionals


def _policy_payload(policy: GitPolicy, repo: Path | None) -> dict[str, Any]:
    default = default_branch(repo) if repo is not None else ""
    return {
        "protected_tokens": list(policy.protected_tokens),
        "protected_branches": sorted(policy.protected_names(default)),
        "default_branch": default,
        "default_branch_resolved": bool(default),
        "force_push": policy.force_push,
        "author_mode": policy.author_mode,
        "authors": [item.render() for item in policy.authors],
    }


def _cmd_policy(rest: list[str]) -> int:
    options, _positionals = _take(rest)
    unknown = options.pop("__unknown__", [])
    if unknown:
        raise GitToolError(f"unsupported policy flag {unknown[0]}")
    repo = None
    if options.get("repo") or os.environ.get("AGENTS_GIT_REPO", "").strip():
        repo = _resolve_repo(str(options.get("repo") or ""))
    payload = _policy_payload(load_git_policy(), repo)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_status(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported status flag {options['__unknown__'][0]}")
    if positionals:
        raise GitToolError("status does not take a path")
    repo = _resolve_repo(str(options.get("repo") or ""))
    return _print_proc(_git(["status", "--short", "--branch"], repo))


def _cmd_diff(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported diff flag {options['__unknown__'][0]}")
    repo = _resolve_repo(str(options.get("repo") or ""))
    args = ["diff"]
    if options.get("staged"):
        args.append("--staged")
    args.extend(positionals)
    return _print_proc(_git(args, repo))


def _cmd_log(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported log flag {options['__unknown__'][0]}")
    repo = _resolve_repo(str(options.get("repo") or ""))
    raw = options.get("max_count", _LOG_DEFAULT)
    try:
        count = int(raw)
    except (TypeError, ValueError) as exc:
        raise GitToolError("max-count must be an integer") from exc
    if count < 1 or count > _LOG_MAX:
        raise GitToolError(f"max-count must be from 1 to {_LOG_MAX}")
    args = ["log", f"-n{count}", "--format=%h %s"]
    args.extend(positionals)
    return _print_proc(_git(args, repo))


def _cmd_show(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported show flag {options['__unknown__'][0]}")
    repo = _resolve_repo(str(options.get("repo") or ""))
    rev = str(options.get("rev") or (positionals[0] if positionals else "HEAD"))
    extra = positionals[1:] if not options.get("rev") and positionals else positionals
    if options.get("rev"):
        extra = positionals
    return _print_proc(_git(["show", "--no-ext-diff", "--format=medium", rev, *extra], repo))


def _cmd_rev_parse(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported rev-parse flag {options['__unknown__'][0]}")
    allowed = {"--abbrev-ref", "--short", "--show-toplevel", "--verify", "--quiet"}
    flags = [item for item in rest if item in allowed]
    repo = _resolve_repo(str(options.get("repo") or ""))
    revs = [item for item in positionals if not item.startswith("-")]
    if not flags and not revs:
        revs = ["HEAD"]
    return _print_proc(_git(["rev-parse", *flags, *revs], repo))


def _cmd_add(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported add flag {options['__unknown__'][0]}")
    repo = _resolve_repo(str(options.get("repo") or ""))
    args = ["add"]
    if options.get("all"):
        args.append("--all")
    elif options.get("update"):
        args.append("--update")
    if not positionals and not options.get("all") and not options.get("update"):
        raise GitToolError("add needs a path")
    if positionals:
        args.append("--")
        args.extend(positionals)
    return _print_proc(_git(args, repo))


def _cmd_commit(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported commit flag {options['__unknown__'][0]}")
    if positionals:
        raise GitToolError("commit message must be passed with --message")
    message = str(options.get("message") or "").strip()
    if not message:
        raise GitToolError("commit needs --message")
    repo = _resolve_repo(str(options.get("repo") or ""))
    policy = load_git_policy()
    requested = None
    if options.get("author"):
        requested = parse_identity(str(options["author"]))
        if requested is None:
            raise GitToolError("author must be 'Name <email>'")
    inject = guard_commit(
        policy,
        author=_read_ident(repo, "GIT_AUTHOR_IDENT"),
        committer=_read_ident(repo, "GIT_COMMITTER_IDENT"),
        requested_author=requested,
    )
    args = ["commit", "--message", message]
    if requested is not None and inject is None:
        args.extend(["--author", requested.render()])
    return _print_proc(_git(args, repo, identity=inject))


def _push_git_args(intent: PushIntent) -> list[str]:
    """Rebuild a git push argv from a parsed intent, dropping tool-only flags."""
    args = ["push"]
    if intent.dry_run:
        args.append("--dry-run")
    if intent.delete:
        args.append("--delete")
    if intent.force:
        if "--force-with-lease" in intent.git_args or any(
            item.startswith("--force-with-lease") for item in intent.git_args
        ):
            args.append("--force-with-lease")
        elif "--force-if-includes" in intent.git_args:
            args.append("--force-if-includes")
        else:
            args.append("--force")
    if "--set-upstream" in intent.git_args or "-u" in intent.git_args:
        args.append("--set-upstream")
    if intent.remote:
        args.append(intent.remote)
    if intent.destinations and not intent.delete:
        args.extend(intent.destinations)
    elif intent.delete and intent.destinations:
        args.extend(intent.destinations)
    return args


def _cmd_push(rest: list[str]) -> int:
    intent = parse_push(rest)
    repo = _resolve_repo(intent.repo)
    policy = load_git_policy()
    guard_push(
        intent,
        policy,
        current=current_branch(repo),
        default=default_branch(repo),
    )
    return _print_proc(_git(_push_git_args(intent), repo, timeout=60))


def _cmd_checkout(rest: list[str], *, create: bool = False) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported checkout flag {options['__unknown__'][0]}")
    if options.get("force") or options.get("delete"):
        raise GitToolError("checkout cannot discard files or take --force")
    repo = _resolve_repo(str(options.get("repo") or ""))
    if not positionals:
        raise GitToolError("checkout needs a branch")
    if len(positionals) != 1:
        raise GitToolError("checkout takes one branch, not a path")
    ref = positionals[0]
    if ref.startswith("-"):
        raise GitToolError("checkout branch must not look like a flag")
    args = ["switch"]
    if create or options.get("create"):
        args.append("--create")
    args.append(ref)
    return _print_proc(_git(args, repo))


def _cmd_branch(rest: list[str]) -> int:
    options, positionals = _take(rest)
    if options.get("__unknown__"):
        raise GitToolError(f"unsupported branch flag {options['__unknown__'][0]}")
    repo = _resolve_repo(str(options.get("repo") or ""))
    name = str(options.get("name") or (positionals[0] if positionals else ""))
    if options.get("delete"):
        if not name:
            raise GitToolError("branch delete needs a name")
        policy = load_git_policy()
        guard_branch_delete(name, policy, default=default_branch(repo))
        flag = "-D" if options.get("force_delete") else "-d"
        return _print_proc(_git(["branch", flag, name], repo))
    if name:
        return _print_proc(_git(["branch", name], repo))
    return _print_proc(_git(["branch", "--list"], repo))


_COMMANDS = {
    "policy": _cmd_policy,
    "status": _cmd_status,
    "diff": _cmd_diff,
    "log": _cmd_log,
    "show": _cmd_show,
    "rev-parse": _cmd_rev_parse,
    "add": _cmd_add,
    "commit": _cmd_commit,
    "push": _cmd_push,
    "branch": _cmd_branch,
}


def help_json() -> dict[str, Any]:
    """Command metadata. Embedded in the module manifest; this stays in sync."""
    return {
        "commands": {
            "policy": {"side_effects": "read-only", "action": "git.policy"},
            "status": {"side_effects": "read-only", "action": "git.status"},
            "diff": {
                "side_effects": "read-only",
                "action": "git.diff",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "staged": {"type": "boolean"},
                    },
                },
            },
            "log": {"side_effects": "read-only", "action": "git.log"},
            "show": {"side_effects": "read-only", "action": "git.show"},
            "rev-parse": {"side_effects": "read-only", "action": "git.rev_parse"},
            "add": {"side_effects": "mutating", "action": "git.add"},
            "commit": {"side_effects": "mutating", "action": "git.commit"},
            "push": {"side_effects": "mutating", "action": "git.push"},
            "checkout": {
                "side_effects": "mutating",
                "action": "git.checkout",
                "aliases": ["switch"],
            },
            "branch": {"side_effects": "mutating", "action": "git.branch"},
        }
    }


def arguments_to_git_argv(arguments: Any) -> list[str]:
    """Normalize a tool-call payload to ``[command, ...]``."""
    args = unwrap_arguments(arguments)
    if isinstance(args, list):
        return [str(item) for item in args]
    if isinstance(args, str):
        text = args.strip()
        return text.split() if text else []
    if not isinstance(args, dict):
        return []
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        return [str(item) for item in argv]
    command = ""
    for key in ("command", "command_name", "subcommand", "cmd"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            command = val.strip()
            break
    if not command:
        return []
    out = [command]
    if args.get("repo"):
        out.extend(["--repo", str(args["repo"])])
    if command == "push":
        if args.get("force") in (True, "true", "1", 1):
            out.append("--force")
        if args.get("force_with_lease") in (True, "true", "1", 1):
            out.append("--force-with-lease")
        if args.get("delete") in (True, "true", "1", 1):
            out.append("--delete")
        if args.get("remote"):
            out.extend(["--remote", str(args["remote"])])
        if args.get("branch"):
            out.extend(["--branch", str(args["branch"])])
        refspec = args.get("refspec")
        if isinstance(refspec, str) and refspec.strip():
            out.append(refspec.strip())
        elif isinstance(refspec, list):
            out.extend(str(item) for item in refspec)
    elif command == "commit":
        if args.get("message"):
            out.extend(["--message", str(args["message"])])
        if args.get("author"):
            out.extend(["--author", str(args["author"])])
    elif command == "add":
        paths = args.get("paths") or args.get("path")
        if isinstance(paths, str):
            out.append(paths)
        elif isinstance(paths, list):
            out.extend(str(item) for item in paths)
    elif command in ("checkout", "switch"):
        ref = args.get("ref") or args.get("branch")
        if ref:
            out.append(str(ref))
    elif command == "branch":
        if args.get("delete") in (True, "true", "1", 1):
            out.append("--delete")
        if args.get("name"):
            out.extend(["--name", str(args["name"])])
    elif command == "diff" and args.get("staged") in (True, "true", "1", 1):
        out.append("--staged")
    elif command == "log" and args.get("max_count") not in (None, ""):
        out.extend(["--max-count", str(args["max_count"])])
    elif command == "show" and args.get("rev"):
        out.extend(["--rev", str(args["rev"])])
    return out


def actions_for_git(arguments: Any) -> list[str]:
    """Action keys for the approval policy. No git process is started."""
    argv = arguments_to_git_argv(arguments)
    if not argv:
        return ["git"]
    op = argv[0]
    keys = ["git", f"git.{op.replace('-', '_')}"]
    if op == "push":
        try:
            intent = parse_push(argv[1:])
        except GitToolError:
            keys.append("git.push")
            return _dedupe(keys)
        keys.append("git.push")
        if intent.force:
            keys.append("git.force_push")
        if intent.delete:
            keys.append("git.delete")
        for dest in intent.destinations:
            keys.append(f"git.push:{dest}")
            if intent.force:
                keys.append(f"git.force_push:{dest}")
            if intent.delete:
                keys.append(f"git.delete:{dest}")
    elif op == "commit":
        keys.append("git.commit")
    elif op == "branch":
        try:
            options, positionals = _take(argv[1:])
        except GitToolError:
            return _dedupe(keys)
        if options.get("delete"):
            keys.append("git.branch_delete")
            name = str(options.get("name") or (positionals[0] if positionals else ""))
            if name:
                keys.append(f"git.branch_delete:{name}")
    return _dedupe(keys)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--help-json"]:
        print(json.dumps(help_json(), ensure_ascii=False, indent=2))
        return 0
    if not args or args[0] in ("-h", "--help"):
        print(
            "usage: python -m runner.git_tool "
            "{policy|status|diff|log|show|rev-parse|add|commit|push|checkout|branch}",
            file=sys.stderr,
        )
        return 2 if not args else 0
    op = args[0]
    handler = _COMMANDS.get(op)
    if op in ("checkout", "switch"):
        try:
            return _cmd_checkout(args[1:])
        except GitToolError as exc:
            print(exc.message, file=sys.stderr)
            print(exc.message)
            return GUARD_EXIT
    if handler is None:
        print(f"refused: unknown git command {op}", file=sys.stderr)
        print(f"refused: unknown git command {op}")
        return GUARD_EXIT
    try:
        return handler(args[1:])
    except GitToolError as exc:
        print(exc.message, file=sys.stderr)
        print(exc.message)
        return GUARD_EXIT


if __name__ == "__main__":
    sys.exit(main())
