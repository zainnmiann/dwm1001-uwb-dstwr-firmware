#!/usr/bin/env python3
"""Live view of the UWB range stream off the MQTT broker.

    ./watch_mqtt.py                      localhost
    ./watch_mqtt.py --host 10.0.0.5      another broker
    ./watch_mqtt.py --raw                print every message instead of a table
"""
import argparse, collections, json, statistics, time
import paho.mqtt.client as mqtt

PAIRS = collections.OrderedDict()          # (tag, anchor) -> deque
FLAGS = collections.Counter()
LAST = {}
WIN = 400


def on_msg(_c, args, msg):
    try:
        d = json.loads(msg.payload)
    except ValueError:
        return
    parts = msg.topic.split("/")
    if args.raw:
        print(f"{msg.topic}  {json.dumps(d, separators=(',', ':'))}")
        return
    if len(parts) >= 4 and parts[1] == "ranging":
        key = (d.get("tag_label", parts[2]), d.get("anchor_label", parts[3]))
        PAIRS.setdefault(key, collections.deque(maxlen=WIN)).append(
            (time.time(), d.get("range_m"), d.get("rssi")))
        for f in d.get("flag_names") or []:
            FLAGS[f] += 1
    elif len(parts) >= 3 and parts[1] == "position":
        LAST[d.get("tag_label", parts[2])] = d


def draw(t0, host):
    now = time.time()
    print("\033[H\033[J", end="")
    print(f"  MQTT  {host}:1883   topic uwb/#      {now - t0:6.1f} s")
    print(f"  {'tag':6s} {'anchor':7s} {'n':>7s} {'Hz':>7s} {'mean m':>9s} "
          f"{'sd cm':>8s} {'rssi':>8s}")
    print("  " + "-" * 58)
    if not PAIRS:
        print("  no ranging messages yet -- is localize.py running?")
    for (tag, anch), dq in PAIRS.items():
        rs = [x[1] for x in dq if x[1] is not None]
        rx = [x[2] for x in dq if x[2] is not None]
        recent = [x for x in dq if now - x[0] < 3.0]
        print(f"  {tag:6s} {anch:7s} {len(dq):>7d} {len(recent) / 3.0:>7.1f} "
              f"{(statistics.mean(rs) if rs else float('nan')):>9.3f} "
              f"{(statistics.stdev(rs) * 100 if len(rs) > 1 else float('nan')):>8.2f} "
              f"{(statistics.mean(rx) if rx else float('nan')):>8.1f}")
    for tag, d in LAST.items():
        bits = [f"sfn {d.get('sfn')}", f"fw {d.get('fw')}"]
        if d.get("lost_slots"):
            bits.append(f"lost slots {d['lost_slots']}")
        if d.get("lsq"):
            bits.append(f"fix ({d['lsq']['x']}, {d['lsq']['y']})")
        print(f"\n  {tag}: " + "  ".join(str(b) for b in bits))
    if FLAGS:
        print("\n  flags: " + ", ".join(f"{k}={v}" for k, v in sorted(FLAGS.items())))
    print("\n  Ctrl-C to stop.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--root", default="uwb")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--secs", type=float, default=0.0)
    a = ap.parse_args()

    c = (mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
         if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client())
    c.user_data_set(a)
    c.on_message = on_msg
    c.on_connect = lambda *x: print(f"  connected to {a.host}:{a.port}")
    c.connect(a.host, a.port, 30)
    c.subscribe(f"{a.root}/#", qos=0)
    c.loop_start()

    t0 = time.time()
    try:
        while not a.secs or time.time() - t0 < a.secs:
            if not a.raw:
                draw(t0, a.host)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        c.loop_stop()
        if not a.raw:
            draw(t0, a.host)


if __name__ == "__main__":
    main()
