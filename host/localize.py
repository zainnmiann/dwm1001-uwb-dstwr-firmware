#!/usr/bin/env python3
"""
Live localization server for a DWM1001 RTLS.

Opens every connected tag's serial port, turns on the `lec` CSV stream, parses
the range reports and serves a live view at http://127.0.0.1:8765

Two position estimates are shown side by side:
  * ONBOARD - the tag's own on-board location engine (the POS field of `lec`)
  * LSQ   - our least-squares multilateration of the same ranges

They should agree closely. Divergence points at a bad anchor coordinate or an
outlier range, which is exactly what you want to see during commissioning.

    python3 localize.py                 # all connected tags
    python3 localize.py --port 9000
    python3 localize.py --tag-height 1.2

Ctrl-C to stop.
MQTT
----
Pass --mqtt-host to also publish every reading to an MQTT broker. This process
is then the single owner of the serial ports, so do NOT run mqtt_bridge.py at
the same time -- only one process can hold a serial port.

    python3 localize.py --mqtt-host 192.168.1.110

    uwb/ranging/<tag>/<anchor> one message per tag-anchor range
    uwb/position/<tag>         on-board fix plus this program's least-squares fix
    uwb/status/<tag>           per-tag health

The firmware exposes no true per-range RSSI or TWR timestamps -- the "rssi" field in
each ranging message is a CALCULATED estimate (free-space path-loss model
from range_m, refined by the firmware's own quality factor), not a hardware
measurement. The three TWR timestamps remain unavailable; see the
`unavailable` field in each ranging message.
"""
import argparse, json, os, re, threading, time
from collections import deque
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import numpy as np
import serial
from serial.tools import list_ports
from dwm_ports import dwm_ports

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

HERE = os.path.dirname(os.path.abspath(__file__))

# Serial number -> friendly name. Anything not listed still works, it just
# shows up under its serial number.
NODE_NAMES = {
    "000760117288": "T03",
    "000760100811": "T04",
    "000760131635": "T01",
    "000760131617": "T02",
    "000760131554": "A09",
    "000760131720": "A06",
    "000760131604": "A07",
    "000760131574": "A08",
    "000760131698": "A03",
    "000760131578": "A05",
    "000760131615": "A02",
    "000760100815": "A04",
    "000760131579": "A01",
    # The four boards on this branch's DS-TWR firmware. Deliberately not
    # T0x/A0x: every one of those labels is already taken above by a different
    # serial, and two boards answering to one label is worse than a long name.
    "000760131590": "TAG1",
    "000760131633": "TAG2",
    "000760131573": "ANC1",
    "000760131584": "ANC2",
}

# 4-hex UWB id -> the label set on that anchor with `nls`. The firmware sends only the
# id in the lec stream, so the label has to be resolved here on the host.
ANCHOR_NAMES = {"1FF8": "A01", "3002": "A02", "155B": "A03",
                "D089": "A04", "1FE6": "A05", "4659": "A06",
                "216A": "A07", "1925": "A08", "3003": "A09",
                # This branch's anchors carry explicitly assigned addresses
                # rather than PANS labels, so slot order is the name.
                "0101": "ANC1", "0102": "ANC2"}

# The device shell echoes each character and drops fast bursts.
CHAR_DELAY = 0.03
STALE_AFTER = 3.0          # seconds before a tag is shown as stale

# Calculated-RSSI model. The firmware exposes no real signal-strength register for
# TWR ranging (confirmed via dwm.h), so this is a free-space path-loss
# estimate from range_m, refined by the firmware's own 0-100 quality factor. None of
# these constants are documented anywhere in the firmware -- they are stated
# assumptions, not values read from the radio.
UWB_FREQ_MHZ = 6489.6          # DW1000 default UWB channel 5 centre frequency
ASSUMED_TX_POWER_DBM = -14.0   # typical DW1000 UWB TX power (FCC Part 15)
QF_PENALTY_DB_PER_POINT = 0.15  # extra loss per quality-factor point below 100


