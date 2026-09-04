#!/usr/bin/env python3
"""Re-decode a capture's raw .bin and check it against the .jsonl.

    ./verify_capture.py ~/Documents/uwb_capture_20260904_1710

The .bin is the archive of record; the .jsonl is a derived convenience. If
they disagree, the analysis is describing something the radio never sent.
Also re-checks decoded counts against the firmware's own ranges counter,
which is the only signal that catches a cleanly-dead USB link -- such a link
truncates the stream without corrupting a single byte, so the decoder itself
reports nothing wrong.
"""
import glob
import json
import os
import sys

import dwm_bin


def stat_val(block, key):
    for line in block or []:
        toks = line.lstrip("$").split(",")
        for i in range(1, len(toks) - 1):
            if toks[i] == key:
                try:
                    return int(toks[i + 1].split("*")[0])
                except ValueError:
                    return None
    return None


def fw_ranges(meta):
    """Firmware-counted ranges across the capture.

    Prefers the verify block capture.py now writes, but falls back to the
    raw $STAT bracket so captures taken before that block existed still get
    checked rather than silently passing.
    """
    v = (meta.get("verify") or {}).get("fw_ranges")
    if v is not None:
        return v
    a = stat_val(meta.get("after", {}).get("$STAT"), "ranges")
    b = stat_val(meta.get("before", {}).get("$STAT"), "ranges")
    return (a - b) if None not in (a, b) else None


def main():
    d = os.path.expanduser(sys.argv[1])
    ok = True
    for binpath in sorted(glob.glob(os.path.join(d, "*.bin"))):
        tag = os.path.basename(binpath)[:-4]
        jl = os.path.join(d, tag + ".jsonl")
        mp = os.path.join(d, tag + ".meta.json")

        dec = dwm_bin.Decoder()
        n_bin = 0
        with open(binpath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                n_bin += len(dec.feed(chunk))

        n_json = sum(1 for _ in open(jl)) if os.path.exists(jl) else -1
        meta = json.load(open(mp)) if os.path.exists(mp) else {}
        loss = dec.losses()

        fw = fw_ranges(meta)
        agree = (n_bin == n_json)
        yield_pct = (100.0 * n_json / fw) if fw else None
        # Proportional, not absolute: a couple of dozen resync bytes in a
        # multi-megabyte stream is one record's worth of noise, and treating
        # that the same as a stream that died at 70% would make the flag
        # useless for spotting the one that matters.
        nbytes = os.path.getsize(binpath)
        loss_pct = 100.0 * dec.lost() / max(1, nbytes)
        good = agree and loss_pct <= 0.01 and (
            yield_pct is None or yield_pct >= 99.0)
        ok = ok and good

        print(f"  {tag}: bin re-decode {n_bin:,}  jsonl {n_json:,}  "
              f"{'agree' if agree else 'MISMATCH'}")
        print(f"        decoder {loss}  "
              f"({dec.lost()} B lost of {nbytes:,} = {loss_pct:.4f}%)")
        if fw:
            print(f"        firmware counted {fw:,} ranges -> "
                  f"{yield_pct:.2f}% captured")
        if meta.get("stalled"):
            print(f"        stream stalled at {meta.get('stall_at_s')}s")
        print(f"        {'OK' if good else 'SUSPECT'}")
    print(f"  capture {'verified' if ok else 'SUSPECT -- see above'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
