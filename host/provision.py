#!/usr/bin/env python3
"""Re-apply the whole network's configuration to every attached board.

A board's configuration lives only in its own flash, set over serial, so a
reflash or an erase loses it. This script is the record of what the working
setup is, and re-applies it.

    ./provision.py --dry-run     show what would be sent, touch nothing
    ./provision.py               provision every attached board in BOARDS
    ./provision.py --verify      report each board's live state, change nothing
    ./provision.py --forget      clear the recorded provisioning state

Boards do not need to be attached together. The values applied are identical
every run, so provisioning them ONE AT A TIME reaches the same result --
frame_dur_us included, since it is derived from $TABLEB, which computes it
purely from the hashed parameters. Each board's hash is recorded in
.provision_state.json, so agreement is judged across runs rather than within
one, and the record survives coming back a day later.

Three things it has to get right, all learned the hard way:

  * Ordering. cfg_validate() checks equation (14) on every $SET, so raising a
    slot pitch while frame_dur_us is still narrow is rejected. The frame is
    opened wide first, then closed to the figure $TABLEB says is required.

  * Baud. A freshly flashed board boots at the section 8.1 default of 921600,
    which the J-Link OB VCOM cannot carry cleanly board->host -- characters
    drop. Host->board is fine, so the drop to 115200 is issued blind at 921600
    and everything after it is verified at 115200.

  * The hash. resp_base_us, resp_pitch_us, report_base_us, report_pitch_us,
    frame_dur_us, resp_count and init_count are all in cfg_hash(), so every
    node must carry identical values or its Polls are rejected by the others.
    The script records each board's hash and reports agreement across the
    whole declared fleet, not just the boards attached to this run.
"""
import argparse
import hashlib
import json
import os
import sys
import time

import serial

from dwm_ports import dwm_ports, normalise_sn

# --------------------------------------------------------------------------- #
# The network. Edit this, not the code.
# --------------------------------------------------------------------------- #
# J-Link serial -> (label, role, index). The index is frame_index for a tag
# and slot_index for an anchor. Anchor addresses are pinned rather than left
# to the part-id derivation, so the tags' anchor lists resolve and $RNG
# reports a real anchor_id.
BOARDS = {
    "760131590": ("TAG1", "INIT", 0),
    "760131633": ("TAG2", "INIT", 1),
    "760131573": ("ANC1", "RESP", 0),
    "760131584": ("ANC2", "RESP", 1),
    "760131626": ("ANC3", "RESP", 2),
    "760217925": ("ANC4", "RESP", 3),
    "760217915": ("ANC5", "RESP", 4),
    "760217943": ("ANC6", "RESP", 5),
    "760217956": ("ANC7", "RESP", 6),
    "760217975": ("ANC8", "RESP", 7),
}

# The network is sized for its FINAL size, not for however many boards happen
# to be built yet. resp_count and init_count are in cfg_hash(), so raising
# them later means re-provisioning every single board -- sizing for the end
# state up front avoids that entirely. A slot with no anchor in it yet simply
# reports RESP_LOST on every frame, which costs frame time but breaks nothing.
#
# The price is paid in rate: T_f grows about 1226 us per anchor slot, whether
# or not that slot is occupied.
N_TAGS = 2
N_ANCHORS = 8

# Hashed: identical on every node, or the network splits.
#
# These are not the specification's defaults. Its own values (T_r0 400,
# dr 300, T_p0 400) proved too aggressive for real DW1000 SPI at 8 MHz: slot 0
# could not arm its Response at all, and the Report ran 82 us over budget,
# which suppressed DS-TWR entirely in favour of the single-sided fallback.
# Every figure here came from $MEASURE and $TABLEB on these boards -- the
# section 13 step 5 procedure -- with the measured costs being 313 us to arm a
# Response and 682 us to arm a Report.
TIMING = {
    "resp_base_us":    600,      # T_r0
    "resp_pitch_us":   615,      # dr
    "report_base_us": 1000,      # T_p0
    "report_pitch_us": 611,      # dp
    "guard_us":        200,      # T_g
}