def estimate_rssi_dbm(range_m, dist_qf=None):
    """Calculated RSSI estimate in dBm -- NOT a hardware measurement.

    Friis/log-distance free-space path loss from range_m, plus a penalty
    scaled by (100 - dist_qf) when the firmware's own per-range quality factor is
    available (dwm-simple firmware only; 0 penalty otherwise).
    """
    r = max(float(range_m), 0.01)
    fspl_db = 20 * np.log10(r) + 20 * np.log10(UWB_FREQ_MHZ) - 27.55
    penalty_db = (100 - dist_qf) * QF_PENALTY_DB_PER_POINT if dist_qf is not None else 0.0
    return ASSUMED_TX_POWER_DBM - fspl_db - penalty_db

RATE_WINDOW = 10.0         # short window -> responsive Hz readout
STAT_WINDOW = 60.0         # long window  -> stable mean / sd

# Rate watchdog: how often to compare observed vs requested rate, how long to
# wait before judging (the Hz average needs time to settle after any change),
# and how far below expected counts as broken rather than jitter.
RATE_WATCHDOG_PERIOD = 15.0
RATE_WATCHDOG_GRACE = 25.0
RATE_WATCHDOG_FRAC = 0.5

STATE_LOCK = threading.Lock()
STATE = {"tags": {}, "anchors": {}, "started": time.time()}

# (tag, anchor_id) -> deque of (timestamp, range); anchor_id None = whole tag,
# where range is None. Counted in the serial thread so nothing is missed --
# sampling this over HTTP would only ever see the most recent frame.
SAMPLES = {}

PUB = None            # the Mqtt instance, or None when --mqtt-host is absent
PUB_TARGET = None


def note_hit(tag, anchor_id, t, rng=None):
    dq = SAMPLES.setdefault((tag, anchor_id), deque())
    dq.append((t, rng))
    cutoff = t - STAT_WINDOW
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def rates(now):
    """Per tag and per tag-anchor: Hz over RATE_WINDOW, mean/sd over STAT_WINDOW."""
    out = {}
    for (tag, aid), dq in SAMPLES.items():
        recent = sum(1 for t, _ in dq if t >= now - RATE_WINDOW)
        hz = recent / min(RATE_WINDOW, max(now - STATE["started"], 1e-6))
        d = out.setdefault(tag, {"tag_hz": 0.0, "tag_n": 0, "anchors": {}})
        if aid is None:
            d["tag_hz"] = hz
            d["tag_n"] = len(dq)
            continue
        vals = [r for _, r in dq if r is not None]
        e = {"hz": hz, "n": len(dq), "n_stat": len(vals)}
        if vals:
            a = np.asarray(vals)
            e.update(mean=float(a.mean()),
                     sd=float(a.std(ddof=1)) if len(a) > 1 else 0.0,
                     min=float(a.min()), max=float(a.max()))
        d["anchors"][aid] = e
    # share of the tag's frames each anchor appeared in
    for tag, d in out.items():
        tot = max(d["tag_n"], 1)
        for e in d["anchors"].values():
            e["share"] = 100.0 * e["n"] / tot
    return out


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
def multilaterate(anchors, ranges, tag_height, seed=None):
    """Least-squares position from ranges.

    >=4 anchors -> full 3D solve.
     3 anchors   -> 2D solve with z pinned to tag_height (3 ranges cannot
                    pin height without the solution mirroring about the
                    anchor plane).
    Returns (position, rms_residual_metres, mode) or (None, None, reason).
    """
    A = np.asarray(anchors, float)
    r = np.asarray(ranges, float)
    n = len(r)
    if n < 3:
        return None, None, f"need 3+ anchors, have {n}"

    solve_z = n >= 4
    p = np.asarray(seed, float) if seed is not None else np.append(A[:, :2].mean(0), tag_height)
    if not solve_z:
        p[2] = tag_height

    for _ in range(60):
        d = p - A
        dist = np.linalg.norm(d, axis=1)
        dist[dist < 1e-6] = 1e-6
        resid = dist - r
        J = d / dist[:, None]                    # d(dist)/d(p)
        if not solve_z:
            J = J[:, :2]
        try:
            # damped Gauss-Newton keeps it stable on near-degenerate geometry
            H = J.T @ J + np.eye(J.shape[1]) * 1e-6
            step = np.linalg.solve(H, J.T @ resid)
        except np.linalg.LinAlgError:
            return None, None, "singular geometry"
        if not solve_z:
            step = np.append(step, 0.0)
        # Step limiting. Without it a near-degenerate row set can throw the
        # estimate thousands of metres away and never recover.
        p = p - np.clip(step, -2.0, 2.0)
        if not solve_z:
            p[2] = tag_height
        if np.linalg.norm(step) < 1e-6:
            break

    dist = np.linalg.norm(p - A, axis=1)
    rms = float(np.sqrt(np.mean((dist - r) ** 2)))
    return p.tolist(), rms, "3D" if solve_z else f"2D @ z={tag_height}"


