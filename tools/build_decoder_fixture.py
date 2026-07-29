#!/usr/bin/env python3
"""Rebuild the golden fixture for tests/test_protobuf_decoder.py.

Input is a capture of raw broadcast payloads (JSONL, one
``{"topic": ..., "payload_b64": ...}`` per line) taken off a live robot -- see
`pyscript/narwal_traj_capture.py` in the Home Assistant config for the hook
that produces one.

Duplicate payloads are dropped (idle topics repeat the same bytes for minutes
on end) and each surviving frame is stored together with a digest of what
blackboxprotobuf decodes it to.  That digest is the contract: the decoder in
`narwal_client/protobuf_decoder.py` has to reproduce it byte for byte.

    python tools/build_decoder_fixture.py capture.jsonl [more.jsonl ...]

BEFORE COMMITTING A REGENERATED FIXTURE
---------------------------------------
Broadcasts carry robot identifiers.  ``status/robot_base_status`` field 13 is a
32-character hex device id, and this repository is public.  ``SCRUB`` below
replaces the known ones; check for new ones with::

    python - <<'EOF'
    import base64, gzip, json, re
    from narwal_client.protobuf_decoder import decode
    def walk(v, p, out):
        if isinstance(v, dict): [walk(x, f"{p}.{k}", out) for k, x in v.items()]
        elif isinstance(v, list): [walk(x, p, out) for x in v]
        elif isinstance(v, str): out.add((p, v))
        elif isinstance(v, bytes) and re.fullmatch(rb"[ -~]{4,}", v): out.add((p, v))
    out = set()
    for line in gzip.open("tests/fixtures/narwal_broadcast_frames.jsonl.gz", "rt"):
        r = json.loads(line)
        walk(decode(base64.b64decode(r["payload_b64"])), r["topic"], out)
    print(*sorted(out), sep="\\n")
    EOF

A replacement must keep the original byte length (so the wire layout is
untouched) and must decode to the same *kind* of value -- a placeholder that
happens to parse as a nested message would quietly drop the string branch of
the decoder from the fixture's coverage.
"""

from __future__ import annotations

import base64
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.decoder_digest import digest  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "narwal_broadcast_frames.jsonl.gz"
)

# Robot identifiers that must never reach the public repo, and their equal-length
# replacements.  See the module docstring for how to spot new ones.
SCRUB: dict[bytes, bytes] = {
    b"9a17559e87714ab2a2991d80977cba3d": b"REDACTED-DEVICE-ID--------------",
}


def main(paths: list[str]) -> int:
    import blackboxprotobuf

    seen: set[bytes] = set()
    records: list[dict[str, str]] = []
    for path in paths:
        with open(path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                entry = json.loads(line)
                payload = base64.b64decode(entry["payload_b64"])
                for secret, placeholder in SCRUB.items():
                    if secret in payload:
                        assert len(secret) == len(placeholder), "length must match"
                        payload = payload.replace(secret, placeholder)
                if payload in seen:
                    continue
                seen.add(payload)
                decoded, _ = blackboxprotobuf.decode_message(payload)
                records.append(
                    {
                        "topic": entry["topic"],
                        "payload_b64": base64.b64encode(payload).decode(),
                        "digest": digest(decoded),
                    }
                )

    records.sort(key=lambda r: (r["topic"], r["payload_b64"]))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(FIXTURE, "wt", encoding="utf-8", compresslevel=9) as out:
        for record in records:
            out.write(json.dumps(record, sort_keys=True) + "\n")

    print(f"{len(records)} unique frames -> {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
