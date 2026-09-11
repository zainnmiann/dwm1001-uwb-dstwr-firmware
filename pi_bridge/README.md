# UWB Range Bridge — Raspberry Pi Zero 2 W

A headless bridge that reads tag-to-anchor ranges off the Pi's GPIO UART and
publishes them to MQTT, one message per anchor-range. It runs as a systemd
service, so the Pi comes up streaming on power alone with no login.

This is the deployment used on the buoy, where the Pi and the tag run on
independent power and there is no host computer attached.

## Layout

| file | purpose |
|---|---|
| `uwb_bridge.py` | the bridge process |
| `uwb-bridge.service` | systemd unit |
| `uwb-bridge.env` | runtime configuration, installed to `/etc/default/uwb-bridge` |
| `install.sh` | frees the GPIO UART, installs files, enables the service |
| `uwb_anchors.json` | surveyed anchor coordinates in metres, keyed by anchor number |

## What it publishes

One message per anchor-range:

```
topic:   buoy/uwb/ranging/<tag_label>/<anchor_label>
```
```json
{"source": "buoy", "tag_label": "T05", "anchor_label": "A03",
 "range_m": 15.517, "rssi": null,
 "timestamp_sent": 79610174, "timestamp_received": 1789133031.732}
```

`timestamp_sent` is the tag's own clock in microseconds; `timestamp_received`
is Unix time on the Pi. `rssi` is always `null` — no per-range RSSI is exposed
over this interface.

It also retains `buoy/uwb/ranging/status`, set to `{"online": false}` by the
MQTT will, so a dead Pi is visible to subscribers rather than merely silent.

Anchor labels are resolved by matching the coordinates the tag reports against
`uwb_anchors.json`, so a new anchor labels itself with no hex-address table to
maintain. The per-update anchor indices are reordered every sample, so ranges
are keyed by anchor address and never by index.

## Wiring

| DWM1001-DEV | Raspberry Pi |
|---|---|
| `P0.05` (TX) | pin 10, `GPIO15` / RXD |
| `P0.11` (RX) | pin 8, `GPIO14` / TXD |
| `GND` | pin 6 |

Power the module from **5 V**, not the Pi's 3V3 rail. UWB bursts sharing a rail
with WiFi bursts is what browns the module out, and a browned-out module loses
its configuration back to factory defaults: it returns as a *passive* node,
never initiates ranging, and so goes completely silent while looking healthy.
The bridge detects and repairs that state on startup, but the fix is electrical.

Do not leave the module's USB attached to a host while the Pi is wired — the
on-board debugger's virtual COM port shares `P0.05`/`P0.11` with the header
pins, and two drivers on the same line corrupt each other's bytes.

## Install

```sh
scp -r pi_bridge <user>@<pi-address>:~/
ssh <user>@<pi-address>
cd ~/pi_bridge && sudo bash install.sh
sudo reboot                 # required: the UART change is applied at boot
```

`install.sh` removes the serial console from `GPIO14/15` (it would otherwise
fight the tag for those pins), enables the UART, hands the stable PL011 to the
GPIO header, adds the service user to `dialout`, and enables the unit.

## Running it

```sh
sudo systemctl start uwb-bridge       # start
sudo systemctl stop uwb-bridge        # stop
sudo systemctl restart uwb-bridge     # restart, e.g. after editing config
systemctl status uwb-bridge           # current state
sudo journalctl -u uwb-bridge -f      # live log
sudo systemctl enable  uwb-bridge     # start at boot (install.sh does this)
sudo systemctl disable uwb-bridge     # stop starting at boot
```

In the foreground, for debugging — stop the service first, or the two fight
over the serial port:

```sh
sudo systemctl stop uwb-bridge
set -a; . /etc/default/uwb-bridge; set +a
python3 /opt/uwb/uwb_bridge.py
```

Confirm messages are arriving, from any machine that can reach the broker:

```sh
mosquitto_sub -h <broker-ip> -p 1883 -t 'buoy/uwb/ranging/#' -v
```

## Configuration

Edit `/etc/default/uwb-bridge`, then `sudo systemctl restart uwb-bridge`.

| variable | default | meaning |
|---|---|---|
| `MQTT_HOST` / `MQTT_PORT` | — / `1883` | broker address |
| `MQTT_USER` / `MQTT_PASS` | unset | credentials, if the broker needs them |
| `MQTT_TOPIC_ROOT` | `buoy/uwb/ranging` | topic prefix |
| `UWB_PORT` / `UWB_BAUD` | `/dev/serial0` / `115200` | serial port |
| `UWB_SOURCE` | `buoy` | `source` field in every payload |
| `UWB_ANCHORS` | `/opt/uwb/uwb_anchors.json` | surveyed anchor coordinates |
| `UWB_TAG_LABEL` | unset | pin the tag label; unset reads it from the tag |
| `UWB_RATE` | `1` | update rate in units of 100 ms: `1` = 10 Hz, `10` = 1 Hz |

## SSH access to the Pi

```sh
ssh <user>@<pi-address>
```

The Pi advertises itself over mDNS, so its hostname works without knowing the
address, provided `avahi-daemon` is installed and you are on the same network:

```sh
ssh <user>@<hostname>.local
```

For unattended access — which the service does not need, but deployment does —
install a key so no password is required:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_pi -N ""
ssh-copy-id -i ~/.ssh/id_ed25519_pi.pub <user>@<pi-address>
ssh -i ~/.ssh/id_ed25519_pi <user>@<pi-address>
```

If the Pi cannot be found on the network, its WiFi is configured on the FAT
boot partition of its microSD, which is readable from any machine — useful when
the Pi itself is unreachable. Note that a Pi Zero 2 W has a **2.4 GHz-only**
radio and cannot associate with a 5 GHz-only SSID, and that an access point with
client isolation enabled will let the Pi reach the internet while silently
blocking the broker on the same subnet.

## Troubleshooting

| symptom | cause |
|---|---|
| `no stream after enabling les` in the log | the module browned out and returned as a passive node; the bridge issues the recovery itself, but check the 5 V supply has current headroom |
| service runs, no MQTT messages | broker unreachable from the Pi — check with `ping <broker-ip>`; on an isolated access point the gateway answers but LAN peers do not |
| `Permission denied` on `/dev/serial0` | the service user is not in `dialout`; re-run `install.sh` and log out and back in |
| ranges only from some anchors | expected — the tag ranges against a limited set of anchors per update and rotates which ones, so a distant anchor appears intermittently |
| rate stuck at 1 Hz | `UWB_RATE` is applied only when the port is quiet at startup, since commands cannot be issued reliably into a live stream; stop the service, then start it |
