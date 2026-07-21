# Contributing

Thanks for your interest in improving the Narwal integration! Bug reports,
testing reports on different models/firmwares, translations and code are all
welcome.

## Development setup

```bash
git clone https://github.com/sytchi/NarwalIntegration.git
cd NarwalIntegration
python3.13 -m venv .venv          # use Python 3.13 — 3.14 currently breaks the test stubs
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/
```

All tests must pass before a PR is merged; the `validate.yml` workflow also
runs [hacs/action](https://github.com/hacs/action) and
[hassfest](https://github.com/home-assistant/actions#hassfest), and `lint.yml`
runs `ruff check`.

## Repository layout — the one rule that will bite you

The protocol client lives **twice** in the tree:

- `narwal_client/` (repository root) — the canonical, importable copy used by
  tests and the dev tools;
- `custom_components/narwal/narwal_client/` — the embedded copy Home Assistant
  actually loads.

They must stay **byte-for-byte identical** — `tests/test_integration_structure.py`
fails otherwise. When you change one, copy the change to the other (e.g.
`cp narwal_client/*.py custom_components/narwal/narwal_client/`).

## Reverse-engineering tools

`tools/` contains the helpers used to decode the local protocol:

- `capture_pmd3.sh` — capture phone ↔ robot traffic from an iPhone via
  `pymobiledevice3` (the local WebSocket on port 9002 is plaintext);
- `decode_ws_pcap.py` — extract and pretty-print protobuf WebSocket frames
  from a pcap/pcapng;
- `probe_shortcuts.py`, `replay_zone.py`, `verify_zone_full.py` — send
  hand-crafted payloads to a robot for validation.

Copy `tools/config.example.py` to `tools/config.py` (gitignored) and set your
robot's IP there. **Never commit real IPs, captures or map dumps** — captures
of your own home contain your floor plan.

Keep in mind the robot accepts **one WebSocket client at a time**: while Home
Assistant is connected, a second script gets no broadcasts and no replies.
Stop the integration (or use HA itself) when probing.

## Pull requests

- Branch from `master`, keep PRs focused on one change.
- Follow the existing code style (`ruff check` must pass); type hints where
  practical.
- Add or update tests for behavior changes — protocol decoding changes should
  come with a captured-payload test case (sanitized: no real session ids, IPs
  or map data).
- Commit messages: conventional style (`feat:`, `fix:`, `chore:`, …),
  English.
- Hardware validation notes are gold: state the model and firmware version you
  tested on in the PR description.

## Versioning and releases

- Semantic versioning; the version lives in
  `custom_components/narwal/manifest.json`.
- Every release gets a `CHANGELOG.md` entry, a `vX.Y.Z` tag and a GitHub
  release (HACS installs releases, not bare tags).

## Reporting issues

Use the issue templates — model, firmware version, HA version and debug logs
make diagnosis much faster. Debug logging:

```yaml
logger:
  logs:
    custom_components.narwal: debug
```
