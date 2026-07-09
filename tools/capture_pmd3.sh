#!/bin/bash
# Capture iPhone traffic with pymobiledevice3 (the maintained replacement for
# the broken rvictl on iOS 17+/macOS 26). Talks to the device's pcapd service
# over the modern RSD tunnel and writes a standard pcap.
#
# Usage:
#   tools/capture_pmd3.sh [seconds]
# Requires: iPhone on USB, unlocked, Developer Mode ON. The RSD tunnel needs
# root, so this uses sudo for the tunnel daemon (you'll be asked once).

set -uo pipefail

SECS="${1:-90}"
HERE="$(cd "$(dirname "$0")" && pwd)"     # tools/
ROOT="$(dirname "${HERE}")"               # worktree root (venvs live here)
PMD="${ROOT}/.venv-pmd3/bin/python -m pymobiledevice3"
OUT="${HERE}/phone_capture.pcap"          # keep pcap in tools/
if [[ ! -x "${ROOT}/.venv-pmd3/bin/python" ]]; then
  echo "pymobiledevice3 venv not found at ${ROOT}/.venv-pmd3 — wrong dir?"
  exit 1
fi

echo "== Narwal phone capture (pymobiledevice3) =="

# 0) Device present? Retry a few times — USB re-enumeration can lag.
SEEN=""
for _ in 1 2 3 4 5; do
  if ${PMD} usbmux list 2>/dev/null | grep -q '"Identifier"'; then
    SEEN=1; break
  fi
  sleep 1
done
if [[ -z "${SEEN}" ]]; then
  echo "No device over usbmux. Plug the iPhone in with a CABLE, unlock it,"
  echo "tap 'Trust' if asked, keep it unlocked, then re-run."
  exit 1
fi
echo "Device visible over usbmux."

# NOTE: pcapd over the RSD tunnel does NOT stream on iOS 17+/26 — it returns
# "com.apple.pcapd.shim.remote service is USB only" (pmd3 issue #1515). The
# working path is DIRECT USB capture, run as root, with NO --tunnel:
#     sudo pymobiledevice3 pcap --out FILE
# So we skip tunneld entirely and run pcap over usbmux under sudo.

PCAP_PID=""
cleanup() {
  echo
  [[ -n "${PCAP_PID}" ]] && sudo kill "${PCAP_PID}" >/dev/null 2>&1 || true
  sudo pkill -f "pymobiledevice3 pcap" >/dev/null 2>&1 || true
  # Make the pcap readable/owned by the user for decoding + git.
  [[ -f "${OUT}" ]] && sudo chown "$(id -un)" "${OUT}" >/dev/null 2>&1 || true
  echo "pcap: ${OUT}"
  echo "Decode: .venv/bin/python tools/decode_ws_pcap.py '${OUT}' --talkers"
}
trap cleanup EXIT INT TERM

# Clear any stale, possibly root-owned pcap from an earlier run.
sudo rm -f "${OUT}" 2>/dev/null || true

# Capture directly over USB (root), killed after SECS (macOS has no `timeout`).
echo "Capturing ${SECS}s to ${OUT} (direct USB, sudo) ..."
echo ">>> NOW in the Narwal app: draw a ZONE and start the clean. <<<"
echo "    (keep the phone UNLOCKED; Ctrl-C to stop early)"
sudo ${PMD} pcap --out "${OUT}" >/tmp/pmd3_pcap.log 2>&1 &
PCAP_PID=$!
sleep "${SECS}"
sudo kill "${PCAP_PID}" >/dev/null 2>&1 || true
wait "${PCAP_PID}" 2>/dev/null || true
PCAP_PID=""

echo "Capture finished."
if [[ ! -s "${OUT}" ]]; then
  echo "  pcap is empty — pcap client log:"; tail -12 /tmp/pmd3_pcap.log | sed 's/^/    /'
fi
