"""Probe Narwal robot for shortcut-related WebSocket topics.

Discovers whether robot shortcuts (Narwal app "Shutcut" presets) are
retrievable via local WebSocket or are cloud-only. Tries known candidates
from APK analysis and listens for broadcast topics containing "shortcut".

Usage:
    py tools\\probe_shortcuts.py           # probe all topics + listen 30s
    py tools\\probe_shortcuts.py --quick   # probe topics only, skip broadcast listen
"""

import asyncio
import json
import re
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from narwal_client.client import NarwalClient
from narwal_client.protocol import (
    PROTOBUF_FIELD5_TAG,
    ProtocolError,
    build_frame,
    parse_frame,
)

# ── Config ──────────────────────────────────────────────────────────────
HOST = "192.168.x.x"
PORT = 9002
RESPONSE_TIMEOUT = 10.0   # seconds to wait for response per command
INTER_CMD_DELAY = 2.0     # seconds between commands
BROADCAST_LISTEN_SECS = 30.0  # seconds to listen for shortcut-related broadcasts

# ANSI
G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; C = "\033[36m"
M = "\033[35m"; D = "\033[2m"; B = "\033[1m"; X = "\033[0m"

STATE_NAMES = {0: "UNKNOWN", 1: "STANDBY", 4: "CLEANING", 5: "CLEANING_ALT", 10: "DOCKED", 14: "CHARGED"}

# ── Probe topic candidates ───────────────────────────────────────────────
# (label, short_topic, hex_payload, description)
# Ordered: most-likely first (config/get and feature_list), then speculative
PROBE_TOPICS = [
    ("get_device_info",    "common/get_device_info",       "", "Baseline — confirms connection works"),
    ("config_get",         "config/get",                   "", "May contain robot_shortcuts JSON field (PRIMARY CANDIDATE)"),
    ("get_feature_list",   "common/get_feature_list",      "", "Check for shortcut feature flag"),
    ("shortcut_get",       "shortcut/get",                 "", "Speculative: dedicated shortcut topic"),
    ("clean_shortcut_get", "clean/shortcut/get",           "", "Speculative: clean-prefixed shortcut"),
    ("robot_shortcut_get", "robot/shortcut/get",           "", "Speculative: robot-prefixed shortcut"),
    ("get_cur_plan",       "clean/cur_plan/get",           "", "Current clean plan — may reference shortcut"),
    ("get_plans",          "clean/plan/get",               "", "All cleaning plans — may contain shortcut params"),
]

# ── Logging ─────────────────────────────────────────────────────────────
log_lines: list[str] = []
all_responses: dict[str, dict] = {}
broadcast_topics_seen: list[dict] = []

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg: str):
    stamped = f"[{ts()}] {msg}"
    log_lines.append(stamped)
    print(stamped)

def log_raw(msg: str):
    log_lines.append(msg)
    print(msg)


# ── Protobuf decode ──────────────────────────────────────────────────────
def decode_protobuf(payload: bytes) -> dict:
    try:
        import blackboxprotobuf
        decoded, _ = blackboxprotobuf.decode_message(payload)
        return decoded
    except Exception as e:
        return {"_error": str(e), "_hex": payload.hex()}


def fmt_value(val, depth=0, max_depth=5) -> str:
    """Pretty-print protobuf values with full depth."""
    if depth > max_depth:
        return "..."

    if isinstance(val, bytes):
        try:
            t = val.decode("utf-8")
            if t.isprintable() and len(t) > 0:
                return f'"{t.strip()}"'
        except UnicodeDecodeError:
            pass
        if len(val) == 4:
            f32 = struct.unpack('<f', val)[0]
            if -10000 < f32 < 10000 and f32 != 0:
                return f"0x{val.hex()} (float32={f32:.4f})"
        if len(val) <= 32:
            return f"0x{val.hex()} ({len(val)}B)"
        return f"({len(val)}B blob)"

    if isinstance(val, dict):
        indent = "  " * (depth + 1)
        parts = []
        for k, v in val.items():
            parts.append(f"{indent}{k}: {fmt_value(v, depth + 1, max_depth)}")
        return "{\n" + "\n".join(parts) + "\n" + "  " * depth + "}"

    if isinstance(val, list):
        if len(val) == 0:
            return "[]"
        if len(val) <= 5 and all(not isinstance(v, (dict, list)) for v in val):
            return "[" + ", ".join(fmt_value(v, depth + 1, max_depth) for v in val) + "]"
        indent = "  " * (depth + 1)
        parts = []
        for i, v in enumerate(val):
            parts.append(f"{indent}[{i}]: {fmt_value(v, depth + 1, max_depth)}")
        return f"[{len(val)} items]\n" + "\n".join(parts)

    return str(val)