# Not hashed: per-board, so these may differ between nodes.
LOCAL = {
    "proc_us":        320,       # measured responder arm cost, for $TABLEB
    "isr_us":           0,       # SYS_STATUS is polled; no interrupt latency
    "margin_us":      100,
    "proc_final_us":  700,
    # The TX prep margin, and deliberately generous. $MEASURE reports ~140 us
    # here, but that statistic is only recorded after radio_tx_at() SUCCEEDS,
    # so it is survivor-biased and does not bound the worst case; it also has
    # to absorb the worst-case return from the in-flight radio_rx(), which
    # nothing measures. At 200 us every Poll was late.
    "proc_poll_us":  1000,
    "rate_div":         2,
    # BIN, not CSV. A text record is ~57 bytes, so 8 anchors at 21 Hz costs
    # 9.7 kB/s against the 11.5 kB/s that 115200 8N1 carries -- 84% sustained
    # duty, at which the TX ring never fully drains between frame bursts. The
    # tail of the last record of each frame was being dropped along with its
    # CRLF, leaving the next frame's first record with nowhere to begin, so
    # slots 7 and 0 vanished from every view and ANC1 looked dead. A binary
    # record is 25 bytes: 4.3 kB/s, and tx_dropped measured 0 over 25 s
    # afterwards. Decoded by host/dwm_bin.py.
    "out_format":   "BIN",
    "uart_baud":   115200,
    "autostart":        1,
    # Decawave's canonical DWM1001 figure, and NOT CFG_ANT_DELAY_UNCAL, so
    # radio.c programs it directly to both registers rather than treating it
    # as "uncalibrated". Replace per board once section 13 step 1 is done --
    # this generic value is why pairs at one distance still read 0.81-1.00 m.
    "ant_delay_tx": 16436,
    "ant_delay_rx": 16436,
}

FRAME_WIDE = 20000     # opened this wide while the pitches are set
BAUDS = (115200, 921600)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".provision_state.json")


def fingerprint(n_tags, n_anch):
    """Identify the settings a board was provisioned against.

    Hashes alone cannot separate a board that genuinely disagrees from one
    provisioned correctly against an OLDER TIMING table -- both read as a
    mismatch, but only the second tells you which boards need redoing. This
    makes staleness distinguishable from disagreement.
    """
    src = json.dumps({"timing": TIMING, "n_tags": n_tags, "n_anch": n_anch},
                     sort_keys=True)
    return hashlib.sha256(src.encode()).hexdigest()[:12]


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)          # atomic, so an interrupted run cannot
                                    # leave a half-written record


# --------------------------------------------------------------------------- #
def send(s, cmd, wait=0.45):
    s.reset_input_buffer()
    s.write((cmd + "\r\n").encode())
    s.flush()
    time.sleep(wait)
    return s.read(65536).decode("utf-8", "replace").strip()


def first_line(txt):
    return txt.splitlines()[0].strip() if txt.strip() else "(no reply)"


def alive(reply):
    """Is a board listening at this baud?

    Any $-framed response proves it is. Requiring $OK specifically was wrong
    twice over: at 921600 the reply itself arrives corrupted over this VCOM,
    and a board whose line buffer holds junk answers a perfectly good command
    with $ERR,1 -- alive, just annoyed.
    """
    return "$OK" in reply or "$ERR" in reply


def clear_line(s):
    """Flush whatever partial line the board is holding.

    Probing at the wrong baud leaves the board's parser mid-line, so the next
    clean command is appended to that garbage and rejected as unknown. A bare
    CRLF terminates it and costs nothing when the buffer is already empty.
    """
    s.reset_input_buffer()
    s.write(b"\r\n")
    s.flush()
    time.sleep(0.25)
    s.reset_input_buffer()


