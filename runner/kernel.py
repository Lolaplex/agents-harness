"""Check and reduce job terms. Mix peels the static spine.

Catalog JSON (modules + schedules) is the alphabet. The kernel rejects
names that are not in it, then reduces. Combinator steps follow
Kernel.lean IStep. Atom β is execute_job (Cordis) — the dialect's extra.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from .executor import execute_job, list_schedules
from .iterm import ParseError, Term, desugar, parse
from .modules import list_modules

Execute = Callable[[Dict[str, Any]], Dict[str, Any]]


class CheckError(ValueError):
    pass


def alphabet(
    modules: List[Dict[str, Any]] | None = None,
    schedules: List[Dict[str, Any]] | None = None,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in list_modules() if modules is None else modules:
        name = row["name"]
        if name in out:
            raise CheckError(f"duplicate catalog name '{name}'")
        out[name] = row
    for row in list_schedules() if schedules is None else schedules:
        name = row["name"]
        if name in out:
            raise CheckError(f"duplicate catalog name '{name}'")
        out[name] = row
    return out


def check(term: Term, catalog: Dict[str, Dict[str, Any]]) -> None:
    t = desugar(term)
    missing = [n for n in t.atoms() if n not in catalog]
    if missing:
        raise CheckError(f"unknown verbs (not in catalog): {', '.join(missing)}")


@dataclass
class ReduceResult:
    status: str
    jobs: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    residual: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "jobs": self.jobs,
            "records": self.records,
            "residual": self.residual,
        }


def specialize(term: Term) -> Term:
    """Mix: atoms/combinators are static; dyn stays a hole (Futamura.lean specialize)."""
    t = desugar(term)
    return _spec(t)


def _spec(term: Term) -> Term:
    if term.op == "dyn":
        return term
    if term.op == "atom":
        return term
    if term.op == "norm":
        return term
    return Term(
        term.op,
        name=term.name,
        f=_spec(term.f) if term.f else None,
        g=_spec(term.g) if term.g else None,
        x=_spec(term.x) if term.x else None,
        steps=tuple(_spec(s) for s in term.steps),
    )


def reduce_term(
    term: Term,
    catalog: Dict[str, Dict[str, Any]] | None = None,
    *,
    execute: Execute = execute_job,
    mix: bool = True,
) -> ReduceResult:
    cat = catalog if catalog is not None else alphabet()
    check(term, cat)
    body = specialize(term) if mix else desugar(term)
    jobs: list[str] = []
    records: list[dict[str, Any]] = []

    def run_atom(name: str) -> dict[str, Any]:
        rec = execute(cat[name])
        jobs.append(name)
        records.append(rec)
        return rec

    def ev(t: Term) -> str:
        if t.op == "dyn" or t.op == "norm":
            return "SUCCESS"
        if t.op == "atom":
            rec = run_atom(t.name)
            return rec.get("status", "ERROR")
        if t.op == "konst" and t.x is not None:
            return ev(t.x)
        if t.op == "app" and t.f is not None and t.x is not None:
            f, x = t.f, t.x
            if f.op == "norm":
                return ev(x)
            if f.op == "konst" and f.x is not None:
                return ev(f.x)
            if f.op == "app" and f.f is not None and f.x is not None:
                inner = f.f
                if inner.op == "konst" and inner.x is not None:
                    # (konst · a) · y → a
                    return ev(inner.x)
                if (
                    inner.op == "app"
                    and inner.f is not None
                    and inner.f.op == "comp"
                    and inner.x is not None
                    and f.x is not None
                ):
                    # ((comp · f) · g) · x → f · (g · x)
                    ff, gg = inner.x, f.x
                    gx = Term("app", f=gg, x=x)
                    return ev(Term("app", f=ff, x=gx))
                if inner.op == "comp" and f.x is not None:
                    # (comp · f) · g  still needs x — wait for outer app
                    pass
            if f.op == "comp" and f.f is not None and f.g is not None:
                # (comp f g) · x → f · (g · x)
                gx = Term("app", f=f.g, x=x)
                return ev(Term("app", f=f.f, x=gx))
            if f.op == "atom":
                st = ev(x)
                if st != "SUCCESS":
                    return st
                return ev(f)
            st = ev(x)
            if st != "SUCCESS":
                return st
            return ev(f)
        if t.op == "comp" and t.f is not None and t.g is not None:
            return "SUCCESS"
        return "ERROR"

    status = ev(body)
    if any(r.get("status") != "SUCCESS" for r in records):
        status = "FAILED"
    return ReduceResult(
        status=status,
        jobs=jobs,
        records=records,
        residual=body.to_json(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check / mix / reduce a job term against the catalog alphabet"
    )
    parser.add_argument("term", nargs="?", help="JSON file or inline JSON term")
    parser.add_argument("--check", action="store_true", help="Well-formedness only")
    parser.add_argument("--reduce", action="store_true", help="Check, mix, execute atoms")
    parser.add_argument("--alphabet", action="store_true", help="Print catalog names")
    args = parser.parse_args(argv)

    cat = alphabet()
    if args.alphabet:
        for name in sorted(cat):
            print(name)
        return 0

    if not args.term:
        parser.print_help()
        return 1

    raw_path = Path(args.term)
    if raw_path.is_file():
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    else:
        data = json.loads(args.term)

    try:
        term = parse(data)
        check(term, cat)
    except (ParseError, CheckError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.check and not args.reduce:
        print(json.dumps({"ok": True, "atoms": desugar(term).atoms()}, indent=2))
        return 0

    result = reduce_term(term, cat)
    print(json.dumps(result.to_json(), indent=2, default=str))
    return 0 if result.status == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