def robust_multilaterate(anchors, ranges, tag_height, seed=None, tol=0.35):
    """multilaterate() plus greedy outlier rejection.

    A single anchor with a wrong stored coordinate biases every fix that uses
    it, so while the residual stays above `tol` we drop the worst-fitting
    anchor and refit -- as long as 3 anchors remain. Returns the fit plus the
    ids of whatever was excluded, so the UI can show it rather than hide it.
    """
    idx = list(range(len(ranges)))
    excluded = []
    best = multilaterate([anchors[i] for i in idx], [ranges[i] for i in idx],
                         tag_height, seed)
    # Judge an anchor by how far its own residual sits from the pack, not by
    # whether the overall rms improves -- rms is not comparable across fits of
    # different dimensionality (a 3-anchor fit is z-constrained, a 4-anchor one
    # is not), which made an earlier version never reject anything.
    while best[0] is not None and len(idx) > 3:
        p = np.asarray(best[0])
        resid = np.array([abs(np.linalg.norm(p - np.asarray(anchors[i])) - ranges[i])
                          for i in idx])
        med = float(np.median(resid))
        w = int(np.argmax(resid))
        if not (resid[w] > tol and resid[w] > 3.0 * max(med, 0.05)):
            break
        excluded.append(idx[w])
        idx = [i for k, i in enumerate(idx) if k != w]
        best = multilaterate([anchors[i] for i in idx], [ranges[i] for i in idx],
                             tag_height, best[0])
    return best[0], best[1], best[2], excluded


# --------------------------------------------------------------------------- #
# mqtt
# --------------------------------------------------------------------------- #
# The firmware gives no access to the three TWR timestamps -- the range is computed
# inside its closed library. Publishing an explicit list beats emitting
# nulls, which a consumer cannot tell apart from a dropped reading. "rssi" is
# no longer in this list: a CALCULATED estimate is now published for it (see
# estimate_rssi_dbm) -- it is not a real measurement, the firmware still provides none.
UNAVAILABLE = ["t_poll", "t_resp", "t_final"]