def open_talking(dev):
    """Return a port at a baud the board actually answers on.

    A freshly flashed or never-configured board sits at the section 8.1
    default of 921600, which garbles its replies over this VCOM, so the drop
    to 115200 is sent blind and then confirmed.
    """
    for baud in BAUDS:
        s = serial.Serial(dev, baud, timeout=0.3)
        time.sleep(0.35)
        clear_line(s)
        if alive(send(s, "$RANGE,STOP", 0.7)):
            if baud == BAUDS[0]:
                return s, baud
            # Talking, but at a baud we cannot read reliably: move it.
            send(s, f"$SET,uart_baud,{BAUDS[0]}", 0.5)
            s.close()
            time.sleep(0.6)
            s = serial.Serial(dev, BAUDS[0], timeout=0.3)
            time.sleep(0.35)
            clear_line(s)
            if alive(send(s, "$RANGE,STOP", 0.7)):
                return s, BAUDS[0]
        s.close()
    return None, None


def read_cfg(s, keys):
    out = {}
    for line in send(s, "$CFG", 1.5).splitlines():
        line = line.strip()
        if not line.startswith("$C,"):
            continue
        parts = line.split("*")[0].split(",")
        if len(parts) >= 3 and parts[1] in keys:
            out[parts[1]] = parts[2]
    return out


def read_hash(s):
    for line in send(s, "$CFG", 1.5).splitlines():
        if "hash=" in line:
            return line.split("hash=")[1].split("*")[0]
    return None


def table_b(s):
    """eq 12/13/14 status, and the frame duration equation (14) requires."""
    st, req = {}, None
    for line in send(s, "$TABLEB", 1.8).splitlines():
        line = line.strip()
        if not line.startswith("$TB,"):
            continue
        f = line.split("*")[0].split(",")
        if f[1].startswith("eq"):
            st[f[1]] = f[2]
        for i, k in enumerate(f):
            if k == "frame_dur_us_required":
                req = int(f[i + 1])
    return st, req


# --------------------------------------------------------------------------- #
def out_of_network(role, index, n_tags, n_anch):
    """True when this board's index falls outside the declared network.

    cfg_validate() rejects slot_index >= resp_count, so shrinking N_ANCHORS
    leaves higher-numbered anchors unprovisionable. Saying so plainly beats
    letting them fail on a $SET with an error that looks like a real fault.
    """
    limit = n_tags if role == "INIT" else n_anch
    return index >= limit