def make_serializable(obj):
    """Convert protobuf decoded dict to JSON-serializable form."""
    if isinstance(obj, bytes):
        try:
            t = obj.decode("utf-8")
            if t.isprintable():
                return {"_type": "string", "_value": t.strip()}
        except UnicodeDecodeError:
            pass
        if len(obj) == 4:
            f32 = struct.unpack('<f', obj)[0]
            return {"_type": "bytes", "_hex": obj.hex(), "_len": len(obj), "_float32": f32}
        return {"_type": "bytes", "_hex": obj.hex() if len(obj) <= 256 else obj[:256].hex() + "...", "_len": len(obj)}
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_serializable(v) for v in obj]
    return obj


def search_for_shortcut(obj, path: str = "") -> list[str]:
    """Recursively search decoded protobuf for any shortcut-related keys or values."""
    hits = []
    shortcut_keywords = ("shortcut", "robot_shortcut", "robot_shortcuts", "shutcut")

    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else str(k)
            k_str = str(k).lower()
            if any(kw in k_str for kw in shortcut_keywords):
                hits.append(f"KEY match at {child_path}: {fmt_value(v)[:120]}")
            if isinstance(v, bytes):
                try:
                    s = v.decode("utf-8", errors="replace").lower()
                    if any(kw in s for kw in shortcut_keywords):
                        hits.append(f"VALUE match at {child_path}: {v[:120]!r}")
                except Exception:
                    pass
            elif isinstance(v, str) and any(kw in v.lower() for kw in shortcut_keywords):
                hits.append(f"VALUE match at {child_path}: {v[:120]}")
            hits.extend(search_for_shortcut(v, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(search_for_shortcut(item, f"{path}[{i}]"))

    return hits


# ── State tracking ───────────────────────────────────────────────────────
current_state = "?"

def update_state(decoded: dict):
    global current_state
    field3 = decoded.get("3", {})
    if isinstance(field3, dict) and "1" in field3:
        val = int(field3["1"])
        current_state = STATE_NAMES.get(val, f"UNKNOWN({val})")


# ── Core send/receive ───────────────────────────────────────────────────
async def send_and_capture(ws, full_topic: str, payload: bytes, timeout: float = RESPONSE_TIMEOUT) -> dict:
    """Send command on a raw websocket, capture full response."""
    frame = build_frame(full_topic, payload)
    t0 = time.monotonic()
    await ws.send(frame)

    deadline = t0 + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 0.5))
        except TimeoutError:
            continue

        if not isinstance(data, bytes) or len(data) < 4:
            continue
        try:
            msg = parse_frame(data)
        except ProtocolError:
            continue

        decoded = decode_protobuf(msg.payload) if msg.payload else {}

        if msg.field_tag == PROTOBUF_FIELD5_TAG:
            elapsed = (time.monotonic() - t0) * 1000
            return {
                "status": "response",
                "data": decoded,
                "raw_len": len(msg.payload) if msg.payload else 0,
                "ms": elapsed,
            }

        if msg.short_topic == "status/robot_base_status":
            update_state(decoded)

    elapsed = (time.monotonic() - t0) * 1000
    return {"status": "timeout", "data": {}, "raw_len": 0, "ms": elapsed}