class Mqtt:
    """Optional MQTT publisher. Drops (does not buffer) while disconnected --
    replaying stale positions after an outage is worse than losing them."""

    def __init__(self, host, port, root, qos, user, pw, client_id):
        if mqtt is None:
            raise SystemExit("pip install --user paho-mqtt")
        self.root, self.qos = root, qos
        self.pub = self.drop = 0
        self.connected = False
        self.lock = threading.Lock()
        if hasattr(mqtt, "CallbackAPIVersion"):      # paho 2.x
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=client_id, clean_session=True)
            self.c.on_connect = lambda c, u, f, rc, p=None: self._up(rc == 0, rc)
            self.c.on_disconnect = lambda c, u, f, rc, p=None: self._up(False, rc)
        else:                                        # paho 1.x
            self.c = mqtt.Client(client_id=client_id, clean_session=True)
            self.c.on_connect = lambda c, u, f, rc: self._up(rc == 0, rc)
            self.c.on_disconnect = lambda c, u, rc: self._up(False, rc)
        if user:
            self.c.username_pw_set(user, pw)
        self.c.reconnect_delay_set(min_delay=1, max_delay=15)
        try:
            self.c.connect_async(host, port, keepalive=30)
        except Exception as e:
            print(f"  mqtt: {e}")
        self.c.loop_start()

    def _up(self, ok, rc):
        self.connected = ok
        print(f"  mqtt: {'connected' if ok else f'disconnected ({rc})'}")

    def emit(self, topic, payload):
        r = self.c.publish(f"{self.root}/{topic}",
                           json.dumps(payload, separators=(",", ":")),
                           qos=self.qos, retain=False)
        with self.lock:
            if r.rc == 0:
                self.pub += 1
            else:
                self.drop += 1

    def publish_reading(self, tag, rd, seq):
        for a in rd["anchors"]:
            label = ANCHOR_NAMES.get(a["id"], a["id"])
            # NOTE: "rssi" is a CALCULATED estimate (estimate_rssi_dbm), not a
            # hardware measurement -- the firmware has no true per-range RSSI register
            # (confirmed via dwm.h). timestamp_sent is the tag's own on-board
            # estimate of when the fix was taken (dwm_systime_us_get(), wraps
            # ~35 min, unwrapped to monotonic here; not available on stock
            # stock lec). timestamp_received is when this machine parsed the line.
            msg_r = {
                "tag_label": tag,
                "anchor_label": label,
                "range_m": round(a["range"], 3),
                "timestamp_sent": rd.get("device_us"),
                "timestamp_received": round(rd["t"], 3),
            }
            if a.get("rxp_dbm") is not None:
                # A real measurement on this firmware: rxp/fpp come from
                # dwt_readdiagnostics() on the DW1000. rssi_estimated stays
                # false so a consumer can tell it apart from the path-loss
                # figure the PANS builds have to fall back on.
                msg_r.update({
                    "rssi": round(a["rxp_dbm"], 1),
                    "rssi_estimated": False,
                    "first_path_dbm": round(a["fpp_dbm"], 1),
                    "tof_ps": a.get("tof_ps"),
                    "slot": a.get("slot"),
                    "flags": a.get("flags"),
                    "flag_names": a.get("flag_names"),
                })
            else:
                msg_r.update({
                    "rssi": round(estimate_rssi_dbm(a["range"],
                                                    a.get("dist_qf")), 1),
                    "rssi_estimated": True,
                })
            self.emit(f"ranging/{tag}/{label}", msg_r)
        msg = {"tag_label": tag, "n_anchors": len(rd["anchors"]),
               "host_time": round(rd["t"], 3), "device_us": rd.get("device_us"),
               "fw": rd.get("fw", "stock"), "seq": seq}
        if rd.get("sfn") is not None:
            msg["sfn"] = rd["sfn"]
        if rd.get("lost_slots"):
            msg["lost_slots"] = rd["lost_slots"]
        if rd.get("onboard"):
            msg["onboard"] = {"x": rd["onboard"]["pos"][0], "y": rd["onboard"]["pos"][1],
                              "z": rd["onboard"]["pos"][2], "qf": rd["onboard"]["qf"]}
        if rd.get("lsq"):
            msg["lsq"] = {"x": round(rd["lsq"][0], 3), "y": round(rd["lsq"][1], 3),
                          "z": round(rd["lsq"][2], 3),
                          "rms_m": round(rd["rms"], 3) if rd.get("rms") else None,
                          "mode": rd.get("mode")}
        if len(msg) > 4:
            self.emit(f"position/{tag}", msg)


# --------------------------------------------------------------------------- #
# serial
# --------------------------------------------------------------------------- #
DIST_RE = re.compile(r"DIST,(\d+),(.*)")

# Tags running the dwm-simple on-board user app emit a different line, carrying
# a device-side microsecond timestamp taken at the moment of the fix:
#   T:<us> POS:[x,y,z,qf]|N/A DIST0:0xADDR[x,y,z]=[dist_mm,qf] DIST1:...
# That epoch is far better than our parse time, so prefer it when present.
SIMPLE_RE = re.compile(r"T:(\d+)\s+POS:(\S+)")
SIMPLE_DIST_RE = re.compile(
    r"DIST\d+:0x([0-9A-Fa-f]{4})\[(-?\d+),(-?\d+),(-?\d+)\]=\[(\d+),(\d+)\]")
SIMPLE_POS_RE = re.compile(r"\[(-?\d+),(-?\d+),(-?\d+),(\d+)\]")

# dwm_systime_us_get() is a 32-bit microsecond counter: it wraps every ~35 min.
# Unwrap per tag into a monotonic 64-bit value so fusion consumers can rely on it.
_US_WRAP = 1 << 32
_us_state = {}


def unwrap_us(tag, raw):
    prev_raw, acc = _us_state.get(tag, (None, 0))
    if prev_raw is not None and raw < prev_raw - _US_WRAP // 2:
        acc += _US_WRAP                      # counter rolled over
    _us_state[tag] = (raw, acc)
    return raw + acc


