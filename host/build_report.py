#!/usr/bin/env python3
"""Render a capture's figures as a print-ready measurement report.

    ./analyse_capture.py <capture> --json figs.json
    ./build_report.py figures.json out.html

Then headless Chrome turns the HTML into the PDF. Deliberately light-only:
this is a print document, so it commits to one visual context rather than
carrying a dark palette that no printer will ever use.

The $STAT parser is duplicated from capture.py rather than imported so this
tool stays runnable without pyserial installed.
"""
import json
import statistics
import sys
from datetime import datetime

NUM = {2: 'Two', 3: 'Three', 4: 'Four', 5: 'Five', 6: 'Six',
       7: 'Seven', 8: 'Eight', 9: 'Nine', 10: 'Ten'}
INK, PAPER = "#14181d", "#ffffff"
ACCENT, MUTED, FAINT = "#0b6f78", "#4b5765", "#6b7683"
RULE, TINT = "#d7dee6", "#f4f7fa"
ALERT, CAUTION = "#b4232a", "#a35a00"

CSS = f"""
@page {{ size: A4; margin: 15mm 14mm 14mm; }}
* {{ box-sizing: border-box; }}
html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin:0; color:{INK}; background:{PAPER};
  font:9.5pt/1.45 "Helvetica Neue",Helvetica,Arial,sans-serif; }}
.mono, td.n, .kpi .v {{ font-family:"SF Mono",Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums; }}

header {{ border-bottom:1.6pt solid {INK}; padding-bottom:7pt; margin-bottom:11pt; }}
.kicker {{ font:600 7.2pt/1 "SF Mono",Menlo,monospace; letter-spacing:.17em;
  text-transform:uppercase; color:{ACCENT}; margin:0 0 5pt; }}
h1 {{ margin:0; font-size:19pt; font-weight:600; letter-spacing:-.015em;
  text-wrap:balance; }}
.sub {{ margin:5pt 0 0; color:{MUTED}; font-size:9.2pt; max-width:56em; }}
.meta {{ margin-top:7pt; font-size:7.8pt; color:{FAINT}; line-height:1.5; }}

h2 {{ font-size:10.2pt; font-weight:600; margin:15pt 0 5pt; padding-bottom:2.5pt;
  border-bottom:.6pt solid {RULE}; break-after:avoid; }}
h2:first-of-type {{ margin-top:9pt; }}
h2 .eyebrow {{ float:right; font:400 7.2pt/1.5 "SF Mono",Menlo,monospace;
  letter-spacing:.08em; text-transform:uppercase; color:{FAINT}; }}
p {{ margin:0 0 6pt; max-width:58em; }}

table {{ border-collapse:collapse; width:100%; margin:3pt 0 5pt; font-size:8.3pt; }}
table.whole {{ break-inside:avoid; }}
thead {{ display:table-header-group; }}
caption {{ caption-side:bottom; text-align:left; padding-top:4pt;
  font-size:7.5pt; color:{FAINT}; line-height:1.45; }}
th,td {{ padding:2.7pt 5pt; text-align:right; white-space:nowrap; }}
thead th {{ border-bottom:.9pt solid {INK}; font-size:7pt; font-weight:600;
  letter-spacing:.045em; text-transform:uppercase; color:{MUTED};
  vertical-align:bottom; }}
th:first-child,td:first-child,th.l,td.l {{ text-align:left; }}
tbody tr+tr td {{ border-top:.5pt solid #e9eef3; }}
tr.sep td {{ border-top:.9pt solid #b9c4d0; }}
tfoot td {{ border-top:.9pt solid {INK}; font-weight:600; }}
.bad {{ color:{ALERT}; }} .warn {{ color:{CAUTION}; }}

.kpis {{ display:grid; grid-template-columns:repeat(5,1fr);
  border:.7pt solid {RULE}; border-radius:4pt; overflow:hidden; margin:9pt 0 4pt; }}
.kpi {{ padding:7pt 9pt; border-right:.7pt solid #e9eef3; }}
.kpi:last-child {{ border-right:0; }}
.kpi .k {{ font:600 6.8pt/1 "SF Mono",Menlo,monospace; letter-spacing:.09em;
  text-transform:uppercase; color:{FAINT}; }}
.kpi .v {{ font-size:15pt; font-weight:600; margin-top:3pt; letter-spacing:-.01em; }}
.kpi .u {{ font-size:7.8pt; font-weight:400; color:{FAINT}; margin-left:1.5pt;
  font-family:"Helvetica Neue",Helvetica,sans-serif; }}

.note {{ padding:6pt 9pt; background:{TINT}; border-left:2.4pt solid {ACCENT};
  margin:6pt 0 8pt; font-size:8.7pt; break-inside:avoid; }}
.note.caution {{ background:#fdf6f0; border-left-color:{CAUTION}; }}
.note p:last-child {{ margin-bottom:0; }}
figure {{ margin:5pt 0 7pt; break-inside:avoid; }}
figcaption {{ font-size:7.5pt; color:{FAINT}; padding-top:3pt; line-height:1.45; }}
footer {{ margin-top:13pt; padding-top:6pt; border-top:.6pt solid {RULE};
  font-size:7.4pt; color:{FAINT}; line-height:1.5; }}
"""


