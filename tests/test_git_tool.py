"""Git guards: default branch, force-push, and optional author checks."""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from runner.git_tool import (
    GitPolicy,
    actions_for_git,
    default_branch,
    guard_push,
    load_git_policy,
    main as git_main,
    parse_push,
)
from runner.helpjson import declared_argv
from runner.modules import find_module


def _base_env(host: Path, **extra: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env["AGENTS_HOST_DIR"] = str(host)
    env.update(extra)
    return env


def _run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, env, clear=True):
        with redirect_stdout(out), redirect_stderr(err):
            code = git_main(argv)
    return code, out.getvalue(), err.getvalue()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init(path: Path, branch: str = "trunk") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", branch, str(path)], check=True, capture_output=True)
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    (path / "README").write_text("hi\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")
    _git(path, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")


class ParseAndPolicy(unittest.TestCase):
    def test_force_refspec_is_not_a_remote_name(self):
        intent = parse_push(["+feature"])
        self.assertTrue(intent.force)
        self.assertEqual(intent.destinations, ["feature"])
        self.assertEqual(intent.remote, "")

    def test_delete_refspec_is_not_force_policy(self):
        intent = parse_push(["origin", ":topic"])
        self.assertTrue(intent.delete)
        self.assertFalse(intent.force)
        self.assertEqual(intent.destinations, ["topic"])

    def test_lease_and_branch_flag(self):
        intent = parse_push(["--force-with-lease", "--remote", "origin", "--branch", "topic"])
        self.assertTrue(intent.force)
        self.assertEqual(intent.remote, "origin")
        self.assertEqual(intent.destinations, ["topic"])

    def test_action_keys_name_the_branch(self):
        keys = actions_for_git(
            {"command": "push", "remote": "origin", "branch": "topic", "force": True}
        )
        self.assertIn("git.push:topic", keys)
        self.assertIn("git.force_push:topic", keys)
        self.assertIn("git.push", keys)

    def test_declared_commit_uses_message_flag(self):
        mod = find_module("git")
        self.assertIsNotNone(mod)
        argv = declared_argv(
            mod, {"command": "commit", "message": "hello", "repo": "/tmp/repo"}
        )
        self.assertEqual(argv[0], "commit")
        self.assertIn("--message", argv)
        self.assertIn("hello", argv)

    def test_default_policy_protects_token_and_blocks_force(self):
        with TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp))
            with patch.dict(os.environ, env, clear=True):
                policy = load_git_policy()
        self.assertEqual(policy.protected_tokens, ["$default"])
        self.assertEqual(policy.force_push, "deny")
        self.assertEqual(policy.author_mode, "off")
        self.assertEqual(policy.authors, [])

    def test_none_clears_protection_without_guessing_a_name(self):
        with TemporaryDirectory() as tmp:
            env = _base_env(Path(tmp), AGENTS_GIT_PROTECTED="none", AGENTS_GIT_FORCE_PUSH="allow")
            with patch.dict(os.environ, env, clear=True):
                policy = load_git_policy()
        self.assertEqual(policy.protected_tokens, [])
        self.assertEqual(policy.force_push, "allow")

    def test_unresolved_default_blocks_push(self):
        policy = GitPolicy(protected_tokens=["$default"], force_push="deny")
        intent = parse_push(["--branch", "topic"])
        with self.assertRaises(Exception) as caught:
            guard_push(intent, policy, current="topic", default="")
        self.assertIn("could not be detected", str(caught.exception))

    def test_explicit_list_does_not_protect_other_branches(self):
        policy = GitPolicy(protected_tokens=["release"], force_push="deny")
        intent = parse_push(["--branch", "topic"])
        guard_push(intent, policy, current="topic", default="trunk")
        blocked = parse_push(["--force", "--branch", "topic"])
        with self.assertRaises(Exception) as caught:
            guard_push(blocked, policy, current="topic", default="trunk")
        self.assertIn("force-push", str(caught.exception))

    def test_workspace_force_keeps_host_protected_list(self):
        from agents_workspace import Host

        with TemporaryDirectory() as tmp:
            host = Host(dir=Path(tmp) / "host")
            host.dir.mkdir(parents=True)
            host.agents_json.write_text(
                json.dumps(
                    {"git": {"protected_branches": ["release"], "force_push": "deny"}}
                ),
                encoding="utf-8",
            )
            ws = host.create("demo")
            ws.config.write_text(
                json.dumps({"git": {"force_push": "allow"}}),
                encoding="utf-8",
            )
            policy = load_git_policy(ws)
        self.assertEqual(policy.protected_tokens, ["release"])
        self.assertEqual(policy.force_push, "allow")
        self.assertEqual(policy.author_mode, "off")


