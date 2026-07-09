"""Local config for the research/replay scripts in tools/.

Copy this file to `tools/config.py` and set your Narwal robot's LAN IP before
running the scripts. `tools/config.py` is gitignored so your IP never gets
committed. Alternatively, set the NARWAL_HOST environment variable.
"""

ROBOT_HOST = "192.168.x.x"  # your Narwal's local IP, e.g. 192.168.1.50
