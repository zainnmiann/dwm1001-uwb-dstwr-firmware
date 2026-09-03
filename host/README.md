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

The parser handles three line formats and picks one by listening before it
writes anything:

- `$RNG,...` — this branch's protocol (`fw = dstwr`). One record per
  tag-anchor **pair**, accumulated by superframe.
- `T:<us> POS:... DISTn:...` — the dwm-simple on-board app.
- `DIST,...` — stock PANS, via `lec`.

This firmware has no `dwm>` shell, so the shell is only entered when no `$RNG`
stream appears; sending the double-Enter to it would just be stray bytes in
its command parser. `--rate` and the rate watchdog are PANS shell commands and
are skipped on `dstwr` — use `$SET,rate_div,N` on the tag instead.

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
uwb/position/<tag>           per-superframe metadata, plus a least-squares fix
                              when three or more surveyed anchors were heard
uwb/status/<tag>             per-tag health (observed rate, connection state)
```

`ranging` carries six fields and nothing else, so it stays a plain stream of
raw ranges:

```json
{"tag_label":"TAG1","anchor_label":"ANC1","range_m":0.98,"rssi":-78.5,
 "timestamp_sent":1099116851,"timestamp_received":1788439680.999}
```

**Order.** Each tag owns its own port and thread, so publishing from those
threads would interleave the tags by whichever port was read first — unrelated
to the order the exchanges happened on air. Section 3 gives frame *k* of every
superframe to initiator *k*, so readings are buffered and released in
`(superframe, frame)` order: tag frame 0's ranges, then frame 1's, then the
next superframe. A superframe is released as soon as every initiator has
reported it, otherwise after `SEQ_HOLD` (250 ms), so one tag going quiet
delays the stream rather than stalling it.

Everything that used to ride on `ranging` — superframe number, frame index,
lost slots, time of flight, first-path power, and the DS-TWR/SS-TWR flags — is
on `position/<tag>` under `detail`.

The live view is at `http://<host>:8765/` once running (or whatever
`--bind`/`--port` you chose).

## Provisioning

A board's configuration lives only in its own flash, set over serial, so a
reflash or an erase loses it. `provision.py` is the record of the working
setup and re-applies it:

```sh
./provision.py --verify     # report each board's state, hash and eq 12/13/14
./provision.py --dry-run    # show what would be sent, change nothing
./provision.py              # provision every attached board
```

Edit the `BOARDS`, `TIMING` and `LOCAL` tables at the top rather than the
code. `TIMING` is the hashed set -- every node must carry identical values or
its Polls are rejected -- so **all boards have to be attached together**; the
script compares the hash across them at the end and says so if they diverge.

The timing there is not the specification's default. Its own values (`T_r0`
400, `dr` 300, `T_p0` 400) proved too aggressive for real DW1000 SPI at 8 MHz:
slot 0 could not arm its Response at all and the Report ran 82 us over budget,
which suppressed DS-TWR entirely in favour of the single-sided fallback. The
figures in `TIMING` came from `$MEASURE` and `$TABLEB` on the boards
themselves (section 13 step 5), against measured costs of 313 us to arm a
Response and 682 us to arm a Report. `frame_dur_us` is not hardcoded -- the
script asks `$TABLEB` what equation (14) requires and sets that.

`ant_delay_tx/rx` is Decawave's generic 16436, not a per-board calibration.
Until section 13 step 1 is done, pairs at one physical distance still read
0.81-1.00 m.

Two lighter viewers, both in this directory:

```sh
./watch_ranges.py     # straight off serial -- no broker, no HTTP
./watch_mqtt.py       # off the broker; --raw prints every message
```

## Notes

- `rssi` is a **real measurement** on the `dstwr` firmware: it is the received
  power the DW1000 itself reports via `dwt_readdiagnostics()`, carried in the
  protocol's Report. On the PANS builds, where no such register is exposed,
  it falls back to the free-space path-loss estimate in
  `estimate_rssi_dbm()`; `position/<tag>` distinguishes the two.
- `timestamp_received` is when this host parsed the line, and the host reads
  serial in chunks — a burst of superframes can share one parse instant.
  `timestamp_sent` is the tag's own clock and is the better epoch.
- `timestamp_sent` is the tag's own on-board clock at the moment of the fix;
  `timestamp_received` is when this host parsed the line.
- To run on a new device: copy this directory over, then point
  `--mqtt-host`/`--bind` at that box.
