"""Judge whether an agent used retrieved facts instead of asking the user.

The cases are ones where the answer is already in notes or a search result.
A reply passes when it contains the fact or a search was made. Asking for
that fact, with no search, fails. A case with no stored fact may ask.

This does not call a model. ``python -m runner.recall_eval`` runs the
built-in transcripts and two stub agents (one uses context or searches, one
always asks) and exits 0 when the judge matches every expected label.
``--transcript FILE`` scores replies for those case ids and exits 1 when an
info-exists case was asked instead of used or searched.

Turn recall on for real traffic only after transcripts you care about pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

_ASK = (
    re.compile(r"\bcould you\b", re.I),
    re.compile(r"\bcan you (tell|share|give)\b", re.I),
    re.compile(r"\bplease (tell|share|provide|confirm)\b", re.I),
    re.compile(r"\bwhat is your\b", re.I),
    re.compile(r"\bwhich .+ should i\b", re.I),
    re.compile(r"\bdo you know\b", re.I),
    re.compile(r"\?\s*$"),
)


@dataclass(frozen=True)
class RecallCase:
    id: str
    question: str
    fact: str
    corpus: str
    info_exists: bool


@dataclass(frozen=True)
class Judgment:
    label: str
    passed: bool
    used: bool
    searched: bool
    asked: bool


CASES: tuple[RecallCase, ...] = (
    RecallCase(
        id="canary-color",
        question="What color is the sandbox canary?",
        fact="ochre",
        corpus="The sandbox canary is ochre.",
        info_exists=True,
    ),
    RecallCase(
        id="relay-port",
        question="Which port does the relay listen on?",
        fact="8741",
        corpus="The relay listens on port 8741.",
        info_exists=True,
    ),
    RecallCase(
        id="cache-key",
        question="What is the cache directory setting called?",
        fact="cache_dir",
        corpus="The cache directory setting is cache_dir.",
        info_exists=True,
    ),
    RecallCase(
        id="favorite-tea",
        question="What is the user's favorite tea?",
        fact="",
        corpus="The relay listens on port 8741.",
        info_exists=False,
    ),
)


def asks_user(reply: str) -> bool:
    text = (reply or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _ASK)


def judge(*, fact: str, reply: str, searched: bool, info_exists: bool) -> Judgment:
    """Score one reply. ``used`` wins over ``searched``, which wins over ``asked``."""
    fact_text = (fact or "").strip().lower()
    reply_text = reply or ""
    used = bool(fact_text) and fact_text in reply_text.lower()
    asked = asks_user(reply_text)
    if used:
        return Judgment("used", True, True, searched, asked)
    if searched and info_exists:
        return Judgment("searched", True, False, True, asked)
    if not info_exists:
        label = "ask_ok" if asked else "abstained"
        return Judgment(label, True, False, searched, asked)
    if asked:
        return Judgment("asked", False, False, searched, True)
    return Judgment("miss", False, False, searched, asked)


def case_by_id(case_id: str) -> RecallCase | None:
    for case in CASES:
        if case.id == case_id:
            return case
    return None


def competent_reply(case: RecallCase, *, context: str, search: Callable[[], str]) -> tuple[str, bool]:
    """Use a fact already in context, else search, else ask."""
    if case.info_exists and case.fact.lower() in (context or "").lower():
        return f"The answer is {case.fact}.", False
    found = search() or ""
    if case.info_exists and case.fact.lower() in found.lower():
        return f"The answer is {case.fact}.", True
    return "Could you tell me?", False


def asking_reply(case: RecallCase, *, context: str, search: Callable[[], str]) -> tuple[str, bool]:
    _ = (case, context, search)
    return "Could you tell me?", False


@dataclass(frozen=True)
class EvalRow:
    agent: str
    case_id: str
    expect: str
    judgment: Judgment


def _search(case: RecallCase) -> Callable[[], str]:
    def run() -> str:
        if case.info_exists:
            return case.corpus
        return ""

    return run


def builtin_rows() -> list[EvalRow]:
    """Reference transcripts plus the two stub agents."""
    rows: list[EvalRow] = []
    canary = case_by_id("canary-color")
    assert canary is not None
    rows.append(
        EvalRow(
            "transcript",
            canary.id,
            "used",
            judge(fact=canary.fact, reply="The sandbox canary is ochre.", searched=False, info_exists=True),
        )
    )
    rows.append(
        EvalRow(
            "transcript",
            canary.id,
            "searched",
            judge(
                fact=canary.fact,
                reply="I looked through the notes and still need another pass.",
                searched=True,
                info_exists=True,
            ),
        )
    )
    rows.append(
        EvalRow(
            "transcript",
            canary.id,
            "asked",
            judge(
                fact=canary.fact,
                reply="Could you tell me the color?",
                searched=False,
                info_exists=True,
            ),
        )
    )
    tea = case_by_id("favorite-tea")
    assert tea is not None
    rows.append(
        EvalRow(
            "transcript",
            tea.id,
            "ask_ok",
            judge(fact=tea.fact, reply="Could you tell me?", searched=False, info_exists=False),
        )
    )
    for case in CASES:
        context = case.corpus if case.info_exists else ""
        reply, searched = competent_reply(case, context=context, search=_search(case))
        label = judge(
            fact=case.fact, reply=reply, searched=searched, info_exists=case.info_exists
        )
        expect = "used" if case.info_exists else "ask_ok"
        rows.append(EvalRow("uses-context", case.id, expect, label))
        reply, searched = asking_reply(case, context=context, search=_search(case))
        label = judge(
            fact=case.fact, reply=reply, searched=searched, info_exists=case.info_exists
        )
        expect = "asked" if case.info_exists else "ask_ok"
        rows.append(EvalRow("asks-user", case.id, expect, label))
        reply, searched = competent_reply(case, context="", search=_search(case))
        label = judge(
            fact=case.fact, reply=reply, searched=searched, info_exists=case.info_exists
        )
        expect = "used" if case.info_exists else "ask_ok"
        rows.append(EvalRow("searches", case.id, expect, label))
    return rows


def score_transcript(path: str) -> tuple[list[Judgment], list[str]]:
    """Score ``[{id, reply, searched}]``. Returns judgments and error strings."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("transcript must be a JSON list")
    judgments: list[Judgment] = []
    errors: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            errors.append("transcript row is not an object")
            continue
        case = case_by_id(str(item.get("id") or ""))
        if case is None:
            errors.append(f"unknown case {item.get('id')}")
            continue
        result = judge(
            fact=case.fact,
            reply=str(item.get("reply") or ""),
            searched=bool(item.get("searched")),
            info_exists=case.info_exists,
        )
        judgments.append(result)
        if case.info_exists and not result.passed:
            errors.append(f"{case.id} {result.label}")
    return judgments, errors


def format_row(row: EvalRow) -> str:
    mark = "pass" if row.judgment.label == row.expect else "FAIL"
    return (
        f"{mark:4} {row.agent:<14} {row.case_id:<16} "
        f"label={row.judgment.label:<8} expect={row.expect}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score search-before-asking cases")
    parser.add_argument(
        "--transcript",
        default="",
        help="JSON list of {id, reply, searched}. Exit 1 when an info-exists case fails.",
    )
    args = parser.parse_args(argv)
    if args.transcript:
        _judgments, errors = score_transcript(args.transcript)
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    failed = 0
    for row in builtin_rows():
        print(format_row(row))
        if row.judgment.label != row.expect:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