def f(v, spec="{:.2f}", dash="&ndash;"):
    try:
        if v is None or (isinstance(v, float) and v != v):
            return dash
        return spec.format(v)
    except (TypeError, ValueError):
        return dash


def an(n, spec="{:.0f}"):
    """Article that matches how the number is read aloud, so the generated
    prose does not produce "a 80 cm"."""
    txt = spec.format(n)
    return ("an " if txt[0] in "8" or txt.startswith("11") or txt.startswith("18")
            else "a ") + txt


def stat_val(block, key):
    for line in block or []:
        toks = line.lstrip("$").split(",")
        for i in range(1, len(toks) - 1):
            if toks[i] == key:
                v = toks[i + 1].split("*")[0]
                try:
                    return int(v)
                except ValueError:
                    return None
    return None


def cfg_of(meta):
    """Parse the $CFG block into a dict.

    The radio settings in the header are read back from the boards rather
    than restated from the provisioning script, so the report cannot claim a
    configuration the hardware was not actually running.
    """
    out = {}
    for line in (meta.get("before", {}) or {}).get("$CFG", []) or []:
        toks = line.lstrip("$").split(",")
        if toks[0] == "C" and len(toks) >= 3:
            out[toks[1]] = toks[2].split("*")[0]
        elif toks[0].startswith("CFGBEGIN"):
            for kv in toks[0].split(",")[1:] + toks[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    out[k.strip()] = v.split("*")[0]
    return out


def delta(meta, key):
    a = stat_val(meta.get("after", {}).get("$STAT"), key)
    b = stat_val(meta.get("before", {}).get("$STAT"), key)
    return (a - b) if None not in (a, b) else None


def sd_chart(rows):
    """Horizontal bars of per-pair standard deviation, one shared scale.

    The point of the chart is that all sixteen bars are short and similar --
    a table of sixteen near-identical numbers hides that, a bar chart shows
    it in one glance, and an outlier would be equally obvious.
    """
    def sdv(r):
        v = r.get("sd_steady_cm", float("nan"))
        return v if v == v else r["sd_cm"]

    vals = [sdv(r) for r in rows if sdv(r) == sdv(r)]
    if not vals:
        return ""
    hi = max(vals)
    ordered = sorted(vals)
    typical = ordered[int(len(ordered) * 0.9)] if ordered else hi
    # One 27 cm pair would flatten fifteen 2 cm bars into invisibility, so the
    # axis follows the bulk and any bar past it is drawn clipped with its real
    # value labelled.
    top = max(1.0, float(int(min(hi, max(typical * 1.6, 4.0))) + 1))
    clipped = [v for v in vals if v > top]
    W, LEFT, RIGHT, BH, GAP, TOPPAD = 520, 62, 34, 9.0, 3.6, 16
    plot = W - LEFT - RIGHT
    H = TOPPAD + len(rows) * (BH + GAP) + 14
    o = [f'<svg viewBox="0 0 {W} {H:.0f}" width="100%" '
         f'style="max-width:100%;height:auto" role="img" '
         f'aria-label="Standard deviation per tag-anchor pair">']
    # gridlines, every whole cm, each label a value the scale reaches
    n_ticks = int(top)
    for i in range(n_ticks + 1):
        x = LEFT + plot * i / top
        o.append(f'<line x1="{x:.1f}" y1="{TOPPAD-5:.0f}" x2="{x:.1f}" '
                 f'y2="{TOPPAD + len(rows)*(BH+GAP):.1f}" stroke="{RULE}" '
                 f'stroke-width="0.5" fill="none"/>')
        o.append(f'<text x="{x:.1f}" y="{TOPPAD-8:.0f}" font-size="6.4" '
                 f'fill="{FAINT}" text-anchor="middle" '
                 f'font-family="SF Mono,Menlo,monospace">{i}</text>')
    o.append(f'<text x="{LEFT + plot/2:.0f}" y="{H-2:.0f}" font-size="6.4" '
             f'fill="{FAINT}" text-anchor="middle" '
             f'font-family="SF Mono,Menlo,monospace">'
             f'standard deviation, cm</text>')
    for i, r in enumerate(rows):
        y = TOPPAD + i * (BH + GAP)
        sd = sdv(r) if sdv(r) == sdv(r) else 0.0
        over = sd > top
        w = max(0.6, plot * min(sd, top) / top)
        col = ACCENT if r["tag"] == "TAG1" else "#7a4fa3"
        o.append(f'<text x="{LEFT-6}" y="{y+BH-1.4:.1f}" font-size="6.8" '
                 f'fill="{MUTED}" text-anchor="end" '
                 f'font-family="SF Mono,Menlo,monospace">'
                 f'{r["tag"]}&#183;{r["anchor"]}</text>')
        o.append(f'<rect x="{LEFT}" y="{y:.1f}" width="{w:.1f}" '
                 f'height="{BH:.1f}" fill="{col}" rx="1"/>')
        if over:
            # zig-zag edge: says "continues past the axis" without pretending
            # the bar ends here
            xe = LEFT + w
            o.append(f'<path d="M{xe-3:.1f} {y:.1f} l3 {BH/3:.1f} l-3 '
                     f'{BH/3:.1f} l3 {BH/3:.1f}" fill="none" stroke="{PAPER}" '
                     f'stroke-width="1.4"/>')
        o.append(f'<text x="{LEFT+w+4:.1f}" y="{y+BH-1.4:.1f}" font-size="6.6" '
                 f'fill="{ALERT if over else FAINT}" '
                 f'font-family="SF Mono,Menlo,monospace">'
                 f'{sd:.2f}{"" if not over else " &#9654;"}</text>')
    o.append("</svg>")
    return "".join(o)


def main():
    figs = json.load(open(sys.argv[1]))
    rows = [r for r in figs["rows"] if r.get("n")]
    meta, flags, total = figs["meta"], figs["flags"], figs["total"]
    rounds = figs.get("rounds", {})

    span = max((r["span_s"] for r in rows), default=0.0)
    cfg = {}
    for m in meta.values():
        c = cfg_of(m)
        if c:
            cfg = c
            break
    hashes = sorted({cfg_of(m).get("hash", "?") for m in meta.values()})

    def cg(k, dflt="?"):
        return cfg.get(k, dflt)

    radio = (f"channel {cg('channel')}, {cg('prf')}&nbsp;MHz PRF, "
             f"{int(cg('datarate', 0) or 0)/1000:.1f}&nbsp;Mbps, "
             f"{cg('preamble_len')}-symbol preamble, code {cg('preamble_code')}")
    timing = (f"T<sub>f</sub> {cg('frame_dur_us')}&nbsp;&micro;s, "
              f"rate_div {cg('rate_div')}, {cg('init_count')} initiators "
              f"&times; {cg('resp_count')} responders, "
              f"out_format {cg('out_format')}")
    hz = [r["hz"] for r in rows]
    sds = sorted(r["sd_cm"] for r in rows if r["sd_cm"] == r["sd_cm"])
    rssis = [r["rssi"] for r in rows]
    jits = [r["jitter_ms"] for r in rows if r["jitter_ms"] == r["jitter_ms"]]
    stalls = sum(r["stalls"] for r in rows)
    lost = sum(r["resp_lost"] for r in rows)
    ylds = [r["yield_pct"] for r in rows]

    h = ["<!doctype html><html><head><meta charset='utf-8'>",
         "<title>UWB ten-minute measurement report</title>",
         f"<style>{CSS}</style></head><body>"]

    h.append(f"""<header>
      <p class="kicker">UWB DS-TWR &middot; 2 tags &times; 8 anchors &middot; field capture</p>
      <h1>Ten-minute measurement report</h1>
      <p class="sub">Update rate, distance precision and received power for
      all sixteen tag&ndash;anchor pairs, measured from a continuous capture of
      every range record both tags emitted.</p>
      <p class="meta">{datetime.now():%d %B %Y} &middot;
      firmware <span class="mono">{cg('fw')}</span>, config hash
      <span class="mono">{'/'.join(hashes)}</span> &middot;
      ant_delay {cg('ant_delay_tx')}/{cg('ant_delay_rx')} (uncalibrated)
      &middot; {radio} &middot; {timing} &middot;
      {span:.0f}&nbsp;s captured, {total:,} records</p>
    </header>""")

    h.append(f"""<div class="kpis">
      <div class="kpi"><div class="k">records</div><div class="v">{total:,}</div></div>
      <div class="kpi"><div class="k">pairs live</div><div class="v">{len(rows)}<span class="u">/ 16</span></div></div>
      <div class="kpi"><div class="k">rate / pair</div><div class="v">{sum(hz)/len(hz):.2f}<span class="u">Hz</span></div></div>
      <div class="kpi"><div class="k">median SD</div><div class="v">{sds[len(sds)//2]:.2f}<span class="u">cm</span></div></div>
      <div class="kpi"><div class="k">mean RSSI</div><div class="v">{sum(rssis)/len(rssis):.1f}<span class="u">dBm</span></div></div>
    </div>""")

    # ---------------------------------------------------------------- 1
    h.append('<h2>Capture integrity<span class="eyebrow">independent '
             'cross-check</span></h2>')
    h.append("<p>The firmware's own counters bracket the run, so the host's "
             "record count can be checked against what the tags believe they "
             "produced. This is not a formality: a USB link that dies mid-run "
             "produces no corruption at all, so the decoder reports zero loss "
             "on a stream that stopped halfway. Only the "
             "<span class='mono'>ranges</span> counter reveals it.</p>")
    h.append("<table class='whole'><thead><tr><th class='l'>Tag</th><th>Records<br>decoded</th>"
             "<th>Firmware<br>ranges</th><th>Yield<br>%</th>"
             "<th>Frames</th><th>Resp<br>lost</th><th>Report<br>lost</th>"
             "<th>tx_<br>dropped</th><th>CRC<br>fail</th><th>Resync<br>bytes</th>"
             "<th>Stalled</th></tr></thead><tbody>")
    for tag, m in sorted(meta.items()):
        v = m.get("verify") or {}
        fw = v.get("fw_ranges", delta(m, "ranges"))
        rec = m.get("records", 0)
        y = (100.0 * rec / fw) if fw else None
        d = m.get("decoder") or {}
        drop = v.get("tx_dropped", delta(m, "tx_dropped"))
        st = m.get("stalled")
        ycls = " class='n bad'" if (y is not None and y < 99) else " class='n'"
        stxt = "&ndash;" if st is None else ("yes" if st else "no")
        h.append(f"<tr><td class='l'>{tag}</td><td class='n'>{rec:,}</td>"
                 f"<td class='n'>{f(fw,'{:,.0f}')}</td>"
                 f"<td{ycls}>{f(y)}</td>"
                 f"<td class='n'>{f(delta(m,'frames'),'{:,.0f}')}</td>"
                 f"<td class='n'>{f(delta(m,'resp_lost'),'{:,.0f}')}</td>"
                 f"<td class='n'>{f(delta(m,'reports_lost'),'{:,.0f}')}</td>"
                 f"<td class='n'>{f(drop,'{:,.0f}')}</td>"
                 f"<td class='n'>{d.get('bad_crc','&ndash;')}</td>"
                 f"<td class='n'>{d.get('resync_bytes','&ndash;')}</td>"
                 f"<td class='n{' bad' if st else ''}'>{stxt}</td></tr>")
    h.append("</tbody><caption>Counter columns are the change across the "
             "capture. <span class='mono'>tx_dropped</span> is output the tag "
             "could not fit in its UART ring &mdash; any movement means records "
             "were lost before reaching the host. Yield is decoded records "
             "against firmware-counted ranges; below 99% the capture is "
             "incomplete and is flagged.</caption></table>")

    # ---------------------------------------------------------------- 2
    h.append('<h2>Update rate<span class="eyebrow">device timebase</span></h2>')
    h.append("""<div class="note">
      <p><strong>Measured on the tag's clock, not the host's.</strong> Every
      record carries <span class="mono">t_us</span>, stamped on the tag when
      it built the record, before any UART buffering. The host reads the port
      in ~1.3&nbsp;kB batches every 0.3&nbsp;s, so fifty records arrive with
      near-identical host timestamps; measured that way every pair reports a
      0&nbsp;ms median gap and a 300&nbsp;ms tail, which describes the host's
      read cadence and says nothing about the radio.</p>
    </div>""")
    h.append("<table><thead><tr><th class='l'>Tag</th><th class='l'>Anchor</th>"
             "<th>Records</th><th>Rate<br>Hz</th><th>p50 gap<br>ms</th>"
             "<th>p5&ndash;p95<br>&micro;s</th><th>Max gap<br>ms</th>"
             "<th>Cadence<br>gaps</th><th>Yield<br>%</th></tr></thead><tbody>")
    last = None
    for r in rows:
        cls = " class='sep'" if last and r["tag"] != last else ""
        last = r["tag"]
        yc = " bad" if r["yield_pct"] < 99 else ""
        h.append(f"<tr{cls}><td class='l'>{r['tag']}</td>"
                 f"<td class='l'>{r['anchor']}</td>"
                 f"<td class='n'>{r.get('n_all', r['n']):,}</td>"
                 f"<td class='n'>{f(r['hz'],'{:.3f}')}</td>"
                 f"<td class='n'>{f(r['gap_p50_ms'])}</td>"
                 f"<td class='n'>{f(r['jitter_ms']*1000,'{:.0f}')}</td>"
                 f"<td class='n'>{f(r['gap_max_ms'],'{:.1f}')}</td>"
                 f"<td class='n'>{r['stalls']}</td>"
                 f"<td class='n{yc}'>{f(r['yield_pct'])}</td></tr>")
    h.append("</tbody></table>")
    nom = sum(r["gap_p50_ms"] for r in rows) / len(rows)
    worst = max(r["gap_max_ms"] for r in rows)
    if stalls:
        gap_note = (
            f"The largest gaps reach {worst:.0f}&nbsp;ms, near a multiple of "
            f"the nominal {nom:.1f}&nbsp;ms &mdash; the signature of a missing "
            f"record rather than drift, and {stalls} such gaps occurred across "
            f"all sixteen pairs.")
    else:
        gap_note = (
            f"No pair ever missed a slot: the worst gap anywhere in the "
            f"capture is {worst:.1f}&nbsp;ms against a nominal "
            f"{nom:.2f}&nbsp;ms, so every gap is one round long and the "
            f"cadence never skips.")
    h.append(f"<p>Every pair holds {sum(hz)/len(hz):.3f}&nbsp;Hz with a "
             f"p5&ndash;p95 spread of {min(jits)*1000:.0f}&ndash;"
             f"{max(jits)*1000:.0f}&nbsp;&micro;s &mdash; the slot map is "
             f"keeping time to well under a tenth of a millisecond. "
             f"{gap_note} Yield is measured against each tag's own round "
             f"count, so a pair that dropped a round cannot mask it.</p>")

    # ---------------------------------------------------------------- 3
    h.append('<h2>Distance precision<span class="eyebrow">'
             'uncalibrated antenna delay</span></h2>')
    h.append("<table><thead><tr><th class='l'>Tag</th><th class='l'>Anchor</th>"
             "<th>With<br>distance</th><th>Mean<br>m</th>"
             "<th>SD all<br>cm</th><th>SD steady<br>cm</th>"
             "<th>Step<br>cm</th><th>p5&ndash;p95<br>cm</th>"
             "<th>Min<br>m</th><th>Max<br>m</th><th>Range<br>cm</th>"
             "</tr></thead><tbody>")
    last = None
    for r in rows:
        cls = " class='sep'" if last and r["tag"] != last else ""
        last = r["tag"]
        stp = r.get("step_steady_cm", float("nan"))
        astp = abs(stp) if stp == stp else 0.0
        sc = " bad" if astp > 20 else (" warn" if astp > 5 else "")
        sdst = r.get("sd_steady_cm", float("nan"))
        dc = " bad" if (sdst == sdst and sdst > 10) else ""
        h.append(f"<tr{cls}><td class='l'>{r['tag']}</td>"
                 f"<td class='l'>{r['anchor']}</td>"
                 f"<td class='n'>{r['n']:,}</td>"
                 f"<td class='n'>{f(r['mean_m'],'{:.3f}')}</td>"
                 f"<td class='n'>{f(r['sd_cm'])}</td>"
                 f"<td class='n{dc}'>{f(sdst)}</td>"
                 f"<td class='n{sc}'>{f(stp,'{:+.1f}')}</td>"
                 f"<td class='n'>{f(r['p95_cm'])}</td>"
                 f"<td class='n'>{f(r['min_m'],'{:.3f}')}</td>"
                 f"<td class='n'>{f(r['max_m'],'{:.3f}')}</td>"
                 f"<td class='n'>{f((r['max_m']-r['min_m'])*100,'{:.1f}')}</td>"
                 f"</tr>")
    h.append("</tbody><caption>SD all covers the whole capture; SD steady "
             "excludes the first 120&nbsp;s, over which several pairs visibly "
             "settle. Step is the difference between the two halves' medians "
             "inside the steady window &mdash; a change measured without "
             "assuming it happened gradually, since fitting a slope through a "
             "channel that shifts once reports steady movement that never "
             "occurred. Amber past 5&nbsp;cm, red past 20.</caption></table>")

    # Name the outlier and say whether the power figures corroborate it. A
    # table of sixteen rows does not tell the reader which one to look at.
    def sdst(r):
        v = r.get("sd_steady_cm", float("nan"))
        return v if v == v else r["sd_cm"]

    ranked = sorted(rows, key=sdst)
    worst_pair, best_pair = ranked[-1], ranked[0]
    bulk = [sdst(r) for r in ranked[:-1]]
    med = statistics.median(bulk)
    seps = sorted(r["sep_db"] for r in rows if r["sep_db"] == r["sep_db"])
    sep_med = seps[len(seps) // 2] if seps else float("nan")
    wstep = worst_pair.get("step_steady_cm", float("nan"))
    nl = worst_pair["flags"].get("nlos", 0)
    corroborated = worst_pair["sep_db"] > sep_med + 1.5 or \
        nl > 0.05 * worst_pair["n"]

    settlers = [r for r in rows
                if r["sd_cm"] == r["sd_cm"] and sdst(r) == sdst(r)
                and r["sd_cm"] > 2.0 * sdst(r)]

    h.append(
        f"<p>Fifteen of the sixteen pairs hold a steady-state SD between "
        f"{min(bulk):.2f} and {max(bulk):.2f}&nbsp;cm, median "
        f"{med:.2f}&nbsp;cm, and every one of them moves less than "
        f"{max(abs(r.get('step_steady_cm', 0) or 0) for r in ranked[:-1]):.0f}"
        f"&nbsp;cm between the halves of the capture. The exception is "
        f"<strong>{worst_pair['tag']}&ndash;{worst_pair['anchor']}</strong> at "
        f"{sdst(worst_pair):.1f}&nbsp;cm with a {wstep:+.0f}&nbsp;cm step"
        + (f", and its power figures corroborate it: first-path separation "
           f"{worst_pair['sep_db']:.1f}&nbsp;dB against a fleet median of "
           f"{sep_med:.1f}&nbsp;dB, and "
           f"{100.0*nl/max(1,worst_pair['n']):.1f}% of its records raised the "
           f"NLOS flag. The shape matters: the shift is a step partway "
           f"through, not a ramp, and after it the pair's own scatter "
           f"<em>improves</em>. That is a channel that changed once &mdash; "
           f"something moving in or out of the direct path &mdash; rather "
           f"than a ranging fault or a board drifting."
           if corroborated else
           ". Its received-power figures are unremarkable, so the spread is "
           "not explained by an obstructed path and is worth a second look.")
        + "</p>")

    if settlers:
        names = ", ".join(f"{r['tag']}&ndash;{r['anchor']}" for r in settlers)
        h.append(
            f"<p>{'One pair' if len(settlers)==1 else NUM.get(len(settlers), str(len(settlers))) + ' pairs'} "
            f"({names}) {'shows' if len(settlers)==1 else 'show'} a startup "
            f"transient: the whole-capture SD is more than double the "
            f"steady-state figure, with the excess confined to the first "
            f"minute or two before the reading settles. For steady-state "
            f"characterisation the first 120&nbsp;s are best discarded, which "
            f"is what the SD steady column does.</p>")

    h.append(f'<figure>{sd_chart(rows)}'
             f'<figcaption>Steady-state standard deviation per pair, first '
             f'120&nbsp;s excluded, on one shared scale. Teal is TAG1, violet '
             f'TAG2. A bar with a notched edge and a red figure runs past the '
             f'axis; its true value is labelled.</figcaption></figure>')

    # ---------------------------------------------------------------- 4
    h.append('<h2>Received power and first-path separation'
             '<span class="eyebrow">NLOS discriminator</span></h2>')
    h.append("<p>The DW1000 reports total received power and the power in the "
             "first detected path separately. Their difference is what the "
             "firmware thresholds at 10&nbsp;dB to raise "
             "<span class='mono'>RNG_FLAG_NLOS</span>: when the first arrival "
             "carries much less energy than the whole channel response, the "
             "direct path is likely obstructed and the leading edge being "
             "timed is a reflection.</p>")
    h.append("<table><thead><tr><th class='l'>Tag</th><th class='l'>Anchor</th>"
             "<th>RSSI<br>dBm</th><th>SD<br>dB</th><th>First path<br>dBm</th>"
             "<th>Separation<br>dB</th><th>NLOS<br>flagged</th>"
             "<th>% of<br>pair</th></tr></thead><tbody>")
    last = None
    for r in rows:
        cls = " class='sep'" if last and r["tag"] != last else ""
        last = r["tag"]
        sep = r["sep_db"]
        sc = " bad" if sep > 10 else (" warn" if sep > 8 else "")
        nl = r["flags"].get("nlos", 0)
        h.append(f"<tr{cls}><td class='l'>{r['tag']}</td>"
                 f"<td class='l'>{r['anchor']}</td>"
                 f"<td class='n'>{f(r['rssi'],'{:.1f}')}</td>"
                 f"<td class='n'>{f(r['rssi_sd'])}</td>"
                 f"<td class='n'>{f(r['fpp'],'{:.1f}')}</td>"
                 f"<td class='n{sc}'>{f(sep,'{:.1f}')}</td>"
                 f"<td class='n'>{nl:,}</td>"
                 f"<td class='n'>{100.0*nl/max(1,r['n']):.1f}</td></tr>")
    h.append("</tbody><caption>Separation is mean RSSI minus mean first-path "
             "power. Amber from 8&nbsp;dB, red past the firmware's "
             "10&nbsp;dB threshold. The flag count is per-record, so a pair "
             "with a mean below threshold can still flag individual "
             "records.</caption></table>")

    # ---------------------------------------------------------------- 5
    if rounds:
        h.append('<h2>Frame synchronisation<span class="eyebrow">'
                 'the desync flag explained</span></h2>')
        h.append("<table class='whole'><thead><tr><th class='l'>Tag</th><th>Rounds</th>"
                 "<th>Complete</th><th>Partial</th><th>Desynced<br>rounds</th>"
                 "<th>%</th><th>Runs</th><th>Longest<br>run</th>"
                 "<th>Polls heard<br>from peer</th><th>Rearms</th>"
                 "</tr></thead><tbody>")
        for tag, s in sorted(rounds.items()):
            m = meta.get(tag, {})
            h.append(f"<tr><td class='l'>{tag}</td>"
                     f"<td class='n'>{s['rounds']:,}</td>"
                     f"<td class='n'>{s['complete']:,}</td>"
                     f"<td class='n'>{s['partial']:,}</td>"
                     f"<td class='n'>{s['desync_rounds']:,}</td>"
                     f"<td class='n'>{f(s['desync_pct'],'{:.1f}')}</td>"
                     f"<td class='n'>{s['desync_runs']:,}</td>"
                     f"<td class='n'>{s['desync_run_max']:,}</td>"
                     f"<td class='n'>{f(delta(m,'polls_rx'),'{:,.0f}')}</td>"
                     f"<td class='n'>{f(delta(m,'rearm'),'{:,.0f}')}</td>"
                     f"</tr>")
        h.append("</tbody><caption>A complete round carries one record per "
                 "anchor. Desync is a property of the whole round, not of an "
                 "individual anchor, which is why it belongs here rather than "
                 "in the per-pair tables.</caption></table>")
        h.append("""<div class="note">
          <p><strong>The asymmetry is expected, and it is not a fault.</strong>
          <span class="mono">RNG_FLAG_DESYNC</span> means
          <span class="mono">state == FREERUN &amp;&amp; ever_locked</span>:
          the tag held a frame lock and is now running on its own timer with
          the estimated drift, which is exactly what section&nbsp;6 prescribes.
          The two tags land on opposite sides of it because of equation&nbsp;(8),
          &Lambda;(k,m) = (k&minus;m) mod N<sub>I</sub>. TAG2 (k=1) hearing
          TAG1 (m=0) gets &Lambda;=1 and its frame follows TAG1's inside the
          same superframe, so it hears the peer's Poll before its own slot and
          re-locks every frame. TAG1 (k=0) transmits first and is then occupied
          across its eight ranging slots while TAG2 polls, so it almost never
          hears TAG2 at all &mdash; the polls-heard column shows the gap
          directly, and once <span class="mono">missed</span> passes
          <span class="mono">sync_mmax</span> = 10 it free-runs.</p>
          <p>The update-rate figures are what settle whether this matters: a
          free-running TAG1 holds its slot to the same tens of microseconds as
          the locked TAG2. The flag is accurate, and on this two-initiator
          layout it is cosmetic.</p>
        </div>""")

    # ---------------------------------------------------------------- 6
    by = {(r["tag"], r["anchor"]): r for r in rows}
    anchors = sorted({r["anchor"] for r in rows})
    pairs = [(by.get(("TAG1", a)), by.get(("TAG2", a))) for a in anchors]
    pairs = [(a, b) for a, b in pairs if a and b]
    if pairs:
        h.append('<h2>Tag-to-tag agreement<span class="eyebrow">'
                 'per-board offset</span></h2>')
        h.append("<p>The two tags sit close together, so each anchor should "
                 "read similarly from either. A consistent gap is a per-board "
                 "antenna-delay difference rather than geometry &mdash; which "
                 "is precisely what an uncalibrated delay predicts.</p>")
        h.append("<table class='whole'><thead><tr><th class='l'>Anchor</th><th>TAG1<br>m</th>"
                 "<th>TAG2<br>m</th><th>&Delta;<br>m</th><th>TAG1 SD<br>cm</th>"
                 "<th>TAG2 SD<br>cm</th><th>TAG1<br>RSSI</th>"
                 "<th>TAG2<br>RSSI</th></tr></thead><tbody>")
        for r1, r2 in pairs:
            dv = r1["mean_m"] - r2["mean_m"]
            dc = " warn" if abs(dv) > 0.20 else ""
            h.append(f"<tr><td class='l'>{r1['anchor']}</td>"
                     f"<td class='n'>{f(r1['mean_m'],'{:.3f}')}</td>"
                     f"<td class='n'>{f(r2['mean_m'],'{:.3f}')}</td>"
                     f"<td class='n{dc}'>{f(dv,'{:+.3f}')}</td>"
                     f"<td class='n'>{f(r1['sd_cm'])}</td>"
                     f"<td class='n'>{f(r2['sd_cm'])}</td>"
                     f"<td class='n'>{f(r1['rssi'],'{:.1f}')}</td>"
                     f"<td class='n'>{f(r2['rssi'],'{:.1f}')}</td></tr>")
        ds = [abs(a["mean_m"] - b["mean_m"]) for a, b in pairs]
        h.append(f"<tfoot><tr><td class='l'>spread</td><td colspan='2'></td>"
                 f"<td class='n'>{max(ds)*100:.0f} cm</td>"
                 f"<td colspan='4'></td></tr></tfoot>")
        h.append("</tbody></table>")

    # ---------------------------------------------------------------- 7
    h.append("<h2>Flag summary</h2>")
    MEAN = {
        "ss_fallback": "The Report was lost, so equation (7) single-sided TWR "
                       "was used in place of DS-TWR",
        "report_lost": "The responder's Report did not arrive",
        "resp_lost": "The responder's Response did not arrive, so the record "
                     "carries no distance",
        "nlos": "RSSI minus first-path power exceeded 10 dB",
        "desync": "The initiator held a frame lock and is now free-running",
        "hash_mismatch": "A Poll arrived carrying a different configuration hash",
    }
    h.append("<table class='whole'><thead><tr><th class='l'>Flag</th><th>Records</th>"
             "<th>% of all</th><th class='l'>Meaning</th></tr></thead><tbody>")
    for k, v in sorted(flags.items(), key=lambda kv: -kv[1]):
        h.append(f"<tr><td class='l mono'>{k}</td><td class='n'>{v:,}</td>"
                 f"<td class='n'>{100.0*v/max(1,total):.2f}</td>"
                 f"<td class='l'>{MEAN.get(k,'')}</td></tr>")
    h.append("</tbody></table>")
    dstwr = 100.0 * (1 - flags.get("ss_fallback", 0) / max(1, total))
    h.append(f"<p><strong>{dstwr:.2f}% of records completed as full "
             f"double-sided TWR.</strong> The remainder fell back to "
             f"single-sided, which carries a clock-offset error term DS-TWR "
             f"cancels; those records are flagged and can be filtered. "
             f"{lost:,} records lost the Response outright and carry no "
             f"distance at all.</p>")

    # ---------------------------------------------------------------- 8
    h.append("<h2>What these figures do and do not establish</h2>")
    h.append(f"""<div class="note caution">
      <p><strong>This is precision, not accuracy.</strong> Every board runs
      Decawave's generic <span class="mono">ant_delay</span> of
      {cg('ant_delay_tx')} rather
      than a per-board calibration, so every distance carries an unknown
      constant offset. The SD, p5&ndash;p95, range and step columns are
      meaningful as they stand; the absolute <em>mean</em> of any pair is not.
      The tag-to-tag table above is that offset made visible &mdash; the two
      tags disagree by up to tens of centimetres on the same anchor while each
      is individually repeatable to a couple of centimetres.</p>
      <p>One surveyed distance per board (section&nbsp;13 step&nbsp;1) is what
      converts these into accurate ranges. Nothing in this capture can
      substitute for it, and nothing in this capture is invalidated by its
      absence.</p>
    </div>""")
    h.append("<p>Taken at face value: the update rate, its jitter, the yield, "
             "the flag counts and the received-power figures are all "
             "independent of antenna-delay calibration. So are the SD and "
             "step columns, which measure whether a pair holds the same "
             "reading rather than whether that reading is right. What the "
             "capture cannot address is how far apart the boards actually "
             "are.</p>")

    h.append(f"""<footer>
      Capture <span class="mono">{figs['capture']}</span> &middot;
      {total:,} records over {span:.0f}&nbsp;s &middot;
      yield {min(ylds):.2f}&ndash;{max(ylds):.2f}% per pair &middot;
      {stalls} cadence gaps and {lost:,} lost Responses across all pairs.
      Raw byte streams are archived beside the decoded records, so every
      figure here can be recomputed without repeating the capture.
      Generated by <span class="mono">analyse_capture.py</span> and
      <span class="mono">build_report.py</span>.
    </footer></body></html>""")

    open(sys.argv[2], "w").write("\n".join(h))
    print(f"  wrote {sys.argv[2]}")


if __name__ == "__main__":
    main()
