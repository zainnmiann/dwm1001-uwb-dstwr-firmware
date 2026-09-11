#!/usr/bin/env python3
"""
Stream DWM1001 tag ranges from the UART to MQTT.

One MQTT message per anchor-range, published to
    buoy/uwb/ranging/<tag_label>/<anchor_label>
"""

import json
import os
import re
import signal
import sys
import time

import serial
import paho.mqtt.client as mqtt

SERIAL_PORT = os.environ.get("UWB_PORT", "/dev/serial0")
SERIAL_BAUD = int(os.environ.get("UWB_BAUD", "115200"))
MQTT_HOST = os.environ.get("MQTT_HOST", "192.168.1.110")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None
TOPIC_ROOT = os.environ.get("MQTT_TOPIC_ROOT", "buoy/uwb/ranging")
SOURCE = os.environ.get("UWB_SOURCE", "buoy")
ANCHORS_FILE = os.environ.get("UWB_ANCHORS", "/home/pi/uwb_anchors.json")

# Location update rate in units of 100 ms: 1 = 10 Hz, 10 = 1 Hz.
UWB_RATE = os.environ.get("UWB_RATE", "1")

# The tag stops streaming after a reset; if the port goes quiet this long, re-arm it.
QUIET_TIMEOUT = float(os.environ.get("UWB_QUIET_TIMEOUT", "12"))

# Range line emitted by the tag's on-board application:
#   T:186650380 POS:[19260,27978,2973,79] DIST0:0x155B[18300,14000,7000]=[15780,100] ...
# POS is absent when the location engine has no fix, so it is matched separately.
RE_T = re.compile(r"\bT:(\d+)")
RE_DIST = re.compile(
    r"DIST(\d+):0x([0-9A-Fa-f]{4})"
    r"\[(-?\d+),(-?\d+),(-?\d+)\]"
    r"=\[(-?\d+),(\d+)\]"
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_anchor_map(path):
    """Map anchor coordinates (metres) -> label, e.g. (18.3,14.0,7.0) -> 'A03'.

    The tag reports each anchor's own coordinates next to its address, so labels
    are resolved by matching those against the surveyed anchor list. That avoids
    maintaining a hex-address table by hand.
    """
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except Exception as exc:
        log(f"WARN: no anchor map ({exc}); falling back to address labels")
        return {}
    return {(round(v[0], 2), round(v[1], 2), round(v[2], 2)): "A%02d" % int(k)
            for k, v in raw.items()}


class AnchorLabeller:
    def __init__(self, coord_map):
        self.coord_map = coord_map
        self.cache = {}
        self.warned = set()

    def label(self, addr_hex, x_mm, y_mm, z_mm):
        if addr_hex in self.cache:
            return self.cache[addr_hex]
        key = (round(x_mm / 1000.0, 2), round(y_mm / 1000.0, 2), round(z_mm / 1000.0, 2))
        name = self.coord_map.get(key)
        if name is None:
            # Tolerate small survey/rounding differences before giving up.
            for (ax, ay, az), cand in self.coord_map.items():
                if abs(ax - key[0]) < 0.05 and abs(ay - key[1]) < 0.05 and abs(az - key[2]) < 0.05:
                    name = cand
                    break
        if name is None:
            name = f"A-{addr_hex.upper()}"
            if addr_hex not in self.warned:
                log(f"WARN: anchor 0x{addr_hex} at {key} is not in the survey; using {name}")
                self.warned.add(addr_hex)
        self.cache[addr_hex] = name
        return name


class Tag:
    """Owns the serial port and keeps the tag streaming `les` at the wanted rate."""

    def __init__(self, port, baud):
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.label = os.environ.get("UWB_TAG_LABEL") or None

    def _shell(self):
        """The port starts in a binary mode: two CRs within a second reach `dwm>`."""
        self.ser.write(b"\r")
        time.sleep(0.1)
        self.ser.write(b"\r")
        time.sleep(0.8)

    def _send(self, cmd):
        """Send a command one character at a time.

        The tag echoes input while printing ranges; a burst write gets
        interleaved and the command is mis-parsed (`aurs 1 1` arrives as
        `aurs 1`, and `les` is dropped entirely).
        """
        for ch in cmd:
            self.ser.write(ch.encode())
            self.ser.flush()
            time.sleep(0.04)
        self.ser.write(b"\r")
        self.ser.flush()

    def _is_streaming(self, secs):
        self.ser.reset_input_buffer()
        deadline = time.time() + secs
        seen = b""
        while time.time() < deadline:
            seen += self.ser.read(512)
            if b"T:" in seen and b"DIST" in seen:
                return True
            time.sleep(0.05)
        return False

    def _quiet(self):
        """Stop `les` so command replies are not interleaved with range lines.

        The tag echoes input and prints continuously; a query issued mid-stream
        comes back shredded, which is how `si` silently yields no label.
        """
        if not self._is_streaming(2.0):
            return True
        self._shell()
        for _ in range(4):
            self._send("les")          # toggle
            time.sleep(1.5)
            if not self._is_streaming(2.5):
                return True
        return False

    def _read_label(self):
        """Read the configured label from `si` (e.g. label=T05)."""
        for attempt in range(3):
            self.ser.reset_input_buffer()
            self._send("si")
            time.sleep(2.0)
            text = self.ser.read(8000).decode("utf-8", "replace")
            m = re.search(r"label=(\S+)", text)
            if m:
                return m.group(1)
            log(f"label read attempt {attempt + 1} failed; retrying")
        log("WARN: could not read tag label; using 'TAG'")
        return "TAG"

    def _si(self):
        self.ser.reset_input_buffer()
        self._send("si")
        time.sleep(2.5)
        return self.ser.read(8000).decode("utf-8", "replace")

    def ensure_active(self):
        """Restore an active tag if it has reverted to a passive node.

        A brownout drops the config page back to factory defaults: the node
        comes back *passive*, which never initiates ranging, so it emits no
        ranges at all and looks like dead hardware.
        """
        info = self._si()
        m = re.search(r"mode: tn \((\w+)", info)
        if not m:
            log("WARN: could not read node mode")
            return False
        if m.group(1) != "pasv":
            return False
        log("tag is PASSIVE (brownout default) - restoring active mode")
        self._send("nmt")          # switching node mode resets the tag
        time.sleep(10)
        self._shell()
        if self.label:
            self._send(f"nls {self.label}")
            time.sleep(1.5)
            log(f"label restored to {self.label}")
        return True

    def set_rate(self, rate):
        """Force the update rate via `aurs <norm> <stat>`, one character at a time.

        A burst sent while the tag echoes gets interleaved and the trailing
        argument is lost. A browned-out tag also reverts to the 1 Hz factory
        default, so this is re-applied on every start rather than trusted.
        """
        self._send(f"aurs {rate} {rate}")
        time.sleep(1.0)
        log(f"update rate set to {rate} (x100ms)")

    def start(self):
        """Bring the tag to a known state: quiet -> label + rate -> streaming."""
        if self._quiet():
            if self.ensure_active():
                self._quiet()
            if not self.label:
                self.label = self._read_label()
                log(f"tag label = {self.label}")
            self.set_rate(UWB_RATE)
        else:
            # Commands cannot be issued reliably into a live stream, but a tag
            # that is already streaming needs no correcting; the recovery path
            # that matters (brownout -> passive -> silent) finds a quiet port.
            log("tag already streaming; leaving label and rate as-is")
            self.label = self.label or "TAG"
        self._shell()
        self._send("les")
        time.sleep(0.6)
        if self._is_streaming(4.0):
            log("streaming")
        else:
            log("WARN: no stream after enabling les")
        return self.label

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def build_mqtt():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"uwb-bridge-{os.uname().nodename}")
    except (AttributeError, TypeError):        # paho 1.x
        client = mqtt.Client(client_id=f"uwb-bridge-{os.uname().nodename}")
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(f"{TOPIC_ROOT}/status", json.dumps({"source": SOURCE, "online": False}),
                    qos=1, retain=True)

    def on_connect(c, u, flags, rc, props=None):
        log(f"MQTT connected ({rc})")
        c.publish(f"{TOPIC_ROOT}/status", json.dumps({"source": SOURCE, "online": True}),
                  qos=1, retain=True)

    def on_disconnect(c, u, *a):
        log("MQTT disconnected; reconnecting")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