def parse_simple(line, tag, tag_height):
    """Parse one dwm-simple line. Same shape of result as parse_lec()."""
    m = SIMPLE_RE.search(line)
    if not m:
        return None
    anchors = []
    for aid, x, y, z, dist, qf in SIMPLE_DIST_RE.findall(line):
        anchors.append({"id": aid.upper(),
                        "pos": [int(x) / 1000.0, int(y) / 1000.0, int(z) / 1000.0],
                        "range": int(dist) / 1000.0,
                        "dist_qf": int(qf)})
    if not anchors:
        return None
    onboard = None
    if m.group(2) != "N/A":
        pm = SIMPLE_POS_RE.search(line)
        if pm:
            onboard = {"pos": [int(pm.group(1)) / 1000.0, int(pm.group(2)) / 1000.0,
                               int(pm.group(3)) / 1000.0], "qf": int(pm.group(4))}
    lsq, rms, mode, excl = robust_multilaterate(
        [a["pos"] for a in anchors], [a["range"] for a in anchors],
        tag_height, seed=onboard["pos"] if onboard else None)
    if lsq is not None:
        p = np.asarray(lsq)
        for a in anchors:
            a["resid"] = float(np.linalg.norm(p - np.asarray(a["pos"])) - a["range"])
    return {"t": time.time(), "device_us": unwrap_us(tag, int(m.group(1))),
            "fw": "dwm-simple", "anchors": anchors, "onboard": onboard,
            "lsq": lsq, "rms": rms, "mode": mode, "excluded": excl,
            "excluded_labels": [ANCHOR_NAMES.get(anchors[i]["id"], anchors[i]["id"])
                                for i in excl]}


# --------------------------------------------------------------------------- #
# This branch's protocol: one $RNG record per tag-anchor pair, section 9.4.
#
#   $RNG,<sfn>,<frame>,0x<tag>,0x<anchor>,<slot>,<d_mm>,<tof_ps>,<rxp>,<fpp>,
#        0x<flags>,<t_us>*<cs>
#
# Unlike the stock firmwares, a line is one PAIR, not one fix, so records are
# accumulated by superframe number and flushed when the sfn moves on. Two
# things this protocol gives that the closed PANS library could not: a real
# time of flight in picoseconds, and a genuine measured RSSI -- rxp/fpp come
# from dwt_readdiagnostics() on the DW1000 itself, not a path-loss model.
RNG_RE = re.compile(
    r"\$RNG,(\d+),(\d+),0x([0-9A-Fa-f]{4}),0x([0-9A-Fa-f]{4}),(\d+),"
    r"(-?\d+),(\d+),(-?[\d.]+),(-?[\d.]+),0x([0-9A-Fa-f]+),(\d+)")

# Section 9.4 flag bits.
F_SS_FALLBACK, F_NLOS, F_RESP_LOST = 0x01, 0x02, 0x04
F_REPORT_LOST, F_DESYNC, F_HASH_MISMATCH = 0x08, 0x10, 0x20

_RNG_ACC = {}          # tag -> {"sfn": n, "anchors": [...], "device_us": us}


def _flag_names(f):
    out = []
    for bit, nm in ((F_SS_FALLBACK, "ss_fallback"), (F_NLOS, "nlos"),
                    (F_RESP_LOST, "resp_lost"), (F_REPORT_LOST, "report_lost"),
                    (F_DESYNC, "desync"), (F_HASH_MISMATCH, "hash_mismatch")):
        if f & bit:
            out.append(nm)
    return out


