#!/usr/bin/env python3
"""Characterise a capture: update rates, distances, RSSI, flags.

    ./analyse_capture.py ~/Documents/uwb_capture_20260904_1710
    ./analyse_capture.py <dir> --json out.json     figures for the report

Timebase is the firmware's own t_us, not the host's arrival time. The host
reads the port in ~1.3 kB batches every 0.3 s, so fifty records land with
near-identical host timestamps: measured that way every pair shows a 0 ms
median gap and a 300 ms tail, which is the host's read cadence and says
nothing about the radio. t_us is stamped on the tag at emit time
(role_initiator.c:181), before any UART buffering, and is the only clock in
the record that reflects when the ranging exchange actually happened.

Distances here measure PRECISION, not accuracy. Every pair carries an unknown
constant offset because ant_delay is Decawave's generic 16436 rather than a
per-board calibration (section 13 step 1).
"""
import argparse
import collections
import glob
import json
import os
import statistics
import sys

FLAGS = ((0x01, "ss_fallback"), (0x02, "nlos"), (0x04, "resp_lost"),
         (0x08, "report_lost"), (0x10, "desync"), (0x20, "hash_mismatch"))
ANCH = {f"{0x0101 + i:04X}": f"ANC{i + 1}" for i in range(8)}
WRAP = 1 << 32          # t_us is u32 microseconds: wraps every 71.6 minutes
SETTLE_S = 120.0        # pairs visibly settle over the first minute or two


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def unwrap(us):
    """u32 microsecond stamps -> monotonic seconds.

    A board up for more than 71.6 minutes wraps mid-capture; without this the
    single negative jump becomes a -4295 s gap and poisons every rate figure.
    """
    out, add, prev = [], 0, None
    for v in us:
        if prev is not None and v < prev - WRAP // 2:
            add += WRAP
        prev = v
        out.append((v + add) / 1e6)
    return out


def step_cm(ts, ds):
    """Difference between the two halves' medians, in cm.

    A linear slope is the wrong model for a channel that changes once: fitting
    a line through a step reports steady movement that never happened. Halving
    the window and differencing the medians says how far the pair moved
    without assuming it moved gradually, and medians keep a handful of
    multipath outliers from carrying the answer.
    """
    if len(ts) < 60:
        return float("nan")
    mid = ts[0] + (ts[-1] - ts[0]) / 2.0
    a = [d for t, d in zip(ts, ds) if t < mid]
    b = [d for t, d in zip(ts, ds) if t >= mid]
    if len(a) < 20 or len(b) < 20:
        return float("nan")
    return (statistics.median(b) - statistics.median(a)) * 100.0


def slope_mm_per_min(ts, ds):
    """Least-squares drift in mm/min. A slope in a static setup is the clocks
    or the temperature moving, not the geometry -- which is the whole reason
    for capturing ten minutes rather than twenty seconds."""
    n = len(ts)
    if n < 30:
        return float("nan")
    t0 = ts[0]
    x = [(t - t0) / 60.0 for t in ts]
    mx, my = sum(x) / n, sum(ds) / n
    den = sum((xi - mx) ** 2 for xi in x)
    if den <= 0:
        return float("nan")
    num = sum((xi - mx) * (di - my) for xi, di in zip(x, ds))
    return (num / den) * 1000.0


def load(d):
    """Read every JSONL into per-pair columns, plus per-tag round bookkeeping."""
    # us_all holds every record's timestamp, us only those carrying a
    # distance. Timing must use us_all: a resp_lost record means the round
    # happened and the slot fired on time, it just produced no distance.
    # Measuring cadence on us alone turns each lost Response into a phantom
    # 2x gap, so the same event gets counted once as a lost Response and
    # again as a skipped round.
    per = collections.defaultdict(lambda: {
        "us_all": [], "us": [], "d": [], "rssi": [], "fpp": [], "sfn": [],
        "host": [], "flags": collections.Counter(), "lost": 0})
    # per tag: sfn -> was that round desynced, and how many records it produced
    rounds = collections.defaultdict(dict)
    meta = {}
    for f in sorted(glob.glob(os.path.join(d, "*.meta.json"))):
        m = json.load(open(f))
        meta[m["tag"]] = m
    for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        tag = os.path.basename(f).split(".")[0]
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rd = rounds[tag].setdefault(r["sfn"], [0, False])
            rd[0] += 1
            rd[1] = bool(r["flags"] & 0x10)

            p = per[(tag, ANCH.get(r["anchor_id"], r["anchor_id"]))]
            for bit, nm in FLAGS:
                if r["flags"] & bit:
                    p["flags"][nm] += 1
            p["sfn"].append(r["sfn"])
            p["us_all"].append(r["t_us"])
            if r["flags"] & 0x04:            # RESP_LOST: no distance in it
                p["lost"] += 1
                continue
            p["us"].append(r["t_us"])
            p["host"].append(r["host_time"])
            p["d"].append(r["range_m"])
            p["rssi"].append(r["rssi"])
            p["fpp"].append(r["first_path_dbm"])
    return per, rounds, meta


