# AGENTS.md — agents-harness Developer Guidelines

## Architecture Principles
- **Zero Heavy Dependencies**: Pure Python standard library (`subprocess`, `json`, `datetime`, `pathlib`, `logging`). No Celery, no Redis, no cron daemons.
- **Durable Manifests (Koru Principle)**: Every scheduled flow is declared in `runner/schedules/*.json` with the verified verb it rests on, its cadence, and its expected exit semantics (`exit 0 = healthy`).
- **Strict Layering (Cordis Principle)**: The executor owns no state, no domain store, and no semantics. It never imports module internals; it strictly invokes verified CLIs and inspects exit codes.
- **Plexus Convergence Path**: Manifests are structured to migrate 1:1 into `plexus` native scheduled flow engine and Tamagotchi care-schedule format once plexus scheduler goes live.

## Commands
- Validate all manifests: `python -m runner.executor --validate`
- Run all scheduled flows once: `python -m runner.executor --all`
- Run specific flow: `python -m runner.executor --run <name>`
- Run test suite: `python -m unittest discover tests`