def parse_rng(line, tag, tag_height, coords):
    """Accumulate $RNG pairs into one reading per superframe.

    Returns a reading when a superframe completes, else None."""
    m = RNG_RE.search(line)
    if not m:
        return None
    (sfn, _frame, _tag_id, anchor_id, slot, d_mm, tof_ps,
     rxp, fpp, flags, dev_us) = m.groups()
    sfn, slot, flags = int(sfn), int(slot), int(flags, 16)

    acc = _RNG_ACC.get(tag)
    done = None
    if acc is not None and acc["sfn"] != sfn:
        done = acc                      # the previous superframe is complete
    if done is not None or acc is None:
        acc = {"sfn": sfn, "anchors": [], "device_us": None}
        _RNG_ACC[tag] = acc
    acc["sfn"] = sfn
    acc["device_us"] = unwrap_us(tag, int(dev_us))

    # A lost Response carries no distance; keep it out of the fix but report it.
    if not (flags & F_RESP_LOST):
        # Anchor coordinates come from the survey file, keyed the way
        # uwb_anchors.json is: slot 0 is anchor "1".
        pos = coords.get(str(slot + 1))
        acc["anchors"].append({
            "id": anchor_id.upper(),
            "slot": slot,
            "range": int(d_mm) / 1000.0,
            "tof_ps": int(tof_ps),
            "rxp_dbm": float(rxp),
            "fpp_dbm": float(fpp),
            "flags": flags,
            "flag_names": _flag_names(flags),
            "pos": pos,
            "dist_qf": None,
        })
    else:
        acc.setdefault("lost", []).append(slot)

    if done is None:
        return None

    rd = {"t": time.time(), "device_us": done["device_us"],
          "anchors": done["anchors"], "fw": "dstwr", "sfn": done["sfn"],
          "lost_slots": done.get("lost", [])}

    # A 2D fix needs three ranges against known positions; below that the
    # ranges are the whole output and no position is claimed.
    usable = [a for a in rd["anchors"] if a["pos"]]
    if len(usable) >= 3:
        lsq, rms, mode, excl = robust_multilaterate(
            [a["pos"] for a in usable], [a["range"] for a in usable], tag_height)
        rd.update({"lsq": lsq, "rms": rms, "mode": mode, "excluded": excl})
    return rd


def parse_lec(line, tag_height):
    """Parse one `lec` CSV line into a reading dict, or None."""
    m = DIST_RE.search(line)
    if not m:
        return None
    n = int(m.group(1))
    f = m.group(2).split(",")
    anchors, i = [], 0
    for _ in range(n):
        # ANi,<id>,<x>,<y>,<z>,<range>
        if i + 5 >= len(f) or not f[i].startswith("AN"):
            break
        try:
            anchors.append({
                "id": f[i + 1],
                "pos": [float(f[i + 2]), float(f[i + 3]), float(f[i + 4])],
                "range": float(f[i + 5]),
            })
        except (ValueError, IndexError):
            break
        i += 6

    onboard = None
    if "POS" in f:
        k = f.index("POS")
        try:
            onboard = {"pos": [float(f[k + 1]), float(f[k + 2]), float(f[k + 3])],
                       "qf": int(f[k + 4])}
        except (ValueError, IndexError):
            onboard = None

    lsq, rms, mode, excl = robust_multilaterate(
        [a["pos"] for a in anchors], [a["range"] for a in anchors],
        tag_height, seed=onboard["pos"] if onboard else None)
    excluded = [anchors[i]["id"] for i in excl]
    # per-anchor residual against the accepted fit, for the UI table
    if lsq is not None:
        p = np.asarray(lsq)
        for a in anchors:
            a["resid"] = float(np.linalg.norm(p - np.asarray(a["pos"])) - a["range"])
    return {"t": time.time(), "anchors": anchors, "onboard": onboard,
            "lsq": lsq, "rms": rms, "mode": mode, "excluded": excluded,
            "excluded_labels": [ANCHOR_NAMES.get(i, i) for i in excluded]}


def enter_shell(ser):
    """UART boots in Generic mode; Enter twice within 1 s -> shell."""
    for _ in range(5):
        ser.reset_input_buffer()
        ser.write(b"\r"); time.sleep(0.15)
        ser.write(b"\r"); time.sleep(1.2)
        if b"dwm>" in ser.read(8192):
            return True
    return False


def send(ser, cmd):
    for ch in cmd:
        ser.write(ch.encode()); time.sleep(CHAR_DELAY)
    ser.write(b"\r")


def apply_rate(ser, name, rate, quiet=False):
    """Send `aurs` to a tag. The dwm-simple user app calls
    dwm_upd_rate_set(10,10) -- i.e. 1 Hz -- at every boot, so the wanted rate
    has to be re-asserted rather than set once. Works on either firmware since
    the shell stays compiled in."""
    if not enter_shell(ser):
        if not quiet:
            print(f"  {name}: could not set rate (no shell)")
        return False
    send(ser, f"aurs {rate.replace(',', ' ')}")
    time.sleep(2.5)
    ser.reset_input_buffer()
    if not quiet:
        print(f"  {name}: rate set to {rate}")
    return True


def expected_hz(rate):
    """Frames/sec implied by an `aurs` value (units of 100 ms)."""
    try:
        ur = int(str(rate).split(",")[0])
        return 10.0 / max(ur, 1)
    except (ValueError, IndexError):
        return None


