"""Start one zone and monitor it to completion (no early stop).

Verifies the robot cleans a whole zone and returns to the dock on its own.
Robot moves for the full clean. Owner consent required.

Usage: python tools/verify_zone_full.py [--rect 0,0,20,20] [--max-min 12]
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from narwal_client.client import NarwalClient
from narwal_client.const import CommandResult

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
RECT = (0, 0, 20, 20)
STATUS = {1: "STANDBY", 2: "DOCKED_V2", 3: "MOP_WASHING", 4: "CLEANING",
          5: "CLEANING", 10: "DOCKED", 14: "CHARGED"}


async def connect_awake(host, attempts=4):
    for i in range(attempts):
        c = NarwalClient(host=host, device_id="probe")
        try:
            await c.connect()
            await c.discover_device_id()
            return c
        except Exception as e:
            print(f"  wake attempt {i+1} failed: {str(e)[:50]}")
            try:
                await c.disconnect()
            except Exception:
                pass
            await asyncio.sleep(3)
    raise RuntimeError("robot would not wake — open the Narwal app once")


async def main():
    args = sys.argv[1:]
    rect = RECT
    if "--rect" in args:
        rect = tuple(int(v) for v in args[args.index("--rect") + 1].split(","))
    max_min = float(args[args.index("--max-min") + 1]) if "--max-min" in args else 12.0

    c = await connect_awake(HOST)
    print(f"connected {c.device_id}")

    r = await c.start_zone([rect])
    name = CommandResult(r.result_code).name if r.result_code in list(CommandResult) else str(r.result_code)
    print(f"start_zone({rect}): {r.result_code} ({name})")
    if r.result_code != CommandResult.SUCCESS:
        await c.disconnect()
        return

    print("monitoring to completion (no stop) ...")
    deadline = time.monotonic() + max_min * 60
    was_cleaning = False
    area0 = None
    while time.monotonic() < deadline:
        await asyncio.sleep(15)
        try:
            await c.get_status()
        except Exception as e:
            print(f"  status hiccup: {str(e)[:40]}; reconnecting")
            try:
                await c.disconnect()
            except Exception:
                pass
            c = await connect_awake(HOST)
            continue
        st = int(getattr(c.state, "working_status", 0) or 0)
        docked = getattr(c.state, "is_docked", False)
        area = getattr(c.state, "cleaning_area", None)
        el = int((max_min * 60) - (deadline - time.monotonic()))
        print(f"  t+{el:3d}s  status={st}({STATUS.get(st,'?')}) docked={docked} area={area}")
        if st in (4, 5):
            was_cleaning = True
        # finished: was cleaning, now docked/standby/charging
        if was_cleaning and (docked or st in (10, 14)):
            print("\n✅ ZONE CLEAN COMPLETE — robot back at dock.")
            await c.disconnect()
            return
    print("\n⏱ reached time cap; recalling robot to dock.")
    try:
        await c.return_to_base()
    except Exception:
        pass
    await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
