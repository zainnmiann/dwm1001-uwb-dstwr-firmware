# UWB Ranging Bridge (Host)

Reads live tag-to-anchor ranges over USB serial and publishes them to MQTT,
with a small live-view UI.

## Layout

- `localize.py` — the bridge process. Owns the tag serial ports, parses each
  ranging line, computes an independent least-squares position estimate, and
  (optionally) publishes to MQTT.
- `dwm_ports.py` — finds attached tag/anchor boards by USB vendor/product id
  rather than OS-specific device names, so the same code runs on macOS and
  Linux (e.g. a Jetson).
- `ui.html` — the page `localize.py` serves for a live view of tags, anchors
  and ranges.
- `uwb_anchors.json` — surveyed anchor coordinates (metres), keyed by anchor
  number.

## Compatibility

This bridge's serial parser targets the line framing the tags' **current**
on-board application emits (`DIST,...` CSV lines, or `T:<us> POS:...
DISTn:...` lines) — **not** this branch's `$RNG,...` protocol. Until the tags
are reflashed with the firmware in this branch, `localize.py` is what
actually talks to them; it will not recognize `$RNG` lines. Updating the
parser for the new protocol is a follow-up, not done here.

## Running it

```sh
python3 localize.py --mqtt-host <broker-ip> --bind 0.0.0.0 --rate 1,1
```

- `--mqtt-host` — broker to publish to. Omit to run the local UI only.
- `--bind` — HTTP bind address for the UI (`0.0.0.0` to reach it from other
  machines, e.g. when this runs headless on a Jetson).
- `--rate` — `UR,URS` in units of 100 ms, re-sent to each tag on connect and
  after any detected reboot (the tags' on-board application resets its own
  update rate at boot, so this has to be reasserted rather than set once).

MQTT topics published:

```
uwb/ranging/<tag>/<anchor>   one message per tag-anchor range
uwb/position/<tag>           this bridge's own least-squares fix, plus the
                              tag's on-board fix if available
uwb/status/<tag>             per-tag health (observed rate, connection state)
```

The live view is at `http://<host>:8765/` once running (or whatever
`--bind`/`--port` you chose).

## Notes

- `rssi` in each ranging message is a **calculated estimate** from range and
  the current firmware's own quality factor (a free-space path-loss model) —
  not a hardware measurement. No radio on this hardware exposes true
  per-range signal strength today.
- `timestamp_sent` is the tag's own on-board clock at the moment of the fix;
  `timestamp_received` is when this host parsed the line.
- To run on a new device: copy this directory over, then point
  `--mqtt-host`/`--bind` at that box.