def summarise(per, rounds):
    """Per-pair figures. Yield is measured against the *tag's* round span, not
    the pair's own: a pair whose own first and last record bracket a missed
    round would otherwise report 100% while having dropped it."""
    tagspan = {}
    for tag, rs in rounds.items():
        ss = sorted(rs)
        if len(ss) > 1:
            step = statistics.median([ss[i + 1] - ss[i] for i in range(len(ss) - 1)])
            tagspan[tag] = (ss[0], ss[-1], step or 1)

    rows = []
    for k in sorted(per):
        tag, anch = k
        p = per[k]
        d = p["d"]
        if not d:
            rows.append({"tag": tag, "anchor": anch, "n": 0})
            continue
        ta = unwrap(p["us_all"])                 # cadence: every round
        t = unwrap(p["us"])                      # distance series
        gaps = [(ta[i + 1] - ta[i]) * 1000.0 for i in range(len(ta) - 1)]
        span = ta[-1] - ta[0] if len(ta) > 1 else 0.0
        nominal = statistics.median(gaps) if gaps else float("nan")
        # sfn advances once per superframe; a tag ranges every rate_div-th one,
        # so the round step is measured rather than assumed
        ss = sorted(set(p["sfn"]))
        lo, hi, step = tagspan.get(tag, (ss[0], ss[-1], 1))
        expected = (hi - lo) / step + 1
        # Only trim when there is enough capture left to describe: a 30 s run
        # has no steady state to speak of, and returning nan for every pair
        # would strip the column that matters most from short checks.
        t_cut = (t[0] + SETTLE_S) if (t and (t[-1] - t[0]) > 3.0 * SETTLE_S) \
            else (t[0] if t else 0.0)
        tst = [x for x in t if x >= t_cut]
        dst = [dd for x, dd in zip(t, d) if x >= t_cut]
        rows.append({
            "tag": tag, "anchor": anch, "n": len(d), "n_all": len(ta),
            "hz": (len(ta) - 1) / span if span > 0 else float("nan"),
            "gap_p50_ms": nominal,
            "gap_p05_ms": pct(gaps, 5),
            "gap_p95_ms": pct(gaps, 95),
            "gap_max_ms": max(gaps) if gaps else float("nan"),
            "jitter_ms": (pct(gaps, 95) - pct(gaps, 5)) if gaps else float("nan"),
            "stalls": sum(1 for g in gaps if g > 1.5 * nominal) if gaps else 0,
            "mean_m": statistics.mean(d),
            "sd_cm": statistics.stdev(d) * 100 if len(d) > 1 else float("nan"),
            "min_m": min(d), "max_m": max(d),
            "p95_cm": (pct(d, 95) - pct(d, 5)) * 100,
            "drift_mm_min": slope_mm_per_min(t, d),
            "step_cm": step_cm(t, d),
            # Steady state excludes the settling transient several pairs show
            # over the first minute or two, so the precision figure describes
            # the system as it runs rather than as it starts.
            "n_steady": len(dst),
            "mean_steady_m": statistics.mean(dst) if dst else float("nan"),
            "sd_steady_cm": (statistics.stdev(dst) * 100
                             if len(dst) > 1 else float("nan")),
            "step_steady_cm": step_cm(tst, dst),
            "drift_steady_mm_min": slope_mm_per_min(tst, dst),
            "rssi": statistics.mean(p["rssi"]),
            "rssi_sd": statistics.stdev(p["rssi"]) if len(p["rssi"]) > 1 else float("nan"),
            "fpp": statistics.mean(p["fpp"]),
            "sep_db": statistics.mean(p["rssi"]) - statistics.mean(p["fpp"]),
            "resp_lost": p["lost"],
            "sfn_span": (ss[-1] - ss[0]) if len(ss) > 1 else 0,
            "rounds_expected": int(round(expected)),
            "yield_pct": 100.0 * len(ta) / max(1.0, expected),
            "span_s": span,
            "flags": dict(p["flags"]),
        })
    return rows


