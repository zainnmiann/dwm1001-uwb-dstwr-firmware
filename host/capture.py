#!/usr/bin/env python3
"""Record every range record from every connected tag to disk.

    ./capture.py --secs 600 --out ~/Documents/uwb_capture_20260904_1730

Three files per tag:

  <tag>.bin        the raw byte stream, verbatim
  <tag>.jsonl      one decoded record per line
  <tag>.meta.json  $CFG and $STAT before and after, plus decoder counters

The raw stream is the real archive. If the decode or the analysis later turns
out to be wrong, the capture does not have to be repeated -- which matters for
a ten-minute run.

$STAT is taken before and after because the firmware's own counters are the
only independent check on the capture. Comparing (ranges_after - ranges_before)
against the records actually decoded is what separates "the anchor did not
answer" from "the host lost it" -- the exact confusion that made a healthy
ANC1 look dead for an hour.

That check earned its keep on the first 600 s run: both tags went silent at
the same device timestamp (556.43 s) while the firmware kept counting, and a
read() then blocked 246 s past the deadline. Nothing was corrupt, so the
decoder had nothing to report -- a dead stream looks exactly like a quiet one.
Hence two guards: a stall watchdog that gives up on a port after STALL_S of
silence, and a deadline watchdog in the main thread that cancels a read stuck
past the deadline, since a blocked read cannot time itself out.
"""
import argparse
import json
import os
import sys
import threading
import time

import serial

import dwm_bin
from dwm_ports import dwm_ports, normalise_sn

NAMES = {"760131590": "TAG1", "760131633": "TAG2",
         "760131573": "ANC1", "760131584": "ANC2", "760131626": "ANC3",
         "760217925": "ANC4", "760217915": "ANC5", "760217943": "ANC6",
         "760217956": "ANC7", "760217975": "ANC8"}

PRINT = threading.Lock()
RESULT = {}
# Live ports, so the deadline watchdog can cancel a read that has hung.
PORTS = {}
PORTS_LOCK = threading.Lock()
STALL_S = 15.0          # silence this long means the stream is gone, not idle
GRACE_S = 10.0          # deadline slack before the watchdog cancels a read


def query(dev, cmds, settle=0.4):
    """Read $CFG/$STAT without disturbing the stream for long.

    Ranging is stopped, the counters are read, and it is started again --
    a couple of seconds of interruption at each end of the capture rather than
    guessing at the board's state.
    """
    out = {}
    s = serial.Serial(dev, 115200, timeout=0.3)
    try:
        time.sleep(settle)
        s.reset_input_buffer()
        s.write(b"\r\n")
        s.flush()
        time.sleep(0.25)
        s.reset_input_buffer()
        s.write(b"$RANGE,STOP\r\n")
        s.flush()
        time.sleep(0.8)
        s.read(500000)
        for c in cmds:
            s.reset_input_buffer()
            s.write((c + "\r\n").encode())
            s.flush()
            time.sleep(1.6)
            out[c] = [l.strip() for l in
                      s.read(500000).decode("utf-8", "replace").splitlines()
                      if l.strip().startswith("$")]
        s.write(b"$RANGE,START\r\n")
        s.flush()
        time.sleep(0.4)
    finally:
        s.close()
    return out


