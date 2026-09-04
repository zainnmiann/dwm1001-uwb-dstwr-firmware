#!/usr/bin/env python3
"""Print every raw $RNG record from every connected tag. Ctrl-C to stop.

    ./raw.py                decoded, one line per range
    ./raw.py --json         one JSON object per range, the bridge's format
    ./raw.py --table        a live table, one row per tag-anchor pair
    ./raw.py --plain        the untouched $RNG lines, exactly as the tag sends

    ./raw.py --json --labels=long      TAG1/ANC7 instead of T01/A07
    ./raw.py --json --unordered        do not wait to order by superframe

--json emits the six fields the bridge publishes to
uwb/ranging/<tag>/<anchor>, so a consumer written against the MQTT stream
works here too, straight off serial with no broker in the way.

Records are released a whole superframe at a time, in TDMA order: every one
of tag 1's anchors, then every one of tag 2's, then the next frame. Each tag
arrives on its own port, so without that they interleave by whichever port the
OS happened to deliver first. The tag's own sfn is what orders them -- host
arrival time cannot, since the OS hands over ~50 records at a time.
"""
import json, sys, threading, time
import serial
import dwm_bin
from dwm_ports import dwm_ports, normalise_sn

NAMES = {"760131590": "TAG1", "760131633": "TAG2"}
# All eight anchors. This map used to stop at ANC5, so anchors 6, 7 and 8
# reported their raw hex id and no consumer keyed on labels could see them.
ANCH = {f"{0x0101 + i:04X}": f"ANC{i + 1}" for i in range(16)}

FLAGS = [(0x01, "ss_fallback"), (0x02, "nlos"), (0x04, "resp_lost"),
         (0x08, "report_lost"), (0x10, "desync"), (0x20, "hash_mismatch")]

LOCK = threading.Lock()
PLAIN = "--plain" in sys.argv
JSON = "--json" in sys.argv
TABLE = "--table" in sys.argv
LONG_LABELS = "--labels=long" in sys.argv
ORDERED = "--unordered" not in sys.argv
# Discarded input is counted and reported, never dropped in silence. A
# truncated text stream once made a perfectly healthy anchor look dead for an
# hour precisely because every tool threw malformed lines away without a word.
BAD = {}          # tag -> count of unparseable text lines


def label(name):
    """T01 / A07 by default, TAG1 / ANC7 with --labels=long.

    The short form is what the downstream consumer asked for; the long form is
    what provision.py and the reports use, so both have to be reachable.
    """
    if LONG_LABELS:
        return name
    if name.startswith("TAG"):
        return "T%02d" % int(name[3:])
    if name.startswith("ANC"):
        return "A%02d" % int(name[3:])
    return name


def record(tag, anchor_id, range_m, rssi, t_us):
    return {"tag_label": label(tag),
            "anchor_label": label(ANCH.get(anchor_id, anchor_id)),
            "range_m": round(range_m, 3),
            "rssi": rssi,
            "timestamp_sent": t_us,
            "timestamp_received": round(time.time(), 3)}


class Sequencer:
    """Release records one superframe at a time, tags in frame order.

    A tag's records for one sfn all arrive together, but the two tags are read
    from separate ports, so their frames overlap in host time. Holding each
    sfn until every reader has moved past it restores the order the TDMA
    actually transmitted in.
    """

    def __init__(self, tags, emit):
        self.emit = emit
        self.order = sorted(tags)               # TAG1 before TAG2
        self.frames = {}                        # sfn -> {tag: [rec, ...]}
        self.seen = {t: -1 for t in tags}       # highest sfn each tag reached
        self.released = -1

    def add(self, tag, sfn, rec):
        with LOCK:
            if sfn <= self.released:
                return                          # a straggler: its frame is gone
            self.frames.setdefault(sfn, {}).setdefault(tag, []).append(rec)
            if sfn > self.seen[tag]:
                self.seen[tag] = sfn
            self._flush()

    def _flush(self):
        """Release every frame that no reader can still add to."""
        safe = min(self.seen.values())
        for sfn in sorted(self.frames):
            if sfn >= safe:
                break                           # a slower tag may still add
            frame = self.frames.pop(sfn)
            self.released = sfn
            for tag in self.order:
                for rec in sorted(frame.get(tag, []),
                                  key=lambda r: r["anchor_label"]):
                    self.emit(rec)

    def drain(self):
        with LOCK:
            for sfn in sorted(self.frames):
                for tag in self.order:
                    for rec in sorted(self.frames[sfn].get(tag, []),
                                      key=lambda r: r["anchor_label"]):
                        self.emit(rec)
            self.frames.clear()


