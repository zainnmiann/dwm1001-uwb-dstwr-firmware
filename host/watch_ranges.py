#!/usr/bin/env python3
"""Live per-pair range table, straight off the tags' serial ports.

No broker, no HTTP, nothing to start first -- this reads the $RNG stream
directly and is the quickest way to see whether the network is ranging.

    ./watch_ranges.py                 every connected tag
    ./watch_ranges.py --secs 30       stop after 30 s and print the summary
"""
import argparse, collections, statistics, sys, threading, time
import serial
from dwm_ports import dwm_ports

try:
    from localize import RNG_RE, NODE_NAMES, ANCHOR_NAMES, _flag_names
except Exception:                                    # standalone fallback
    import re
    RNG_RE = re.compile(
        r"\$RNG,(\d+),(\d+),0x([0-9A-Fa-f]{4}),0x([0-9A-Fa-f]{4}),(\d+),"
        r"(-?\d+),(\d+),(-?[\d.]+),(-?[\d.]+),0x([0-9A-Fa-f]+),(\d+)")
    NODE_NAMES, ANCHOR_NAMES = {}, {}
    _flag_names = lambda f: []

WIN = 400                       # samples kept per pair for mean / sd
LOCK = threading.Lock()
PAIRS = collections.OrderedDict()   # (tag, anchor) -> deque of (t, mm, rxp)
LOST = collections.Counter()
FLAGS = collections.Counter()


def reader(dev, name, stop):
    buf = ""
    try:
        s = serial.Serial(dev, 115200, timeout=0.3)
    except Exception as e:
        print(f"  {name}: {e}", file=sys.stderr)
        return
    while not stop.is_set():
        buf += s.read(4096).decode(errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            m = RNG_RE.search(line)
            if not m:
                continue
            (_sfn, _fr, _tag, anch, slot, d_mm, tof_ps,
             rxp, _fpp, flags, _us) = m.groups()
            flags = int(flags, 16)
            label = ANCHOR_NAMES.get(anch.upper(), anch.upper())
            key = (name, label)
            with LOCK:
                for f in _flag_names(flags):
                    FLAGS[f] += 1
                if flags & 0x04:                     # RESP_LOST: no distance
                    LOST[key] += 1
                    PAIRS.setdefault(key, collections.deque(maxlen=WIN))
                else:
                    PAIRS.setdefault(key, collections.deque(maxlen=WIN)).append(
                        (time.time(), int(d_mm), float(rxp)))
    s.close()


def draw(t0):
    with LOCK:
        rows, now = [], time.time()
        for (tag, anch), dq in PAIRS.items():
            n = len(dq)
            recent = [x for x in dq if now - x[0] < 3.0]
            hz = len(recent) / 3.0 if recent else 0.0
            mm = [x[1] for x in dq]
            rx = [x[2] for x in dq]
            rows.append((
                tag, anch, n, LOST[(tag, anch)], hz,
                statistics.mean(mm) / 1000.0 if mm else float("nan"),
                statistics.stdev(mm) / 10.0 if len(mm) > 1 else float("nan"),
                statistics.mean(rx) if rx else float("nan")))
        flags = dict(FLAGS)

    print("\033[H\033[J", end="")
    print(f"  UWB DS-TWR live ranges          {now - t0:6.1f} s elapsed")
    print(f"  {'tag':6s} {'anchor':7s} {'n':>7s} {'lost':>6s} {'Hz':>7s} "
          f"{'mean m':>9s} {'sd cm':>8s} {'rssi dBm':>9s}")
    print("  " + "-" * 64)
    if not rows:
        print("  waiting for $RNG lines ...")
    for tag, anch, n, lost, hz, mean, sd, rssi in rows:
        print(f"  {tag:6s} {anch:7s} {n:>7d} {lost:>6d} {hz:>7.1f} "
              f"{mean:>9.3f} {sd:>8.2f} {rssi:>9.1f}")
    if flags:
        print("\n  flags seen: " + ", ".join(f"{k}={v}" for k, v in
                                             sorted(flags.items())))
    print("\n  sd is the spread of one pair's own samples, not accuracy;"
          "\n  absolute distance needs the antenna-delay calibration (sect 13 step 1)."
          "\n  Ctrl-C to stop.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=0.0,
                    help="stop after this long (0 = run until Ctrl-C)")
    a = ap.parse_args()

    ports = [p for p in dwm_ports()
             if not NODE_NAMES.get(p.serial_number, "").startswith(("A", "ANC"))]
    if not ports:
        raise SystemExit("no tag boards connected")

    stop = threading.Event()
    for p in ports:
        name = NODE_NAMES.get(p.serial_number, p.serial_number[-4:])
        print(f"  reading {name} on {p.device}")
        threading.Thread(target=reader, args=(p.device, name, stop),
                         daemon=True).start()
    time.sleep(1.0)

    t0 = time.time()
    try:
        while not a.secs or time.time() - t0 < a.secs:
            draw(t0)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        time.sleep(0.4)
        draw(t0)


if __name__ == "__main__":
    main()