def tag_thread(device, name, tag_height, stop, pub=None, rate=None,
               coords=None):
    seq = 0
    last_status = 0.0
    while not stop.is_set():
        try:
            ser = serial.Serial(device, 115200, timeout=0.3)
            time.sleep(0.4)

            # Listen before writing anything. This branch's firmware streams
            # $RNG unprompted and has no dwm> shell at all -- the double-Enter
            # that enters the PANS shell would just be stray bytes in its
            # command parser -- so the shell is only entered if no $RNG stream
            # appears and we are therefore talking to a PANS build.
            buf = ""
            fw = None
            deadline = time.time() + 3.0
            while time.time() < deadline and fw is None:
                buf += ser.read(4096).decode(errors="replace")
                if "$RNG," in buf or "$I,fw=" in buf:
                    fw = "dstwr"
                elif SIMPLE_RE.search(buf):
                    fw = "dwm-simple"
                elif "DIST," in buf:
                    fw = "stock"

            if fw is None:
                if not enter_shell(ser):
                    raise RuntimeError("no $RNG stream and no dwm> prompt")
                # `lec` TOGGLES, so blindly sending it can switch off a stream a
                # previous run left running.
                send(ser, "lec")
                deadline = time.time() + 4
                while time.time() < deadline and fw is None:
                    buf += ser.read(4096).decode(errors="replace")
                    if SIMPLE_RE.search(buf):
                        fw = "dwm-simple"
                    elif "DIST," in buf:
                        fw = "stock"
                if fw is None:
                    raise RuntimeError("no range stream (tried enabling lec)")
            print(f"  {name}: firmware = {fw}")

            if rate and fw == "dstwr":
                print(f"  {name}: --rate ignored on dstwr firmware "
                      f"(use $SET,rate_div,N instead)")
            elif rate:
                apply_rate(ser, name, rate)
            # Watchdog state. A soft reset of the nRF52 leaves the USB link up
            # (the J-Link OB is a separate chip), so no reconnect fires and the
            # tag silently reverts to the 1 Hz the app sets at boot. Detect that
            # from the observed rate instead of relying on a disconnect.
            wd_next = time.time() + RATE_WATCHDOG_GRACE
            wd_fixes = 0

            with STATE_LOCK:
                STATE["tags"].setdefault(name, {})["device"] = device
                STATE["tags"][name]["connected"] = True

            while not stop.is_set():
                buf += ser.read(4096).decode(errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if fw == "dstwr":
                        rd = parse_rng(line, name, tag_height, coords or {})
                    elif fw == "dwm-simple":
                        rd = parse_simple(line, name, tag_height)
                    else:
                        rd = parse_lec(line, tag_height)
                    if rd:
                        with STATE_LOCK:
                            STATE["tags"][name].update(rd)
                            STATE["tags"][name]["connected"] = True
                            note_hit(name, None, rd["t"])
                            for a in rd["anchors"]:
                                note_hit(name, a["id"], rd["t"], a["range"])
                                prev = STATE["anchors"].get(a["id"], {})
                                STATE["anchors"][a["id"]] = {
                                    "pos": a["pos"] or prev.get("pos"),
                                    "last": rd["t"]}
                        if pub:
                            seq += 1
                            pub.publish_reading(name, rd, seq)
                            if rd["t"] - last_status > 5:
                                last_status = rd["t"]
                                with STATE_LOCK:
                                    hz = rates(rd["t"]).get(name, {}).get("tag_hz", 0.0)
                                pub.emit(f"status/{name}", {
                                    "tag_label": name, "hz": round(hz, 2),
                                    "online": True,
                                    "host_time": round(rd["t"], 3)})

                # --- rate watchdog ---
                if rate and fw != "dstwr" and time.time() >= wd_next:
                    want = expected_hz(rate)
                    with STATE_LOCK:
                        seen = rates(time.time()).get(name, {}).get("tag_hz", 0.0)
                    # Only act on a clear shortfall, so ordinary jitter and a
                    # briefly quiet link do not trigger a shell excursion (which
                    # itself costs a couple of seconds of stream).
                    if want and seen > 0 and seen < want * RATE_WATCHDOG_FRAC:
                        wd_fixes += 1
                        print(f"  {name}: rate {seen:.2f} Hz < {want:.2f} Hz "
                              f"expected - re-asserting {rate} "
                              f"(fix #{wd_fixes})")
                        apply_rate(ser, name, rate, quiet=True)
                        buf = ""          # drop shell echo left in the buffer
                        wd_next = time.time() + RATE_WATCHDOG_GRACE
                    else:
                        wd_next = time.time() + RATE_WATCHDOG_PERIOD
        except Exception as e:
            with STATE_LOCK:
                STATE["tags"].setdefault(name, {})
                STATE["tags"][name]["connected"] = False
                STATE["tags"][name]["error"] = str(e)
            time.sleep(2)
        finally:
            try:
                ser.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with STATE_LOCK:
                now = time.time()
                tags = {}
                for k, v in STATE["tags"].items():
                    v = dict(v)
                    v["stale"] = (now - v.get("t", 0)) > STALE_AFTER
                    v["age"] = round(now - v["t"], 2) if v.get("t") else None
                    tags[k] = v
                body = json.dumps({
                    "tags": tags,
                    "anchors_seen": STATE["anchors"],
                    "planned": STATE["planned"],
                    "rates": rates(now),
                    "window": RATE_WINDOW,
                    "stat_window": STAT_WINDOW,
                    "mqtt": (None if PUB is None else
                             {"host": PUB_TARGET, "connected": PUB.connected,
                              "published": PUB.pub, "dropped": PUB.drop,
                              "root": PUB.root, "qos": PUB.qos}),
                    "now": now,
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", ""):
            self.path = "/ui.html"
        return super().do_GET()


def main():
    global PUB, PUB_TARGET
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--tag-height", type=float, default=1.5,
                    help="assumed tag z (metres) when only 3 anchors are visible")
    ap.add_argument("--coords", default=os.path.join(HERE, "uwb_anchors.json"))
    ap.add_argument("--mqtt-host", help="also publish to this MQTT broker")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--topic-root", default="uwb")
    ap.add_argument("--qos", type=int, default=0, choices=[0, 1, 2])
    ap.add_argument("--mqtt-user"); ap.add_argument("--mqtt-pass")
    ap.add_argument("--client-id", default="dwm1001-localize")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="HTTP bind address; use 0.0.0.0 to reach the UI from "
                         "other machines (e.g. when running on a Jetson)")
    ap.add_argument("--rate", metavar="UR,URS",
                    help="send `aurs UR URS` to each tag on connect (units of "
                         "100 ms). The dwm-simple user app resets itself to 1 Hz "
                         "on every boot, so pass e.g. 1,1 to hold 10 Hz.")
    a = ap.parse_args()

    if a.mqtt_host:
        PUB_TARGET = f"{a.mqtt_host}:{a.mqtt_port}"
        print(f"  mqtt -> {PUB_TARGET} root '{a.topic_root}' qos {a.qos}")
        print(f"  not published (firmware cannot provide): {', '.join(UNAVAILABLE)}")
        PUB = Mqtt(a.mqtt_host, a.mqtt_port, a.topic_root, a.qos,
                   a.mqtt_user, a.mqtt_pass, a.client_id)

    planned = {}
    if os.path.exists(a.coords):
        planned = {k: v for k, v in json.load(open(a.coords)).items()}
    STATE["planned"] = planned

    ports = dwm_ports()
    if not ports:
        raise SystemExit("no boards connected")

    stop = threading.Event()
    threads = []
    for p in ports:
        name = NODE_NAMES.get(p.serial_number, p.serial_number)
        if name.startswith("A"):
            print(f"  skipping {name} ({p.device}) - anchors do not report ranges")
            continue
        print(f"  reading {name} on {p.device}")
        t = threading.Thread(target=tag_thread,
                             args=(p.device, name, a.tag_height, stop, PUB, a.rate,
                                   planned),
                             daemon=True)
        t.start(); threads.append(t)
    if not threads:
        raise SystemExit("no tags found among connected boards")

    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    shown = "127.0.0.1" if a.bind in ("127.0.0.1", "localhost") else a.bind
    print(f"\n  UI: http://{shown}:{a.port}\n  Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop.set()
        srv.server_close()
        if PUB:
            PUB.c.loop_stop(); PUB.c.disconnect()


if __name__ == "__main__":
    main()
