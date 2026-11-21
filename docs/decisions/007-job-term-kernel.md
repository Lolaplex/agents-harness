# 007 Job terms as an ISAR view

The verified scaffold is Kernel.lean ITerm/IStep and Futamura.lean specialize. Harness does not run Lean at request time. Catalog JSON (modules + schedules) is the alphabet. A job term is a TRSView-style dialect: atom, dyn, norm, konst, comp, app, plus seq sugar.

Check rejects unknown names (closed carrier). Reduce: combinators follow IStep; atom β is Cordis execute_job (IO, not in Lean). Mix peels dyn as the hole. Skill markdown is not a term.

Example: `runner/terms/skill-then-a2a.json`. CLI: `python -m runner.kernel --reduce FILE`.

- Harness job kernel is an ISAR Kernel.lean view, not a new language. Catalog JSON is the alphabet. python -m runner.kernel checks/mixes/reduces. Combinators follow IStep; atom β is Cordis execute_job. seq sugar in runner/terms/. Skill markdown is not a term. Lean is not a runtime dependency.