def provision(dev, sn, label, role, index, n_tags, n_anch, dry):  # noqa: C901
    print(f"\n=== {label}  (SN {sn}, {dev}) ===")
    if out_of_network(role, index, n_tags, n_anch):
        kind = "frame" if role == "INIT" else "slot"
        print(f"   SKIPPED: {kind} {index} is outside the declared network "
              f"({n_tags} tags x {n_anch} anchors) -- raise "
              f"{'N_TAGS' if role == 'INIT' else 'N_ANCHORS'} to include it")
        return None
    if dry:
        print("   would set: role", role, "index", index,
              "| resp_count", n_anch, "init_count", n_tags)
        print("   would set:", ", ".join(f"{k}={v}" for k, v in TIMING.items()))
        return None

    s, baud = open_talking(dev)
    if s is None:
        print("   FAILED: no reply at 115200 or 921600")
        return None
    if baud != BAUDS[0]:
        print(f"   reached it at {baud}, moved to {BAUDS[0]}")

    try:
        send(s, "$RANGE,STOP", 0.5)

        # Identity and network size first; they gate the range checks below.
        # The frame is opened wide FIRST. cfg_validate() checks equation (14)
        # on every $SET, so a larger resp_count or slot pitch is rejected while
        # frame_dur_us still holds a value sized for a smaller network -- which
        # silently leaves the old resp_count in place.
        steps = [("frame_dur_us", FRAME_WIDE),
                 ("role", role),
                 ("init_count", n_tags),
                 ("resp_count", n_anch),
                 ("frame_index" if role == "INIT" else "slot_index", index)]
        steps += list(TIMING.items())
        steps += list(LOCAL.items())

        bad = []
        for k, v in steps:
            r = send(s, f"$SET,{k},{v}")
            if "ERR" in r:
                bad.append(f"{k}={v} -> {first_line(r)}")

        # Close the frame to exactly what equation (14) needs.
        st, req = table_b(s)
        if req:
            r = send(s, f"$SET,frame_dur_us,{req}")
            if "ERR" in r:
                bad.append(f"frame_dur_us={req} -> {first_line(r)}")
            st, _ = table_b(s)

        # Anchor list, so anchor_id resolves in $RNG output.
        for i in range(n_anch):
            r = send(s, f"$ANCHOR,{i},0x{0x0101 + i:04X},{i},0,0,0,0,1")
            if "ERR" in r:
                bad.append(f"anchor {i} -> {first_line(r)}")

        # An anchor's own address must match the list the tags carry.
        if role == "RESP":
            r = send(s, f"$SET,node_id,0x{0x0101 + index:04X}")
            if "ERR" in r:
                bad.append(f"node_id -> {first_line(r)}")

        saved = send(s, "$SAVE")
        h = read_hash(s)
        send(s, "$STAT,CLEAR", 0.3)
        send(s, "$RANGE,START", 0.3)

        recorded = load_state()
        recorded[sn] = {"label": label, "hash": h, "frame_dur_us": req,
                        "fingerprint": fingerprint(n_tags, n_anch),
                        "when": time.strftime("%Y-%m-%d %H:%M")}
        save_state(recorded)

        print(f"   frame_dur_us = {req}   hash = {h}   {first_line(saved)}")
        eqs = " ".join(f"{k}:{v}" for k, v in sorted(st.items()))
        print(f"   {eqs}")
        for b in bad:
            print(f"   ERR {b}")
        if any(v != "ok" for v in st.values()):
            print("   NOTE an equation is not ok -- re-run $MEASURE; the "
                  "processing costs in LOCAL may no longer fit this N_R")
        return h
    finally:
        s.close()


def verify_only(dev, sn, label):
    s, baud = open_talking(dev)
    if s is None:
        print(f"  {label:5s} SN {sn}: no reply")
        return None
    try:
        c = read_cfg(s, {"role", "node_id", "frame_index", "slot_index",
                         "init_count", "resp_count", "frame_dur_us",
                         "resp_pitch_us", "report_base_us", "rate_div",
                         "ant_delay_tx"})
        h = read_hash(s)
        st, _ = table_b(s)
        send(s, "$RANGE,START", 0.3)
        eqs = ",".join(f"{k.replace('eq','')}:{v}" for k, v in sorted(st.items()))
        print(f"  {label:5s} SN {sn}  hash={h}  role={c.get('role')} "
              f"idx={c.get('frame_index') or c.get('slot_index')} "
              f"N_I={c.get('init_count')} N_R={c.get('resp_count')} "
              f"T_f={c.get('frame_dur_us')} dr={c.get('resp_pitch_us')} "
              f"T_p0={c.get('report_base_us')} D={c.get('rate_div')} "
              f"ant={c.get('ant_delay_tx')} eq[{eqs}]")
        return h
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent, change nothing")
    ap.add_argument("--verify", action="store_true",
                    help="report current state and hashes, change nothing")
    ap.add_argument("--forget", action="store_true",
                    help="clear the recorded provisioning state and exit")
    a = ap.parse_args()

    if a.forget:
        try:
            os.remove(STATE)
            print(f"  cleared {STATE}")
        except FileNotFoundError:
            print("  nothing recorded")
        return

    ports = dwm_ports()
    if not ports:
        sys.exit("no boards connected")

    known, unknown = [], []
    for p in ports:
        sn = normalise_sn(p.serial_number)
        (known if sn in BOARDS else unknown).append((p.device, sn))

    n_tags, n_anch = N_TAGS, N_ANCHORS
    built = sum(1 for v in BOARDS.values() if v[1] == "RESP")
    print(f"  network sized for {n_tags} tags x {n_anch} anchors"
          f"  ({built} anchor board(s) known)")
    print(f"  attached and known: {len(known)} of {len(BOARDS)}")
    for dev, sn in unknown:
        print(f"  SKIPPING unknown board SN {sn} ({dev}) -- add it to BOARDS")

    if a.verify:
        for dev, sn in known:
            try:
                verify_only(dev, sn, BOARDS[sn][0])
            except (serial.SerialException, OSError) as e:
                print(f"  {BOARDS[sn][0]:5s} SN {sn}: went away ({e})")
        report_fleet(n_tags, n_anch)
        return
    else:
        for dev, sn in known:
            label, role, index = BOARDS[sn]
            try:
                provision(dev, sn, label, role, index,
                          n_tags, n_anch, a.dry_run)
            except (serial.SerialException, OSError) as e:
                # A board unplugged or reset mid-run used to abort the whole
                # pass, discarding the work already done on every other board.
                # Report it and carry on; the state file records what did
                # succeed, so a re-run only has to cover the rest.
                print(f"   FAILED: {label} went away mid-provision ({e})")
                print("   -- its configuration may be half-applied; re-run "
                      "with it attached")
    if a.dry_run:
        report_fleet(n_tags, n_anch)
        return

    report_fleet(n_tags, n_anch)


