"""Decode Narwal WebSocket frames from a pcap (phone → robot capture).

Reassembles each TCP stream to/from robot port 9002, parses the WebSocket
framing (unmasking client→server frames per RFC 6455), then decodes the
inner Narwal frame (parse_frame) and its protobuf payload. Highlights the
command that starts a clean — especially anything carrying zone geometry.

Usage:
    .venv/bin/python tools/decode_ws_pcap.py tools/phone_capture.pcap
    .venv/bin/python tools/decode_ws_pcap.py tools/phone_capture.pcap --all
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import struct

import blackboxprotobuf
from scapy.all import IP, TCP, Ether

from narwal_client.protocol import ProtocolError, parse_frame

ROBOT_PORT = 9002


def read_packets(pcap_path):
    """Yield scapy packets from a pcap or pcapng file.

    pymobiledevice3 writes a pcapng that scapy's rdpcap rejects ("Invalid
    Block body length"), so walk the blocks by hand: pull Enhanced/Simple
    Packet Blocks and dissect their bytes with the interface's link type
    (Ethernet=1 here). Falls back to scapy rdpcap for classic .pcap.
    """
    data = open(pcap_path, "rb").read()
    if data[:4] != b"\x0a\x0d\x0d\x0a":  # not pcapng → let scapy try
        from scapy.all import rdpcap
        yield from rdpcap(pcap_path)
        return
    off = 0
    linktype = 1
    little = True
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if btype == 0x0A0D0D0A:  # section header — check byte order
            bom, = struct.unpack_from("<I", data, off + 8)
            little = bom == 0x1A2B3C4D
        if blen < 12 or off + blen > len(data):
            break
        body = data[off + 8:off + blen - 4]
        if btype == 0x00000001:  # Interface Description Block
            linktype = struct.unpack_from("<H", body, 0)[0]
        elif btype == 0x00000006:  # Enhanced Packet Block
            caplen = struct.unpack_from("<I", body, 12)[0]
            pkt = body[20:20 + caplen]
            try:
                yield Ether(pkt) if linktype == 1 else IP(pkt)
            except Exception:
                pass
        elif btype == 0x00000003:  # Simple Packet Block
            origlen = struct.unpack_from("<I", body, 0)[0]
            pkt = body[4:4 + origlen]
            try:
                yield Ether(pkt) if linktype == 1 else IP(pkt)
            except Exception:
                pass
        off += blen


def reassemble_streams(pcap_path):
    """Return {(src,sport,dst,dport): reassembled bytes} for port-9002 flows."""
    segs = defaultdict(dict)  # flow -> {seq: payload}
    for pkt in read_packets(pcap_path):
        if not (pkt.haslayer(TCP) and pkt.haslayer(IP)):
            continue
        tcp, ip = pkt[TCP], pkt[IP]
        if tcp.sport != ROBOT_PORT and tcp.dport != ROBOT_PORT:
            continue
        payload = bytes(tcp.payload)
        if not payload:
            continue
        flow = (ip.src, tcp.sport, ip.dst, tcp.dport)
        segs[flow][tcp.seq] = payload
    streams = {}
    for flow, byseq in segs.items():
        data = b"".join(byseq[s] for s in sorted(byseq))
        streams[flow] = data
    return streams


def iter_ws_frames(buf):
    """Yield (opcode, payload_bytes) from a WebSocket byte stream.

    Handles masking (client→server frames are masked) and the 7/16/64-bit
    length forms. Skips the HTTP upgrade preamble if present.
    """
    i = 0
    # Skip an HTTP upgrade header if this is the start of the stream.
    if buf[:3] in (b"GET", b"HTTP"):
        end = buf.find(b"\r\n\r\n")
        if end != -1:
            i = end + 4
    n = len(buf)
    while i + 2 <= n:
        b0, b1 = buf[i], buf[i + 1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        j = i + 2
        if ln == 126:
            if j + 2 > n:
                break
            ln = int.from_bytes(buf[j:j + 2], "big"); j += 2
        elif ln == 127:
            if j + 8 > n:
                break
            ln = int.from_bytes(buf[j:j + 8], "big"); j += 8
        mask = b""
        if masked:
            if j + 4 > n:
                break
            mask = buf[j:j + 4]; j += 4
        if j + ln > n:
            break
        data = bytearray(buf[j:j + ln])
        if masked:
            for k in range(ln):
                data[k] ^= mask[k % 4]
        yield opcode, bytes(data)
        i = j + ln


def decode_narwal(payload):
    try:
        msg = parse_frame(payload)
    except ProtocolError as e:
        return None, f"(not a Narwal frame: {e})"
    try:
        decoded, _ = blackboxprotobuf.decode_message(msg.payload)
    except Exception as e:
        decoded = {"_decode_error": str(e)}
    return msg.short_topic, decoded


def jsonable(o):
    if isinstance(o, bytes):
        return o.hex()
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [jsonable(v) for v in o]
    return o


INTERESTING = ("clean/", "zone", "area", "plan", "spot", "region")


def summarize_talkers(pcap_path):
    """List remote endpoints the phone talked to (bytes each way).

    Answers 'did the zone start go to the robot locally or to the cloud?'.
    """
    from scapy.all import UDP
    flows = defaultdict(lambda: [0, 0])  # (ip,port,proto) -> [pkts, bytes]
    for pkt in read_packets(pcap_path):
        if not pkt.haslayer(IP):
            continue
        ip = pkt[IP]
        if pkt.haslayer(TCP):
            l4, proto = pkt[TCP], "tcp"
        elif pkt.haslayer(UDP):
            l4, proto = pkt[UDP], "udp"
        else:
            continue
        # The phone is the LAN 192.168.x endpoint; the "remote" is the other side.
        for host, port in ((ip.dst, getattr(l4, "dport", 0)),
                           (ip.src, getattr(l4, "sport", 0))):
            if not host.startswith(("192.168.", "10.", "172.")):
                flows[(host, port, proto)][0] += 1
                flows[(host, port, proto)][1] += len(bytes(pkt))
                break
        else:
            # both ends local (e.g. phone↔robot) — key on the non-phone LAN IP
            remote = ip.dst if ip.dst != ip.src else ip.dst
            flows[(ip.dst, getattr(l4, "dport", 0), proto)][0] += 1
            flows[(ip.dst, getattr(l4, "dport", 0), proto)][1] += len(bytes(pkt))
    print("remote endpoints (pkts, bytes):")
    for (host, port, proto), (pk, by) in sorted(
            flows.items(), key=lambda kv: -kv[1][1]):
        print(f"  {proto:3} {host}:{port:<5}  {pk:5} pkts  {by:8} B")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: decode_ws_pcap.py <pcap> [--all] [--talkers]")
    pcap_path = sys.argv[1]
    show_all = "--all" in sys.argv

    if "--talkers" in sys.argv:
        summarize_talkers(pcap_path)
        return

    streams = reassemble_streams(pcap_path)
    print(f"port-9002 TCP flows: {len(streams)}")
    hits = []
    for flow, buf in streams.items():
        src, sport, dst, dport = flow
        direction = "phone→robot" if dport == ROBOT_PORT else "robot→phone"
        for opcode, payload in iter_ws_frames(buf):
            if opcode not in (0x1, 0x2):  # text/binary only
                continue
            topic, decoded = decode_narwal(payload)
            if topic is None:
                continue
            rec = {"dir": direction, "topic": topic,
                   "hex": payload.hex(), "decoded": jsonable(decoded)}
            interesting = any(k in topic for k in INTERESTING)
            if interesting or show_all:
                hits.append(rec)

    # Commands come phone→robot; surface those first.
    hits.sort(key=lambda r: (r["dir"] != "phone→robot", r["topic"]))
    print(f"decoded Narwal frames of interest: {len(hits)}\n")
    for rec in hits:
        print(f"=== [{rec['dir']}] {rec['topic']} ===")
        print(json.dumps(rec["decoded"], ensure_ascii=False, indent=1)[:2500])
        print(f"  raw hex: {rec['hex'][:160]}{'...' if len(rec['hex'])>160 else ''}")
        print()

    out = Path(pcap_path).with_suffix(".decoded.json")
    out.write_text(json.dumps(hits, ensure_ascii=False, indent=1))
    print(f"full decode → {out}")


if __name__ == "__main__":
    main()