async def drain_ws(ws, seconds: float):
    """Read broadcasts for N seconds to let robot settle."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 0.5))
        except TimeoutError:
            continue
        if not isinstance(data, bytes) or len(data) < 4:
            continue
        try:
            msg = parse_frame(data)
        except ProtocolError:
            continue
        if msg.field_tag != PROTOBUF_FIELD5_TAG:
            decoded = decode_protobuf(msg.payload) if msg.payload else {}
            if msg.short_topic == "status/robot_base_status":
                update_state(decoded)


async def listen_for_shortcut_broadcasts(ws, full_prefix: str, seconds: float):
    """Listen for broadcasts and flag any topic containing 'shortcut'."""
    global broadcast_topics_seen

    log(f"\n{B}Listening for shortcut-related broadcasts ({seconds:.0f}s)...{X}")
    log(f"{D}Trigger a shortcut in the Narwal app now if possible.{X}")

    deadline = time.monotonic() + seconds
    topics_logged: set[str] = set()

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 0.5))
        except TimeoutError:
            continue
        if not isinstance(data, bytes) or len(data) < 4:
            continue
        try:
            msg = parse_frame(data)
        except ProtocolError:
            continue

        short = msg.short_topic.lower()
        topic_key = msg.short_topic

        if "shortcut" in short or "shutcut" in short:
            if topic_key not in topics_logged:
                topics_logged.add(topic_key)
                decoded = decode_protobuf(msg.payload) if msg.payload else {}
                log(f"  {G}*** SHORTCUT BROADCAST: {msg.short_topic}{X}")
                for k, v in decoded.items():
                    log(f"    field {k}: {fmt_value(v, depth=1)}")
                broadcast_topics_seen.append({
                    "topic": msg.short_topic,
                    "data": make_serializable(decoded),
                })

        decoded = decode_protobuf(msg.payload) if msg.payload else {}
        hits = search_for_shortcut(decoded)
        if hits:
            log(f"  {M}*** SHORTCUT KEYWORDS in broadcast {msg.short_topic}:{X}")
            for hit in hits:
                log(f"    {hit}")
            if topic_key not in topics_logged:
                broadcast_topics_seen.append({
                    "topic": msg.short_topic,
                    "keyword_hits": hits,
                    "data": make_serializable(decoded),
                })

        if msg.short_topic == "status/robot_base_status":
            update_state(decoded)

    if not broadcast_topics_seen:
        log(f"  {Y}No shortcut-related broadcasts detected.{X}")


# ── Main probe ───────────────────────────────────────────────────────────
async def main():
    """Main probe entry point — discovers device_id via NarwalClient, then probes topics."""
    quick = "--quick" in sys.argv

    log_raw(f"\n{B}{'='*65}{X}")
    log_raw(f"{B}  NARWAL SHORTCUT TOPIC DISCOVERY PROBE{X}")
    log_raw(f"{B}{'='*65}{X}")
    log_raw(f"  {len(PROBE_TOPICS)} topics | {RESPONSE_TIMEOUT}s timeout each")
    if not quick:
        log_raw(f"  + {BROADCAST_LISTEN_SECS:.0f}s broadcast listener")
    log_raw("")

    # Connect via NarwalClient to call discover_device_id
    log(f"Connecting to {HOST}:{PORT}...")
    client = NarwalClient(host=HOST, device_id="probe")
    try:
        await client.connect()
    except Exception as e:
        log(f"{R}Connection failed: {e}{X}")
        return

    log(f"{G}Connected. Discovering device_id...{X}")

    try:
        await client.discover_device_id()
    except Exception as e:
        log(f"{Y}discover_device_id raised: {e} — continuing with what we have{X}")

    prefix = client.topic_prefix
    device_id = client.device_id
    log(f"{G}Topic prefix: {prefix}  device_id: {device_id}{X}")
    log(f"State: {G}{current_state}{X}")
    log_raw("")

    # Get the underlying websocket for direct frame sending
    ws = client._ws
    if ws is None:
        log(f"{R}No websocket available after connect — aborting{X}")
        return

    await drain_ws(ws, 2.0)

    # Probe each topic
    for i, (label, topic, hex_payload, desc) in enumerate(PROBE_TOPICS, 1):
        payload = bytes.fromhex(hex_payload) if hex_payload else b""
        full_topic = f"{prefix}/{device_id}/{topic}"

        log(f"{B}[{i}/{len(PROBE_TOPICS)}] {label}{X}  →  {C}{topic}{X}")
        log(f"  {D}{desc}{X}")

        result = await send_and_capture(ws, full_topic, payload)
        status = result["status"]
        data = result["data"]
        raw_len = result["raw_len"]
        ms = result["ms"]

        if status == "timeout":
            log(f"  {R}TIMEOUT ({ms:.0f}ms){X}")
        else:
            result_code = data.get("1", None)
            field_count = len(data)

            if field_count <= 1 and result_code in (1, 2, 3):
                names = {1: "SUCCESS", 2: "NOT_APPLICABLE", 3: "CONFLICT"}
                color = G if result_code == 1 else Y
                log(f"  {color}{names.get(result_code, f'CODE_{result_code}')}{X} ({ms:.0f}ms, {raw_len}B)")
            else:
                log(f"  {G}DATA RESPONSE{X} ({ms:.0f}ms, {raw_len}B, {field_count} fields)")
                for k, v in data.items():
                    formatted = fmt_value(v, depth=1)
                    if "\n" in formatted:
                        log(f"  field {k}:")
                        for line in formatted.split("\n"):
                            log(f"    {line}")
                    else:
                        log(f"  field {k}: {formatted}")

        hits = search_for_shortcut(data)
        if hits:
            log(f"  {M}*** SHORTCUT KEYWORDS FOUND:{X}")
            for hit in hits:
                log(f"    {hit}")

        all_responses[label] = {
            "topic": topic,
            "status": status,
            "raw_len": raw_len,
            "ms": ms,
            "data": make_serializable(data),
            "shortcut_hits": hits,
        }

        if i < len(PROBE_TOPICS):
            await drain_ws(ws, INTER_CMD_DELAY)

    # Broadcast listen phase
    if not quick:
        await listen_for_shortcut_broadcasts(ws, prefix, BROADCAST_LISTEN_SECS)

    try:
        await client.disconnect()
    except Exception:
        pass
    log("\nDisconnected.")

    # ── Summary ──────────────────────────────────────────────────────────
    log_raw(f"\n{B}{'='*65}{X}")
    log_raw(f"{B}  PROBE SUMMARY{X}")
    log_raw(f"{B}{'='*65}{X}")
    log_raw(f"  {'Label':<22} {'Status':<20} {'Size':>8}  {'Fields':>6}  Shortcut?")
    log_raw(f"  {'─'*22} {'─'*20} {'─'*8}  {'─'*6}  {'─'*9}")

    for label, resp in all_responses.items():
        s = resp["status"]
        hits = resp.get("shortcut_hits", [])
        if s == "timeout":
            sc = f"{R}{'TIMEOUT':<20}{X}"
        elif len(resp["data"]) <= 1:
            sc = f"{Y}{'SIMPLE':<20}{X}"
        else:
            sc = f"{G}{'DATA':<20}{X}"
        fields = len(resp["data"]) if resp["data"] else 0
        has_shortcut = f"{M}YES ({len(hits)}){X}" if hits else "-"
        log_raw(f"  {label:<22} {sc} {resp['raw_len']:>7}B  {fields:>6}  {has_shortcut}")

    # Shortcut conclusion
    log_raw(f"\n{B}  SHORTCUT DISCOVERY RESULT{X}")
    shortcut_found = any(resp.get("shortcut_hits") for resp in all_responses.values())
    broadcast_shortcut = len(broadcast_topics_seen) > 0

    if shortcut_found:
        log_raw(f"  {G}*** Shortcut data found in command responses!{X}")
        for label, resp in all_responses.items():
            if resp.get("shortcut_hits"):
                log_raw(f"    - {label} ({resp['topic']}): {len(resp['shortcut_hits'])} match(es)")
    elif broadcast_shortcut:
        log_raw(f"  {G}*** Shortcut data found in broadcasts!{X}")
        for b in broadcast_topics_seen:
            log_raw(f"    - Broadcast: {b['topic']}")
    else:
        log_raw(f"  {Y}No shortcut data found in any local WS response or broadcast.{X}")
        log_raw(f"  {Y}→ Likely cloud-only (Alibaba device shadow / NrRobotShortcutListGetRequester){X}")
        log_raw(f"  {Y}→ Plan 02 fallback: manual config entry (user enters shortcut name + topic){X}")

    # Save results
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = Path(__file__).parent
    out_dir.mkdir(exist_ok=True)

    json_path = out_dir / f"probe_shortcuts_{now}.json"
    output = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "host": HOST,
        "topic_prefix": prefix,
        "device_id": device_id,
        "results": {
            label: {
                "topic": resp["topic"],
                "status": resp["status"],
                "response_code": resp["data"].get("1") if isinstance(resp["data"], dict) else None,
                "raw_len": resp["raw_len"],
                "ms": resp["ms"],
                "decoded": resp["data"],
                "shortcut_hits": resp.get("shortcut_hits", []),
            }
            for label, resp in all_responses.items()
        },
        "broadcast_shortcut_topics": broadcast_topics_seen,
        "shortcut_found_locally": shortcut_found or broadcast_shortcut,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n{G}JSON: {json_path}{X}")

    # Text log (ANSI stripped)
    ansi_re = re.compile(r'\033\[[0-9;]*m')
    log_path = out_dir / f"probe_shortcuts_{now}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        for line in log_lines:
            f.write(ansi_re.sub("", line) + "\n")
    print(f"{G}Log: {log_path}{X}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{D}Stopped.{X}")
