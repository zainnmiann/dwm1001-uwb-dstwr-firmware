#!/usr/bin/env python3
"""Print every raw $RNG record from every connected tag. Ctrl-C to stop.

    ./raw.py              decoded, one line per range
    ./raw.py --json       one JSON object per range, the bridge's format
    ./raw.py --plain      the untouched $RNG lines, exactly as the tag sends

--json emits the same six fields localize.py publishes to
uwb/ranging/<tag>/<anchor>, so a consumer written against the MQTT stream works
here too -- but straight off serial, with no broker or bridge to start. The one
difference: records appear as each tag's port delivers them, so they are in
slot order within a tag but not strictly ordered across tags. The MQTT path
buffers by superframe to get that; see localize.py's sequencer.
"""
import json, sys, threading, time
import serial
import dwm_bin
from dwm_ports import dwm_ports, normalise_sn

NAMES = {"760131590": "TAG1", "760131633": "TAG2"}
ANCH = {"0101": "ANC1", "0102": "ANC2", "0103": "ANC3", "0104": "ANC4",
        "0105": "ANC5"}
FLAGS = [(0x01, "ss_fallback"), (0x02, "nlos"), (0x04, "resp_lost"),
         (0x08, "report_lost"), (0x10, "desync"), (0x20, "hash_mismatch")]
LOCK = threading.Lock()
PLAIN = "--plain" in sys.argv
JSON = "--json" in sys.argv
# Discarded input is counted and reported, never dropped in silence. A
# truncated text stream once made a perfectly healthy anchor look dead for an
# hour precisely because every tool threw malformed lines away without a word.
BAD = {}          # tag -> count of unparseable text lines


def reader_bin(dev, name):
    """Decode the binary stream (out_format = BIN)."""
    d = dwm_bin.Decoder()
    try:
        s = serial.Serial(dev, 115200, timeout=0.3)
    except Exception as e:
        print(f"  cannot open {dev}: {e}", file=sys.stderr)
        return
    while True:
        for r in d.feed(s.read(4096)):
            if r["flags"] & 0x04:             # RESP_LOST carries no distance
                continue
            if JSON:
                rec = {"tag_label": name,
                       "anchor_label": ANCH.get(r["anchor_id"], r["anchor_id"]),
                       "range_m": round(r["range_m"], 3),
                       "rssi": r["rssi"],
                       "timestamp_sent": r["t_us"],
                       "timestamp_received": round(time.time(), 3)}
                with LOCK:
                    print(json.dumps(rec, separators=(",", ":")), flush=True)
            else:
                names = ",".join(r["flag_names"]) or "-"
                with LOCK:
                    print(f"  {name}  "
                          f"{ANCH.get(r['anchor_id'], r['anchor_id']):5s}  "
                          f"slot {r['slot']}  {r['range_m']:7.3f} m  "
                          f"{r['rssi']:>6.1f} dBm  tof {r['tof_ps']:>7d} ps  "
                          f"sfn {r['sfn']:>6d}  {names}", flush=True)
        with LOCK:
            BAD[name] = d.lost()


def reader(dev, name):
    buf = ""
    try:
        s = serial.Serial(dev, 115200, timeout=0.3)
    except Exception as e:
        print(f"  cannot open {dev}: {e}", file=sys.stderr)
        return
    while True:
        buf += s.read(4096).decode("utf-8", "replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("$RNG"):
                continue
            if PLAIN:
                with LOCK:
                    print(f"{name} {line}", flush=True)
                continue
            f = line.split("*")[0].split(",")
            if len(f) < 12:
                with LOCK:                    # truncated by UART overflow
                    BAD[name] = BAD.get(name, 0) + 1
                continue
            if JSON:
                try:
                    rec = {
                        "tag_label": name,
                        "anchor_label": ANCH.get(f[4][2:], f[4][2:]),
                        "range_m": round(int(f[6]) / 1000.0, 3),
                        "rssi": float(f[8]),
                        "timestamp_sent": int(f[11]),
                        "timestamp_received": round(time.time(), 3),
                    }
                except (ValueError, IndexError):
                    continue
                # A lost Response carries no distance; publishing 0.0 m as a
                # range would be worse than dropping the record.
                if int(f[10], 16) & 0x04:
                    continue
                with LOCK:
                    print(json.dumps(rec, separators=(",", ":")), flush=True)
                continue
            try:
                sfn, anch, slot = f[1], f[4][2:], f[5]
                d_mm, tof, rxp, flags = int(f[6]), f[7], f[8], int(f[10], 16)
            except (ValueError, IndexError):
                continue
            names = ",".join(n for b, n in FLAGS if flags & b) or "-"
            with LOCK:
                print(f"  {name}  {ANCH.get(anch, anch):5s}  slot {slot}  "
                      f"{d_mm/1000.0:7.3f} m  {rxp:>6s} dBm  "
                      f"tof {tof:>7s} ps  sfn {sfn:>6s}  {names}", flush=True)


def main():
    ports = [(p.device, NAMES.get(normalise_sn(p.serial_number),
                                  normalise_sn(p.serial_number)[-4:]))
             for p in dwm_ports()]
    tags = [(d, n) for d, n in ports if n.startswith("TAG")]
    if not tags:
        sys.exit("no tags connected (anchors do not report ranges)")
    for d, n in tags:
        # Sniff rather than require a flag: the tag decides its own format, and
        # guessing wrong looks exactly like a dead tag.
        try:
            probe = serial.Serial(d, 115200, timeout=0.3)
            time.sleep(0.4)
            head = probe.read(4096)
            probe.close()
        except Exception as e:
            print(f"  cannot open {d}: {e}", file=sys.stderr)
            continue
        binary = dwm_bin.SYNC in head and b"$RNG" not in head
        fmt = "BIN" if binary else "CSV"
        print(f"  reading {n} on {d}  ({fmt})", file=sys.stderr)
        threading.Thread(target=reader_bin if binary else reader,
                         args=(d, n), daemon=True).start()
    print("  Ctrl-C to stop\n", file=sys.stderr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        with LOCK:
            bad = {k: v for k, v in BAD.items() if v}
        if bad:
            print("\n  DISCARDED input (truncated or corrupt): "
                  + ", ".join(f"{k} {v}" for k, v in sorted(bad.items()))
                  + "\n  check the tag's $STAT tx_dropped -- the UART is "
                    "losing output", file=sys.stderr)


if __name__ == "__main__":
    main()