def emit_json(rec):
    print(json.dumps(rec, separators=(",", ":")), flush=True)


TABLE_STATE = {}


def emit_table(rec):
    """One row per pair, rewritten in place."""
    TABLE_STATE[(rec["tag_label"], rec["anchor_label"])] = rec
    if rec["anchor_label"] != max(k[1] for k in TABLE_STATE):
        return
    rows = sorted(TABLE_STATE)
    out = ["\033[H\033[2J  UWB live ranges          "
           + time.strftime("%H:%M:%S"),
           "  " + "-" * 52,
           f"  {'tag':5s} {'anchor':7s} {'range m':>9s} {'rssi':>8s}"
           f" {'age s':>7s}"]
    now = time.time()
    for k in rows:
        r = TABLE_STATE[k]
        out.append(f"  {k[0]:5s} {k[1]:7s} {r['range_m']:9.3f} "
                   f"{r['rssi']:8.1f} {now - r['timestamp_received']:7.2f}")
    print("\n".join(out), flush=True)


def emit_text(rec):
    print(f"  {rec['tag_label']:5s} {rec['anchor_label']:5s} "
          f"{rec['range_m']:8.3f} m  {rec['rssi']:>6.1f} dBm  "
          f"sent {rec['timestamp_sent']}", flush=True)


def reader_bin(dev, name, seq, emit):
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
            rec = record(name, r["anchor_id"], r["range_m"], r["rssi"],
                         r["t_us"])
            if seq:
                seq.add(name, r["sfn"], rec)
            else:
                with LOCK:
                    emit(rec)
        with LOCK:
            BAD[name] = d.lost()


def reader_csv(dev, name, seq, emit):
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
            try:
                flags = int(f[10], 16)
                if flags & 0x04:
                    continue
                rec = record(name, f[4][2:], int(f[6]) / 1000.0,
                             float(f[8]), int(f[11]))
                sfn = int(f[1])
            except (ValueError, IndexError):
                with LOCK:
                    BAD[name] = BAD.get(name, 0) + 1
                continue
            if seq:
                seq.add(name, sfn, rec)
            else:
                with LOCK:
                    emit(rec)


def main():
    ports = [(p.device, NAMES.get(normalise_sn(p.serial_number),
                                  normalise_sn(p.serial_number)[-4:]))
             for p in dwm_ports()]
    tags = [(d, n) for d, n in ports if n.startswith("TAG")]
    if not tags:
        sys.exit("no tags connected (anchors do not report ranges)")

    emit = emit_json if JSON else (emit_table if TABLE else emit_text)
    # Ordering needs every tag to be reporting; with one tag the stream is
    # already in slot order and buffering would only add latency.
    seq = None
    if ORDERED and not PLAIN and len(tags) > 1:
        seq = Sequencer([n for _, n in tags], emit)

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
        threading.Thread(target=reader_bin if binary else reader_csv,
                         args=(d, n, seq, emit), daemon=True).start()

    if seq:
        print("  ordered by superframe: "
              + " then ".join(label(n) for n in seq.order)
              + ", each frame in turn", file=sys.stderr)
    print("  Ctrl-C to stop\n", file=sys.stderr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if seq:
            seq.drain()
        with LOCK:
            bad = {k: v for k, v in BAD.items() if v}
        if bad:
            print("\n  DISCARDED input (truncated or corrupt): "
                  + ", ".join(f"{k} {v}" for k, v in sorted(bad.items()))
                  + "\n  check the tag's $STAT tx_dropped -- the UART is "
                    "losing output", file=sys.stderr)


if __name__ == "__main__":
    main()