class RepoGuards(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.host = self.root / "host"
        self.host.mkdir()
        self.repo = self.root / "repo"
        self.bare = self.root / "bare.git"
        _init(self.repo, "trunk")
        subprocess.run(["git", "init", "--bare", "-b", "trunk", str(self.bare)], check=True, capture_output=True)
        _git(self.repo, "remote", "add", "origin", str(self.bare))
        self.env = _base_env(self.host)

    def tearDown(self):
        self._tmp.cleanup()

    def _tool(self, *args: str, **extra: str) -> tuple[int, str, str]:
        env = dict(self.env)
        env.update(extra)
        return _run(list(args), env)

    def test_detected_default_is_trunk_not_a_guessed_name(self):
        self.assertEqual(default_branch(self.repo), "trunk")
        code, out, err = self._tool("policy", "--repo", str(self.repo))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["default_branch"], "trunk")
        self.assertEqual(payload["protected_branches"], ["trunk"])
        self.assertEqual(payload["force_push"], "deny")
        self.assertEqual(payload["author_mode"], "off")

    def test_push_to_default_is_refused_and_feature_lands(self):
        _git(self.repo, "checkout", "-b", "feature")
        (self.repo / "README").write_text("feature\n", encoding="utf-8")
        _git(self.repo, "commit", "-am", "feature")
        blocked, _out, err = self._tool(
            "push", "--repo", str(self.repo), "--remote", "origin", "--branch", "trunk"
        )
        self.assertEqual(blocked, 2)
        self.assertIn("protected branch trunk", err)
        code, _out, err = self._tool(
            "push", "--repo", str(self.repo), "--remote", "origin", "--branch", "feature"
        )
        self.assertEqual(code, 0, err)
        shown = subprocess.run(
            ["git", "--git-dir", str(self.bare), "rev-parse", "feature"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertTrue(shown.stdout.strip())

    def test_force_push_blocked_until_allowed_and_never_on_protected(self):
        _git(self.repo, "checkout", "-b", "feature")
        (self.repo / "README").write_text("one\n", encoding="utf-8")
        _git(self.repo, "commit", "-am", "one")
        code, _out, err = self._tool(
            "push", "--repo", str(self.repo), "--remote", "origin", "--branch", "feature"
        )
        self.assertEqual(code, 0, err)
        first = subprocess.run(
            ["git", "--git-dir", str(self.bare), "rev-parse", "feature"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _git(self.repo, "commit", "--amend", "-m", "rewritten")
        blocked, _out, err = self._tool(
            "push",
            "--repo",
            str(self.repo),
            "--remote",
            "origin",
            "--branch",
            "feature",
            "--force",
        )
        self.assertEqual(blocked, 2)
        self.assertIn("force-push", err)
        still = subprocess.run(
            ["git", "--git-dir", str(self.bare), "rev-parse", "feature"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(still, first)
        allowed, _out, err = self._tool(
            "push",
            "--repo",
            str(self.repo),
            "--remote",
            "origin",
            "--branch",
            "feature",
            "--force",
            AGENTS_GIT_FORCE_PUSH="allow",
        )
        self.assertEqual(allowed, 0, err)
        moved = subprocess.run(
            ["git", "--git-dir", str(self.bare), "rev-parse", "feature"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotEqual(moved, first)
        protected, _out, err = self._tool(
            "push",
            "--repo",
            str(self.repo),
            "--remote",
            "origin",
            "--branch",
            "trunk",
            "--force",
            AGENTS_GIT_FORCE_PUSH="allow",
        )
        self.assertEqual(protected, 2)
        self.assertIn("protected branch trunk", err)

    def test_branch_delete_of_default_is_refused(self):
        _git(self.repo, "checkout", "-b", "feature")
        code, _out, err = self._tool("branch", "--repo", str(self.repo), "--delete", "--name", "trunk")
        self.assertEqual(code, 2)
        self.assertIn("protected branch trunk", err)
        listed, out, err = self._tool("branch", "--repo", str(self.repo))
        self.assertEqual(listed, 0, err)
        self.assertIn("trunk", out)

    def test_author_off_uses_repo_identity(self):
        (self.repo / "note.txt").write_text("n\n", encoding="utf-8")
        _git(self.repo, "add", "note.txt")
        code, _out, err = self._tool(
            "commit", "--repo", str(self.repo), "--message", "note"
        )
        self.assertEqual(code, 0, err)
        shown = _git(self.repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>")
        self.assertEqual(
            shown.stdout.strip(),
            "Test User <test@example.com>|Test User <test@example.com>",
        )

    def test_allow_rejects_other_identity_and_require_sets_both_without_config_write(self):
        (self.repo / "note.txt").write_text("n\n", encoding="utf-8")
        _git(self.repo, "add", "note.txt")
        authors = "Ada Lovelace <ada@example.com>"
        blocked, _out, err = self._tool(
            "commit",
            "--repo",
            str(self.repo),
            "--message",
            "note",
            AGENTS_GIT_AUTHORS=authors,
            AGENTS_GIT_AUTHOR_MODE="allow",
        )
        self.assertEqual(blocked, 2)
        self.assertIn("not in the allowed list", err)
        self.assertNotIn("note", _git(self.repo, "log", "-1", "--format=%s").stdout)
        code, _out, err = self._tool(
            "commit",
            "--repo",
            str(self.repo),
            "--message",
            "note",
            AGENTS_GIT_AUTHORS=authors,
            AGENTS_GIT_AUTHOR_MODE="require",
        )
        self.assertEqual(code, 0, err)
        shown = _git(self.repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>")
        self.assertEqual(
            shown.stdout.strip(),
            "Ada Lovelace <ada@example.com>|Ada Lovelace <ada@example.com>",
        )
        self.assertEqual(_git(self.repo, "config", "user.name").stdout.strip(), "Test User")
        self.assertEqual(_git(self.repo, "config", "user.email").stdout.strip(), "test@example.com")

    def test_single_branch_is_the_default_when_origin_head_is_missing(self):
        repo = self.root / "solo"
        _init(repo, "topic")
        _git(repo, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        self.assertEqual(default_branch(repo), "topic")
