# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Dynamic `schedule.add` no longer defaults to Telegram. Text without a verb requires `--channel` and `--user`; naive ISO datetimes use an optional IANA `--timezone`.

### Fixed
- Loop clock timezone prefers the identity user zone over the global USER.md profile. `mcp.schedule.add` inherits the current turn `--user` and timezone when the model omits them.
- Loop still assembles when `agents-traces` is not installed (CI / thin hosts). Memory-search loop tests skip when `agents-memory` is missing.

## [0.0.1] - 2026-09-05

### Added
- Initial setup and alignment with Autonomous GitHub Standard.
- Agent runtime engine & scheduled task runner.