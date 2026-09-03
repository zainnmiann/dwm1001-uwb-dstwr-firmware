#!/usr/bin/env python3
"""Re-apply the whole network's configuration to every attached board.

A board's configuration lives only in its own flash, set over serial, so a
reflash or an erase loses it. This script is the record of what the working
setup is, and re-applies it.

    ./provision.py --dry-run     show what would be sent, touch nothing
    ./provision.py               provision every attached board in BOARDS
    ./provision.py --verify      only report current state and hashes

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
    The script ends by comparing the hash across all boards.
"""
import argparse
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
}

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
    # 2 tags x N anchors of CSV output overruns 115200 and truncates lines.
    # Raise the ceiling instead (out_format BIN, or out_mask 1) to run at 1.
    "rate_div":         2,
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


# --------------------------------------------------------------------------- #
def send(s, cmd, wait=0.45):
    s.reset_input_buffer()
    s.write((cmd + "\r\n").encode())
    s.flush()
    time.sleep(wait)
    return s.read(65536).decode("utf-8", "replace").strip()


def first_line(txt):
    return txt.splitlines()[0].strip() if txt.strip() else "(no reply)"


def open_talking(dev):
    """Return a port at a baud the board actually answers on.

    A freshly flashed board is at 921600 and garbles its replies over this
    VCOM, so the drop to 115200 is sent blind and then confirmed.
    """
    for baud in BAUDS:
        s = serial.Serial(dev, baud, timeout=0.3)
        time.sleep(0.35)
        if "$OK" in send(s, "$RANGE,STOP", 0.7):
            return s, baud
        if baud == 921600:                    # blind: drop it and re-open
            send(s, f"$SET,uart_baud,{BAUDS[0]}", 0.5)
            s.close()
            time.sleep(0.6)
            s = serial.Serial(dev, BAUDS[0], timeout=0.3)
            time.sleep(0.35)
            if "$OK" in send(s, "$RANGE,STOP", 0.7):
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
def provision(dev, sn, label, role, index, n_tags, n_anch, dry):
    print(f"\n=== {label}  (SN {sn}, {dev}) ===")
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
        steps = [("role", role),
                 ("init_count", n_tags),
                 ("resp_count", n_anch),
                 ("frame_index" if role == "INIT" else "slot_index", index)]
        # Frame opened wide, then the pitches, then closed to the requirement.
        steps += [("frame_dur_us", FRAME_WIDE)]
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
    a = ap.parse_args()

    ports = dwm_ports()
    if not ports:
        sys.exit("no boards connected")

    known, unknown = [], []
    for p in ports:
        sn = normalise_sn(p.serial_number)
        (known if sn in BOARDS else unknown).append((p.device, sn))

    n_tags = sum(1 for v in BOARDS.values() if v[1] == "INIT")
    n_anch = sum(1 for v in BOARDS.values() if v[1] == "RESP")
    print(f"  network as declared in BOARDS: {n_tags} tags x {n_anch} anchors")
    print(f"  attached and known: {len(known)} of {len(BOARDS)}")
    for dev, sn in unknown:
        print(f"  SKIPPING unknown board SN {sn} ({dev}) -- add it to BOARDS")

    if a.verify:
        hashes = [verify_only(dev, sn, BOARDS[sn][0]) for dev, sn in known]
    else:
        hashes = []
        for dev, sn in known:
            label, role, index = BOARDS[sn]
            hashes.append(provision(dev, sn, label, role, index,
                                    n_tags, n_anch, a.dry_run))
    if a.dry_run:
        return

    hs = {h for h in hashes if h}
    print()
    if len(hs) == 1:
        print(f"  config hash identical across {len(hs and hashes)} boards: "
              f"{hs.pop()}")
    else:
        print(f"  HASH MISMATCH {hs} -- nodes with different hashes ignore "
              f"each other's Polls")
        print("  every board must be attached and provisioned together")

    missing = len(BOARDS) - len(known)
    if missing:
        print(f"  {missing} declared board(s) were not attached and still "
              f"carry their old configuration")


if __name__ == "__main__":
    main()