def round_stats(rounds):
    """Per-tag view of whole ranging rounds: completeness and sync state.

    desync is a property of the round, not of one anchor, so it belongs here
    rather than in the per-pair table where it is duplicated eight times.
    """
    out = {}
    for tag, rs in rounds.items():
        ks = sorted(rs)
        counts = [rs[k][0] for k in ks]
        dz = [rs[k][1] for k in ks]
        runs, cur = [], 0
        for flag in dz:
            if flag:
                cur += 1
            elif cur:
                runs.append(cur)
                cur = 0
        if cur:
            runs.append(cur)
        full = max(set(counts), key=counts.count) if counts else 0
        out[tag] = {
            "rounds": len(ks),
            "full_size": full,
            "complete": sum(1 for c in counts if c >= full),
            "partial": sum(1 for c in counts if c < full),
            "desync_rounds": sum(dz),
            "desync_pct": 100.0 * sum(dz) / max(1, len(dz)),
            "locked_rounds": len(dz) - sum(dz),
            "desync_runs": len(runs),
            "desync_run_max": max(runs) if runs else 0,
            "desync_run_med": statistics.median(runs) if runs else 0,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--json")
    a = ap.parse_args()

    d = os.path.expanduser(a.capture)
    per, rounds, meta = load(d)
    if not per:
        sys.exit(f"no records found in {d}")
    rows = summarise(per, rounds)
    rst = round_stats(rounds)

    print(f"\n  capture: {d}")
    for tag, m in sorted(meta.items()):
        print(f"    {tag}: {m.get('records')} records in "
              f"{m.get('actual_secs')}s, decoder {m.get('decoder')}")

    print(f"\n  {'tag':5s} {'anch':5s} {'n':>6s} {'Hz':>6s} {'jit':>5s} "
          f"{'st':>3s} {'yld%':>6s} {'mean m':>8s} {'sd cm':>6s} "
          f"{'sd_st':>6s} {'step':>7s} {'st_stp':>7s} {'rssi':>6s} "
          f"{'fp':>6s} {'sep':>5s}")
    print("  " + "-" * 106)
    for r in rows:
        if not r["n"]:
            print(f"  {r['tag']:5s} {r['anchor']:5s} {'0':>6s}   -- no ranges")
            continue
        print(f"  {r['tag']:5s} {r['anchor']:5s} {r['n']:>6d} {r['hz']:>6.3f} "
              f"{r['jitter_ms']:>5.2f} {r['stalls']:>3d} {r['yield_pct']:>6.2f} "
              f"{r['mean_m']:>8.3f} {r['sd_cm']:>6.2f} "
              f"{r['sd_steady_cm']:>6.2f} {r['step_cm']:>7.1f} "
              f"{r['step_steady_cm']:>7.1f} "
              f"{r['rssi']:>6.1f} {r['fpp']:>6.1f} {r['sep_db']:>5.1f}")

    print("\n  rounds per tag")
    for tag, s in sorted(rst.items()):
        print(f"    {tag}: {s['rounds']} rounds, {s['complete']} complete "
              f"({s['full_size']} records), {s['partial']} partial | "
              f"desync {s['desync_rounds']} ({s['desync_pct']:.1f}%) in "
              f"{s['desync_runs']} runs, longest {s['desync_run_max']}")

    # tag-to-tag agreement: the tags sit close together, so a consistent gap is
    # a per-board offset rather than geometry
    print("\n  tag-to-tag agreement per anchor (TAG1 - TAG2, metres)")
    by = {(r["tag"], r["anchor"]): r for r in rows if r["n"]}
    for anch in sorted({r["anchor"] for r in rows}):
        a1, a2 = by.get(("TAG1", anch)), by.get(("TAG2", anch))
        if a1 and a2:
            print(f"    {anch}: {a1['mean_m']:.3f} vs {a2['mean_m']:.3f}  "
                  f"delta {a1['mean_m']-a2['mean_m']:+.3f}")

    agg = collections.Counter()
    for r in rows:
        for k, v in r["flags"].items():
            agg[k] += v
    total = sum(r["n"] + r["resp_lost"] for r in rows)
    print(f"\n  flags across {total} records: " +
          ", ".join(f"{k}={v} ({100.0*v/max(1,total):.2f}%)"
                    for k, v in sorted(agg.items())))

    if a.json:
        with open(a.json, "w") as f:
            json.dump({"capture": d, "meta": meta, "rows": rows,
                       "rounds": rst, "flags": dict(agg), "total": total},
                      f, indent=1)
        print(f"\n  figures written to {a.json}")


if __name__ == "__main__":
    main()