def capture(dev, name, secs, outdir):
    binpath = os.path.join(outdir, f"{name}.bin")
    jsonpath = os.path.join(outdir, f"{name}.jsonl")
    meta = {"tag": name, "device": dev, "wanted_secs": secs}

    try:
        meta["before"] = query(dev, ["$CFG", "$STAT"])
    except Exception as e:
        meta["before_error"] = str(e)

    d = dwm_bin.Decoder()
    n = 0
    t_start = time.time()
    try:
        s = serial.Serial(dev, 115200, timeout=0.3)
    except Exception as e:
        with PRINT:
            print(f"  {name}: cannot open {dev}: {e}", file=sys.stderr)
        return
    time.sleep(0.3)
    s.reset_input_buffer()
    with PORTS_LOCK:
        PORTS[name] = s

    with open(binpath, "wb") as fb, open(jsonpath, "w") as fj:
        last_note = t_start
        last_byte = time.time()
        while time.time() - t_start < secs:
            try:
                chunk = s.read(4096)
            except Exception as e:
                meta["read_error"] = str(e)
                meta["read_error_at_s"] = round(time.time() - t_start, 1)
                break
            if not chunk:
                # A live tag never goes quiet for long: at 169 records/s the
                # gap between bursts is milliseconds. Prolonged silence is a
                # dead link, and waiting out the full run would just archive
                # the silence.
                if time.time() - last_byte > STALL_S:
                    meta["stalled"] = True
                    meta["stall_at_s"] = round(last_byte - t_start, 1)
                    with PRINT:
                        print(f"  {name}: STREAM STALLED at "
                              f"{meta['stall_at_s']}s -- giving up on this port",
                              file=sys.stderr)
                    break
                continue
            last_byte = time.time()
            fb.write(chunk)
            for r in d.feed(chunk):
                r["host_time"] = round(time.time(), 4)
                fj.write(json.dumps(r, separators=(",", ":")) + "\n")
                n += 1
            if time.time() - last_note >= 60:
                last_note = time.time()
                with PRINT:
                    print(f"  {name}: {int(time.time()-t_start):4d}s  "
                          f"{n:7d} records  "
                          f"{n/max(1e-9, time.time()-t_start):6.1f}/s",
                          file=sys.stderr)
    with PORTS_LOCK:
        PORTS.pop(name, None)
    try:
        s.close()
    except Exception:
        pass

    meta["actual_secs"] = round(time.time() - t_start, 2)
    meta.setdefault("stalled", False)
    meta["records"] = n
    meta["decoder"] = d.losses()
    meta["bin_bytes"] = os.path.getsize(binpath)
    try:
        meta["after"] = query(dev, ["$STAT"])
    except Exception as e:
        meta["after_error"] = str(e)

    with open(os.path.join(outdir, f"{name}.meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    RESULT[name] = meta
    with PRINT:
        print(f"  {name}: done -- {n} records in {meta['actual_secs']}s, "
              f"loss {d.lost()}", file=sys.stderr)


def stat_val(block, key):
    """Pull one counter out of a $STAT block. Keys are name,value pairs and
    the trailing *XX checksum has to come off the last value."""
    for line in block or []:
        toks = line.lstrip("$").split(",")
        for i in range(1, len(toks) - 1, 1):
            if toks[i] == key:
                v = toks[i + 1].split("*")[0]
                try:
                    return int(v)
                except ValueError:
                    return None
    return None


def deadline_watchdog(secs):
    """Cancel any read still blocked well past the deadline.

    pyserial's timeout does not survive the device going away mid-read: the
    call sat in the kernel for 246 s on the first run. cancel_read() is the
    only thing that unsticks it, and only another thread can call it.
    """
    end = time.time() + secs + GRACE_S
    while time.time() < end:
        time.sleep(0.5)
        with PORTS_LOCK:
            if not PORTS:
                return
    with PORTS_LOCK:
        stuck = list(PORTS.items())
    for name, port in stuck:
        with PRINT:
            print(f"  {name}: past deadline and still in read() -- cancelling",
                  file=sys.stderr)
        try:
            port.cancel_read()
        except Exception:
            pass


def verify(meta):
    """Cross-check decoded records against the firmware's own counters.

    A clean USB death produces no corruption at all, so the decoder reports
    zero loss on a stream that stopped halfway. Only the firmware's ranges
    counter reveals it.
    """
    ra = stat_val(meta.get("after", {}).get("$STAT"), "ranges")
    rb = stat_val(meta.get("before", {}).get("$STAT"), "ranges")
    da = stat_val(meta.get("after", {}).get("$STAT"), "tx_dropped")
    db = stat_val(meta.get("before", {}).get("$STAT"), "tx_dropped")
    out = {}
    if None not in (ra, rb):
        out["fw_ranges"] = ra - rb
        out["captured"] = meta.get("records", 0)
        out["yield_pct"] = round(100.0 * meta.get("records", 0) /
                                 max(1, ra - rb), 2)
    if None not in (da, db):
        out["tx_dropped"] = da - db
    meta["verify"] = out
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=600.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    outdir = os.path.expanduser(a.out)
    os.makedirs(outdir, exist_ok=True)

    tags = [(p.device, NAMES.get(normalise_sn(p.serial_number),
                                 normalise_sn(p.serial_number)[-4:]))
            for p in dwm_ports()]
    tags = [(d, n) for d, n in tags if n.startswith("TAG")]
    if not tags:
        sys.exit("no tags connected (anchors do not report ranges)")

    print(f"  capturing {a.secs:.0f}s from {len(tags)} tag(s) into {outdir}",
          file=sys.stderr)
    threads = [threading.Thread(target=capture, args=(d, n, a.secs, outdir))
               for d, n in tags]
    wd = threading.Thread(target=deadline_watchdog, args=(a.secs,), daemon=True)
    for t in threads:
        t.start()
    wd.start()
    for t in threads:
        t.join()

    summary = {"outdir": outdir, "secs": a.secs,
               "started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "tags": RESULT}
    with open(os.path.join(outdir, "capture.json"), "w") as f:
        json.dump(summary, f, indent=1)
    total = sum(m.get("records", 0) for m in RESULT.values())
    print(f"  total {total} records across {len(RESULT)} tag(s)",
          file=sys.stderr)

    ok = True
    for name, m in sorted(RESULT.items()):
        v = verify(m)
        # rewrite the meta now that verify() has filled it in
        with open(os.path.join(outdir, f"{name}.meta.json"), "w") as f:
            json.dump(m, f, indent=1)
        y = v.get("yield_pct")
        drop = v.get("tx_dropped")
        bad = (m.get("stalled") or (y is not None and y < 99.0)
               or (drop or 0) > 0)
        ok = ok and not bad
        print(f"  {name}: {v.get('captured','?')} captured vs "
              f"{v.get('fw_ranges','?')} counted by firmware "
              f"({y if y is not None else '?'}%), tx_dropped {drop}, "
              f"stalled {m.get('stalled')}  ->  "
              f"{'SUSPECT' if bad else 'OK'}", file=sys.stderr)
    print(f"  capture {'OK' if ok else 'SUSPECT -- see above'}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
