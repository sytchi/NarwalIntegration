# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] - 2026-07-21

First public release of this fork.

### Changed
- Repository is now standalone under `sytchi/NarwalIntegration`
  (`codeowners`, `documentation`, `issue_tracker` updated).
- Rewritten README, new CONTRIBUTING, CODE_OF_CONDUCT and this changelog.
- CI: added ruff lint, release drafter, Dependabot and secret scanning.

### Removed
- Internal development-planning documents (`.planning/`).

## [1.4.3] - 2026-07-20

### Fixed
- Manual **pause** is now detected on firmware v01.08.03+: active cleaning
  nondeterministically reports `DOCKED_V2` or `CLEANING_ALT` working status;
  the pause flag was ignored for the `DOCKED_V2` shape, so the entity kept
  showing `cleaning` and play started a new job instead of resuming. Pause
  handling is now gated on a new `is_cleaning_session` property covering
  both shapes.

## [1.4.2] - 2026-07-19

### Fixed
- The active-zone overlay on the map camera is now cleared when a new
  whole-house or room cleaning starts (`start_clean_whole` /
  `start_clean_rooms` did not reset `active_zones`).

## [1.4.1] - 2026-07-16

### Fixed
- Robot no longer reported as **docked** while cleaning on firmware
  v01.08.03+ (point-navi tasks broadcast `DOCKED_V2` while driving).
  Dock-like working status is vetoed when both dock fields explicitly
  report off-dock; the broadcast resubscription gate covers this shape,
  fixing status broadcasts going silent mid-clean.

## [1.4.0] - 2026-07-16

### Added
- **Readable error states**: 48 known Narwal fault codes translated to
  English, French and Polish. The error sensor exposes `code`, `code_hex`,
  `help_url`, `message` and `severity` attributes; unknown codes fall back
  to the raw number.

## [1.3.0] - 2026-07-16

### Added
- **`narwal.resume` service**: unconditional `task/resume` that wakes the
  robot first and never starts a new job. Recovers false "robot lifted"
  pauses (e.g. on a doormat) even when the entity state lags behind.

## [1.2.0] - 2026-07-10

### Added
- **Active-zone overlay**: rectangles sent via `narwal.clean_zone` are drawn
  on the map camera as translucent amber outlines and cleared on
  stop / cancel / return to base.

## [1.1.0] - 2026-07-10

### Added
- **Clean-mode select**: sweep, mop, sweep + mop, and sequential
  **sweep then mop**.
- **`narwal.clean_zone` service**: clean arbitrary rectangles drawn on a map
  card (map-image pixel coordinates; the required coverage path is generated
  client-side).
- **`narwal.clean_rooms` service**: clean rooms by map segment id (works
  around Home Assistant's `area_mapping_not_configured` for
  `vacuum.clean_area`).
- Whole-house cleaning honors the selected clean mode.

### Changed
- All cleaning commands go through `clean/start_clean`, the only command
  path recent firmwares (v01.08.03+) actually honor parameters on.
- Development tools read the robot address from `tools/config.py`
  (gitignored).

### Fixed
- Parse `map_id` from `get_map` (fixes a `vacuum.start` crash).

## [1.0.1] - 2026-07-08

### Added
- **Station buttons**: wash mop, dry mop, empty dustbin, wake robot.
- **Mop humidity select**: dry / normal / wet.
- **Sensors**: dust bag health, cleaning progress (while cleaning),
  station activity (idle / mop washing / mop drying / dust emptying),
  error code.

## [1.0.0] - 2026-05-30

Upstream baseline: [sjmotew/NarwalIntegration](https://github.com/sjmotew/NarwalIntegration)
— local WebSocket vacuum control, sensors, live map camera with room labels,
room cleaning, config flow.

[1.5.0]: https://github.com/sytchi/NarwalIntegration/releases/tag/v1.5.0
