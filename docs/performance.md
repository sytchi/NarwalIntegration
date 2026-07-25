# Map rendering performance

The live map draws several **accumulating** layers — the lidar wall/obstacle
cells (`display_map` field 7) and the vacuumed strip (field 12 rails). These
only grow during a cleaning session (bounded at 60 000 cells / 20 000 strip
quads) and reset when a new session starts.

## The problem: O(total) per frame

The original renderer redrew the **entire accumulated set every frame** — up to
tens of thousands of `ImageDraw` calls on a supersampled canvas, for each map
camera, roughly every 1.5–2 s while cleaning.

Rendering runs in an executor thread, but Pillow's Python-level draw loops hold
the GIL, so a render that grows with the session progressively starves the Home
Assistant event loop. On a constrained HAOS host this showed up as **HA-wide
latency that got worse the longer the robot cleaned** and eased at the
sweep→mop transition (when the accumulators reset).

## The fix: incremental layers (O(new) per frame)

Each map camera keeps the accumulated lidar/strip layers as **persistent RGBA
images** and draws only the **new** cells/quads onto them each frame. Per-frame
cost becomes proportional to what changed (~a dozen cells), independent of how
long the robot has been cleaning. Output is **byte-for-byte identical** to the
full redraw (verified with pixel diffs for lidar-only, strip-only, both layers,
and layer-off cases).

## Stress-test results

### Micro-benchmark — render cost vs. accumulated size

Single map frame, `scale=4`, `supersample=2`, 200×271 grid, on a development
laptop. Absolute numbers are hardware-dependent; the **shape** is the point —
the old path grows linearly with accumulation, the new path stays flat.

| Accumulated lidar cells | Old (full redraw) | New (incremental) |
| ----------------------: | ----------------: | ----------------: |
|                   2 000 |             36 ms |             40 ms |
|                   8 000 |             59 ms |             52 ms |
|                  20 000 |             93 ms |             61 ms |
|                  40 000 |        **135 ms** |         **56 ms** |

The new cost is dominated by the fixed supersample+downsample step, not by the
accumulated set — so it does not keep climbing as a clean progresses.

### Live soak test — event-loop latency during a real clean

Measured on a low-power mini-PC HAOS host as the round-trip time of a trivial
`GET /api/` request (50 samples), which reflects event-loop responsiveness.

| Phase                                   |  Median |          Worst-case | Multi-second stalls |
| --------------------------------------- | ------: | ------------------: | ------------------: |
| **Before** — active clean, high accum.  | ~14 ms  | **3.9 s** (p99 2.1 s) | ~29 % of wall-clock |
| **After** — full clean, active rendering| 14–15 ms |         130–222 ms |                None |

Before the fix the event loop was frozen for up to ~4 seconds at a time as the
map accumulated. After the fix, latency stayed flat for the whole clean while
the map kept rendering live.

## Further tuning

- **Layer switches** — each accumulating layer (trail, cleaned area, furniture,
  lidar walls) has a switch entity; turning off the heaviest layers reduces
  render work further, though the incremental renderer already makes this
  unnecessary for responsiveness.
- **One map camera** — if you only use the HD camera, disabling the legacy 1:1
  `camera.*_map` entity halves the per-frame render work during cleaning.
- **Render interval** — the minimum re-render interval is ~2 s; the map does not
  re-render more often than that regardless of broadcast rate.

## Methodology & reproducibility

- Micro-benchmark: synthetic accumulated sets fed through the same renderer used
  in production; timings are the min of several runs.
- Live latency: `GET /api/` round-trip sampled at 5 Hz during an actual cleaning
  session, before and after deploying the fix on the same host.

> **Planned:** a follow-up round will also record **host CPU load with the robot
> cleaning vs. idle**, to quantify the render load directly rather than only via
> event-loop latency.
