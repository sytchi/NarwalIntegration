# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-07-30

> ⚠️ Upgrading from 1.x? See the [2.0.0](#200---2026-07-25) breaking changes.

### Changed
- Map render: the overlay frame now starts from a copy of the accumulated
  vacuumed strip instead of allocating a canvas and compositing the strip onto it.
- Map render: the zone branch uses the module-level `Image.alpha_composite`
  instead of the in-place method, which Pillow implements as crop + composite +
  paste, three passes over a 13.9 MB buffer.

Rendered output is byte-identical (verified pixel-for-pixel on real map data).
About 44 ms less per rendered frame on a Home Assistant host, roughly -8%.

## [2.1.2] - 2026-07-29

> ⚠️ Upgrading from 1.x? See the [2.0.0](#200---2026-07-25) breaking changes.

### Changed
- Broadcast decoding no longer uses `blackboxprotobuf`. A purpose-built wire-format
  decoder (`narwal_client/protobuf_decoder.py`) is 7.1x faster and bit-for-bit
  compatible; payloads it cannot reproduce fall back to the library.
- Duplicate broadcasts are skipped before decoding. While docked, ~84% of frames
  are byte-identical repeats of the previous one.
- The WebSocket listener yields between frames, so several buffered frames no
  longer land in a single blocking event-loop step.

Event-loop time blocked by the listener, measured on a live Home Assistant host
(x86_64, CPython 3.14): **11.44 ms/s to 0.11 ms/s while docked**, **7.0 ms/s
while cleaning** (0.7% of wall time).

## [2.1.1] - 2026-07-27

> ⚠️ Upgrading from 1.x? See the [2.0.0](#200---2026-07-25) breaking changes.

### Changed
- Rejected start commands now raise an error instead of only logging one, so a
  map-card tap or an automation step no longer looks successful while the robot
  stays put. Applies to `vacuum.start`, `narwal.clean_rooms` and
  `narwal.clean_zone`; `narwal.resume` stays log-only (it is sent blind).
- Rejection reasons are now spelled out. New result code **4 (`NOT_READY`)**:
  the robot declines to start until it has charged — seen after a long clean
  (rejected at 23-26% battery, accepted at 30%; mop drying does not block a
  start). The message includes the current battery level.

### Added
- Map gallery screenshots: a full-session HD map and a live zone clean with the
  amber active-zone overlay.

## [2.1.0] - 2026-07-26

> ⚠️ Upgrading from 1.x? See the [2.0.0](#200---2026-07-25) breaking changes.

### Added
- `rooms` attribute on `camera.*_map_hd` — per-room outlines (world coords),
  names and label anchors.
- One-click Rooms mode via `xiaomi-vacuum-map-card`'s "Generate rooms config"
  button (reads that `rooms` attribute; replaces the ROOM mode's
  `predefined_selections`).
- README images use absolute URLs (render on the HACS info page).

## [2.0.0] - 2026-07-25

> ### ⚠️ BREAKING CHANGES — the map changed
>
> If you use a **map card** or **zone automations**, act before upgrading.

### Removed
- **The legacy 1:1 `camera.*_map` entity is gone.** Only the high-resolution
  `camera.*_map_hd` remains. Any dashboard card, picture entity or automation
  referencing `camera.*_map` **will break** — repoint it to
  `camera.*_map_hd`.
- **The map-image pixel coordinate contract of `narwal.clean_zone` is gone.**
  Zones are now **always robot world coordinates** — exactly what
  `xiaomi-vacuum-map-card` sends with `calibration_source: {camera: true}`.
  The `coordinates` service field is still accepted (so pre-2.0 automations
  keep validating) **but is ignored**: a call that used to pass `pixels`
  is now interpreted as world coordinates and would clean the wrong area.
  Drop `coordinates: pixels` from your `clean_zone` calls.

### Migration
- Point your map card at `camera.*_map_hd` with
  `calibration_source: {camera: true}` (see the README's map-card example).
  The card then produces world coordinates automatically and needs no
  re-calibration when you change `map_scale`.
- The deprecation was announced in 1.6.0. Not ready to migrate? Pin the
  integration to **v1.6.0** in HACS.

## [1.6.0] - 2026-07-25

### Added
- **High-resolution map camera** `camera.*_map_hd`: the map grid is upscaled
  (NEAREST, crisp room edges) with anti-aliased vector overlays (supersampled
  RGBA layer downsampled with BOX) — rendering technique adapted from Piotr
  Machowski's Xiaomi Cloud Map Extractor (MIT). The legacy 1:1 `camera.*_map`
  remains for identity-calibration setups.
- **`calibration_points` attribute** on the HD camera — three vacuum↔image
  point pairs computed live from the active map, ready for
  `xiaomi-vacuum-map-card` with `calibration_source: {camera: true}` (stays
  correct across map scale changes and remaps).
- **Options flow**: `map_scale` (2–6, default 4) controls the HD camera's
  upscale factor.
- **Robot-recorded trail**: the trail line follows the midline of the
  display_map "rails" (field 12 — the robot's own path record), with the
  freshest tail reconstructed from live positions and replaced as new rail
  data arrives.
- **Vacuumed strip**: the freshly vacuumed 11.4 cm track exactly as the robot
  reports it (the strip between the field-12 rail pair), drawn bright and
  semi-transparent.
- **Planned trajectory**: `status/point_navi_plan_traj` (previously
  subscribed but dropped) is decoded and drawn as a thin line showing where
  the robot is about to go; docking anomaly frames are filtered.
- **Lidar cell marks**: incremental wall/obstacle observations (display_map
  field 7) accumulate per saved map. Cells the robot flags as **carpet**
  (field-7 low-byte bit 2) render as a light tint just above the floor
  colour (matching the Narwal app's carpet shading); cells adjacent to a
  saved-map wall take the wall shade; free-standing detections render
  minimally darker than the floor. Every cell is inset ~0.5 px so the grid
  reads cleanly rather than as a solid blob.
- **Carpet persistence**: carpet cells survive across cleaning sessions
  (they describe the home, not one run), while the noisier non-carpet lidar,
  trail and vacuumed-strip buffers are cleared when a new session starts.
- **Furniture rendering**: rectangles honor the robot's rotation angle
  (previously ignored), get a translucent interior fill, and their labels use
  a smaller font placed below the shape with collision avoidance — room names
  always win.
- **Map layer switches**: `switch.*_draw_trail`, `*_draw_cleaned_area`,
  `*_draw_furniture`, `*_draw_lidar_walls` (config category, state restored
  across restarts) toggle each overlay; the furniture switch also rebuilds
  the cached base map.
- **Robot-position fallback**: when display_map drops out mid-clean (30 s+
  gaps on fw v01.08.03+), the robot marker and trail keep moving using the
  planned-trajectory head — whichever broadcast is freshest wins.
- Bundled DejaVuSans font (the HA container ships no TrueType fonts; the
  sized Pillow fallback renders e.g. "ó" as tofu).

### Performance
- **Event-loop stalls eliminated.** The map camera used to redraw every
  accumulated swath quad and lidar cell on every frame — an O(total) cost
  that grew through a clean and, holding the GIL, stalled Home Assistant's
  event loop up to ~3.9 s on a large map. Swath and lidar are now painted
  once onto persistent RGBA layers and only the *new* cells are drawn each
  frame (O(new)), so per-frame cost stays flat for the whole session.
  Measured under a full live clean: CPU flat at 4–19 %, render latency
  ≤360 ms, zero multi-second stalls (see the map-performance page in the
  docs).
- **Adaptive render cadence**: the camera renders on demand and throttles
  redundant frames instead of on a fixed fast timer.
- A batch of low-risk render/loop optimizations (cached base-map reuse,
  cheaper coordinate transforms, fewer per-frame allocations).

### Changed
- **`narwal.clean_zone` gained an optional `coordinates` parameter**
  selecting the coordinate contract: `pixels` (the DEFAULT — the pre-1.6
  map-image-pixel behavior, so **existing configs keep working unchanged**),
  `world` (the new, experimental mode: robot world/map-frame values, exactly
  what the map card sends with camera calibration on the HD camera), or
  `auto` (range-based detection; unreliable on maps whose origin is close
  to (0, 0)). Safety net: a call containing any negative value (impossible
  as pixels) is treated as world.
- Camera `extra_state_attributes` still exposes `render_count`; the HD
  camera adds `calibration_points`.

### Deprecated
- The map-image **pixel contract of `narwal.clean_zone`** and the legacy 1:1
  `camera.*_map` entity. **2.0.0** will remove both and default to world
  coordinates; the `coordinates` parameter will still be accepted (and
  ignored) so existing service calls won't error. Migrate map cards to the
  HD camera with `calibration_source: {camera: true}`.

### Fixed
- Field-7 blobs with a corrupted adler32 checksum are decoded via raw
  deflate instead of being dropped.
- **Zone cleaning overlay no longer hides progress**: the active-zone amber
  fill was painted *on top* of the vacuumed strip and lidar cells, so during
  a zone clean the cleaned area was invisible. Zones are now drawn underneath
  those overlays.

## [1.5.1] - 2026-07-22

### Fixed
- **Manifest requirements** no longer force a pip install/downgrade on modern
  Home Assistant. `websockets` and `protobuf` are provided by HA core; the
  1.5.0 upper bounds (`websockets<14`, `protobuf<6`) excluded the versions HA
  ships (websockets 15.x, protobuf 6.x), which on Python 3.14 could fail to
  reinstall for lack of a matching wheel. Requirements are now
  `websockets>=12.0`, `bbpb>=1.4.0`, `Pillow>=9.0.0` (satisfied by HA's
  bundled versions; `protobuf` is pulled transitively by `bbpb`).

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

[2.1.0]: https://github.com/sytchi/NarwalIntegration/releases/tag/v2.1.0
[2.0.0]: https://github.com/sytchi/NarwalIntegration/releases/tag/v2.0.0
[1.6.0]: https://github.com/sytchi/NarwalIntegration/releases/tag/v1.6.0
[1.5.1]: https://github.com/sytchi/NarwalIntegration/releases/tag/v1.5.1
[1.5.0]: https://github.com/sytchi/NarwalIntegration/releases/tag/v1.5.0
[1.4.3]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.4.2]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.4.1]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.4.0]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.3.0]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.2.0]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.1.0]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.0.1]: https://github.com/sytchi/NarwalIntegration/blob/master/CHANGELOG.md
[1.0.0]: https://github.com/sjmotew/NarwalIntegration
