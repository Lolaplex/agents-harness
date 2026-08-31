"""Job terms as an ISAR view (TRSView-style).

Scaffold is `isar-proofs/src/ISAR/Kernel.lean` ITerm / IStep and
`Futamura.lean` specialize / mix equation. This dialect adds `atom`
(catalog verb) and `dyn` (this turn). Atoms reduce via Cordis, which is
IO — not an IStep. Combinators around them follow the Lean rules:

  norm · x           → x
  (konst · x) · y    → x
  ((comp · f) · g) · x → f · (g · x)

`seq` is surface sugar: chronological verbs, first step first. Desugars to
comp-spine applied to dyn. Unknown atoms fail parse (closed carrier).
Skill bodies and NL prompts are not terms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Op = Literal["atom", "dyn", "norm", "konst", "comp", "app", "seq"]


@dataclass(frozen=True)
class Term:
    op: Op
    name: str = ""
    f: Term | None = None
    g: Term | None = None
    x: Term | None = None
    steps: tuple[Term, ...] = ()

    def atoms(self) -> list[str]:
        if self.op == "atom":
            return [self.name]
        names: list[str] = []
        for child in (self.f, self.g, self.x, *self.steps):
            if child is not None:
                names.extend(child.atoms())
        return names

    def to_json(self) -> dict[str, Any]:
        row: dict[str, Any] = {"op": self.op}
        if self.name:
            row["name"] = self.name
        if self.f is not None:
            row["f"] = self.f.to_json()
        if self.g is not None:
            row["g"] = self.g.to_json()
        if self.x is not None:
            row["x"] = self.x.to_json()
        if self.steps:
            row["steps"] = [s.to_json() for s in self.steps]
        return row


def atom(name: str) -> Term:
    return Term("atom", name=name.strip())


def dyn(name: str = "turn") -> Term:
    return Term("dyn", name=name)


def seq(*steps: Term) -> Term:
    return Term("seq", steps=tuple(steps))


class ParseError(ValueError):
    pass


def parse(data: Any) -> Term:
    if isinstance(data, str):
        return atom(data)
    if not isinstance(data, dict) or "op" not in data:
        raise ParseError("term must be a string name or {op: ...}")
    op = str(data["op"])
    if op == "atom":
        name = str(data.get("name") or "")
        if not name:
            raise ParseError("atom needs name")
        return atom(name)
    if op == "dyn":
        return dyn(str(data.get("name") or "turn"))
    if op == "norm":
        return Term("norm")
    if op == "konst":
        inner = data.get("x") or data.get("keep")
        if inner is None:
            raise ParseError("konst needs x")
        return Term("konst", x=parse(inner))
    if op == "comp":
        if "f" not in data or "g" not in data:
            raise ParseError("comp needs f and g")
        return Term("comp", f=parse(data["f"]), g=parse(data["g"]))
    if op == "app":
        if "f" not in data or "x" not in data:
            raise ParseError("app needs f and x")
        return Term("app", f=parse(data["f"]), x=parse(data["x"]))
    if op == "seq":
        steps = data.get("steps") or []
        if not isinstance(steps, list) or not steps:
            raise ParseError("seq needs a non-empty steps list")
        return seq(*(parse(s) for s in steps))
    raise ParseError(f"unknown op '{op}'")


def desugar(term: Term) -> Term:
    """seq[g, f] → ((comp · f) · g) · dyn  so g runs first (compβ)."""
    if term.op == "seq":
        steps = [desugar(s) for s in term.steps]
        acc = steps[0]
        for nxt in steps[1:]:
            acc = Term("comp", f=nxt, g=acc)
        return Term("app", f=acc, x=dyn())
    if term.f or term.g or term.x:
        return Term(
            term.op,
            name=term.name,
            f=desugar(term.f) if term.f else None,
            g=desugar(term.g) if term.g else None,
            x=desugar(term.x) if term.x else None,
        )
    return term