def report_fleet(n_tags, n_anch):
    """Report every declared board, from the state file.

    Provisioning one board at a time is the normal case here -- not every
    board can be attached at once -- so agreement has to be judged across
    runs rather than within one. The state file is what makes that possible,
    and it survives coming back a day later.
    """
    st = load_state()
    want = fingerprint(n_tags, n_anch)
    print()
    print("  board  hash      provisioned        state")
    print("  " + "-" * 52)

    by_hash = {}
    stale = never = 0
    n_net = sum(1 for v in BOARDS.values()
                if not out_of_network(v[1], v[2], n_tags, n_anch))
    for sn, (label, _role, _idx) in sorted(BOARDS.items(),
                                           key=lambda kv: kv[1][0]):
        role, idx = BOARDS[sn][1], BOARDS[sn][2]
        if out_of_network(role, idx, n_tags, n_anch):
            print(f"  {label:5s}  --        --                 "
                  f"not in this network")
            continue
        rec = st.get(sn)
        if not rec:
            print(f"  {label:5s}  --        --                 never provisioned")
            never += 1
            continue
        if rec.get("fingerprint") != want:
            note = "STALE (older settings)"
            stale += 1
        else:
            note = "current"
            by_hash.setdefault(rec["hash"], []).append(label)
        print(f"  {label:5s}  {rec['hash']:8s}  {rec['when']:17s}  {note}")

    print()
    if len(by_hash) == 1 and not stale and not never:
        h, labels = next(iter(by_hash.items()))
        print(f"  fleet agrees: all {len(labels)} boards on {h}")
    else:
        if by_hash:
            best = max(by_hash.items(), key=lambda kv: len(kv[1]))
            print(f"  {len(best[1])} of {n_net} agree on {best[0]}"
                  f" ({', '.join(best[1])})")
            for h, labels in by_hash.items():
                if h != best[0]:
                    print(f"  DIFFERENT HASH {h}: {', '.join(labels)}"
                          f" -- these are not in the same network")
        todo = stale + never
        if todo:
            print(f"  {todo} board(s) still to provision"
                  f"{' (' + str(stale) + ' stale)' if stale else ''}")
        print("  attach each remaining board and re-run; the values applied are"
              " identical, so order does not matter")


if __name__ == "__main__":
    main()
