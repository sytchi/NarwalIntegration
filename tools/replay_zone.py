"""Replay the byte-exact captured zone CleanTask via clean/start_clean.

Research verdict (2026-07-09): our earlier start_clean attempts got
NOT_APPLICABLE because the payload shape wasn't byte-exact AND the robot
wasn't docked. This reproduces the CleanTask the robot itself reported
after the app's cloud zone-start (tools/zone_task_snapshot.json), field for
field, and sends it on clean/start_clean while docked.

Three variants, in order (stop at first SUCCESS):
  V1 exact task WITHOUT the robot-computed coverage list (zone.field4)
  V2 exact task WITH the coverage list (fully verbatim)
  V3 V1 but TaskOption empty {} (in case field 3 differs by fw)

After SUCCESS: read current_clean_task echo, force-stop, recall.
Robot moves briefly on success. Owner consent required.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blackboxprotobuf

from narwal_client.client import NarwalClient
from narwal_client.const import CommandResult, TOPIC_CMD_CLEAN_TASK

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for local config.py
try:
    from config import ROBOT_HOST as HOST
except ImportError:
    import os

    HOST = os.environ.get("NARWAL_HOST", "")
    if not HOST:
        raise SystemExit(
            "Robot IP not set. Copy tools/config.example.py to tools/config.py "
            "and set ROBOT_HOST, or set the NARWAL_HOST environment variable."
        )
SNAP = Path(__file__).parent / "zone_task_snapshot.json"


def build(task: dict, with_coverage: bool):
    """Encode {1: CleanTask} with a typedef derived from the dict."""
    import copy
    task = copy.deepcopy(task)
    item = task["2"]
    zo = item["1"]
    if not with_coverage:
        zo.pop("4", None)

    vtd = {"type": "message", "seen_repeated": True,
           "message_typedef": {"1": {"type": "int"}, "2": {"type": "int"}}}
    zo_td = {"1": {"type": "int"}, "2": {"type": "int"}, "3": vtd}
    if with_coverage and "4" in zo:
        zo_td["4"] = vtd
    param_td = {k: {"type": "int"} for k in item["2"]}
    taskopt_td = {k: {"type": "int"} for k in task.get("3", {})}
    item_td = {
        "type": "message",
        "message_typedef": {
            "1": {"type": "message", "message_typedef": zo_td},
            "2": {"type": "message", "message_typedef": param_td},
            "3": {"type": "int"},
        },
    }
    typedef = {"1": {"type": "message", "message_typedef": {
        "1": {"type": "int"},
        "2": item_td,
        "3": {"type": "message", "message_typedef": taskopt_td},
        "5": {"type": "int"},
    }}}
    return blackboxprotobuf.encode_message({"1": task}, typedef)


def _prune(o):
    if isinstance(o, dict):
        return {k: _prune(v) for k, v in o.items()}
    if isinstance(o, list):
        if len(o) > 6 and all(isinstance(x, dict) and set(x) <= {"1", "2"} for x in o):
            return o[:3] + [f"...{len(o) - 6} pts..."] + o[-3:]
        return [_prune(x) for x in o]
    return o


async def _wait_docked(client, tries=6):
    for _ in range(tries):
        await client.get_status()
        st = int(getattr(client.state, "working_status", 0) or 0)
        if getattr(client.state, "is_docked", False) and st not in (4, 5):
            return st
        await asyncio.sleep(2)
    return -1


async def main():
    snap = json.load(open(SNAP))
    task = snap["decoded"]["2"]

    client = NarwalClient(host=HOST, device_id="probe")
    await client.connect()
    await client.discover_device_id()
    st = await _wait_docked(client)
    print(f"connected {client.device_id}, docked status={st}")

    variants = [
        ("V1 exact, no coverage", build(task, False)),
        ("V2 exact, WITH coverage", build(task, True)),
        ("V3 no coverage, TaskOption={}", build({**task, "3": {}}, False)),
    ]
    winner = None
    for label, payload in variants:
        print(f"\n=== {label} ({len(payload)}B) ===")
        resp = await client.send_command(TOPIC_CMD_CLEAN_TASK, payload=payload, timeout=10.0)
        name = CommandResult(resp.result_code).name if resp.result_code in list(CommandResult) else str(resp.result_code)
        print(f"result: {resp.result_code} ({name})")
        if resp.result_code == CommandResult.SUCCESS:
            winner = label
            break
        await asyncio.sleep(2)

    if winner:
        print(f"\n*** ACCEPTED: {winner} ***")
        await asyncio.sleep(4)
        t = await client.get_current_task()
        print("queued:", json.dumps(_prune(t.data), default=str)[:1200])
        await client.stop()
        await asyncio.sleep(3)
        await client.return_to_base()
        print("stopped + recalled")
    else:
        print("\nno variant accepted (still NOT_APPLICABLE while docked)")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