def main():
    running = {"go": True}
    signal.signal(signal.SIGTERM, lambda *a: running.update(go=False))
    signal.signal(signal.SIGINT, lambda *a: running.update(go=False))

    labeller = AnchorLabeller(load_anchor_map(ANCHORS_FILE))
    client = build_mqtt()

    tag = Tag(SERIAL_PORT, SERIAL_BAUD)
    tag_label = tag.start()

    buf = b""
    last_data = time.time()
    published = 0
    last_report = time.time()

    while running["go"]:
        chunk = tag.ser.read(512)
        if chunk:
            buf += chunk
            last_data = time.time()
        elif time.time() - last_data > QUIET_TIMEOUT:
            log("no data from tag; re-arming")
            tag_label = tag.start()
            last_data = time.time()

        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            m_t = RE_T.search(line)
            if not m_t:
                continue
            ts_sent = int(m_t.group(1))
            now = time.time()
            for m in RE_DIST.finditer(line):
                _idx, addr, ax, ay, az, dist_mm, _qf = m.groups()
                anchor = labeller.label(addr, int(ax), int(ay), int(az))
                payload = {
                    "source": SOURCE,
                    "tag_label": tag_label,
                    "anchor_label": anchor,
                    "range_m": round(int(dist_mm) / 1000.0, 3),
                    "rssi": None,       # no per-range RSSI is exposed over this interface
                    "timestamp_sent": ts_sent,
                    "timestamp_received": round(now, 3),
                }
                client.publish(f"{TOPIC_ROOT}/{tag_label}/{anchor}",
                               json.dumps(payload), qos=0)
                published += 1
        if time.time() - last_report > 60:
            log(f"published {published} ranges")
            last_report = time.time()

        if len(buf) > 16384:               # never let a stuck line grow unbounded
            buf = buf[-4096:]

    log("shutting down")
    client.publish(f"{TOPIC_ROOT}/status", json.dumps({"source": SOURCE, "online": False}),
                   qos=1, retain=True)
    time.sleep(0.3)
    client.loop_stop()
    tag.close()


if __name__ == "__main__":
    sys.exit(main())
