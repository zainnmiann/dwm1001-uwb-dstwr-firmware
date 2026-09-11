# Flashing and calibrating a board by hand

Everything here is done one board at a time over USB, without the host scripts.
`host/provision.py` automates the configuration half and is the right tool for a
fleet; this document is for a single board, for bring-up, and for the antenna
delay calibration that no script covers yet.

- [1. What you need](#1-what-you-need)
- [2. Flashing](#2-flashing)
- [3. Talking to a board](#3-talking-to-a-board)
- [4. Calibrating the antenna delay](#4-calibrating-the-antenna-delay)
- [5. Board map](#5-board-map)

---

## 1. What you need

| | |
|---|---|
| Toolchain | Arm GNU Toolchain 13.2.rel1 (`arm-none-eabi-gcc`), `make` |
| Programmer | The DWM1001-DEV's on-board J-Link, over USB |
| Flash tool | `JLinkExe` (SEGGER J-Link software pack) **or** `nrfjprog` (nRF Command Line Tools) |
| Serial | Any terminal at **115200 8N1** — `screen`, `minicom`, `picocom`, or Python `pyserial` |

The nRF5 SDK and the Decawave driver are vendored, so nothing else needs
installing.

---

## 2. Flashing

### Build

```sh
make                  # -> _build/dwm1001_tdma_twr.hex and .bin
make check-driver     # optional: verify vendor/deca_driver matches upstream
```

### One board connected

```sh
make flash-jl         # JLinkExe
make flash            # nrfjprog, if you have it instead
```

### Several boards connected at once

Both tools will pick a device arbitrarily when more than one J-Link is
attached, so select the board explicitly by its serial number. The serial is
printed on the board and is the same number the USB port enumerates with.

```sh
HEX="$(pwd)/_build/dwm1001_tdma_twr.hex"
SN=760131590                       # the board you want

printf 'device NRF52832_XXAA\nsi 1\nspeed 4000\nr\nh\nloadfile %s\nr\ng\nqc\n' \
  "$HEX" > /tmp/flash.jlink

JLinkExe -SelectEmuBySN "$SN" -CommanderScript /tmp/flash.jlink
```

A good run ends with `O.K.` after `Downloading file` and then
`Script processing completed`.

With `nrfjprog` the equivalent is:

```sh
nrfjprog -f nrf52 --snr "$SN" --program _build/dwm1001_tdma_twr.hex --sectorerase --verify
nrfjprog -f nrf52 --snr "$SN" --reset
```

### What survives a flash

**Configuration is preserved.** It lives in the last flash page, outside the
application image, and both commands above write only the regions the hex
covers. A reflashed board keeps its role, slot index, antenna delay and
everything else — which is why the anchors did not need re-provisioning after
the last firmware update.

Two things do reset, because they live in RAM:

- the `$STAT` counters,
- the frame synchronisation state, which re-acquires within a couple of seconds.

### Coming from the factory PANS firmware

A board still running Decawave's shipped firmware needs a full erase first,
otherwise the bootloader region interferes:

```sh
make erase            # nrfjprog -f nrf52 --eraseall
```

After an erase the board comes up with default settings and must be configured
from scratch — role, indices and network size — before it will join. That is
what `host/provision.py` is for.

### Verifying

Open a terminal on the board at 115200 and reset it. The boot banner reports
what is running:

```
$I,fw=1.0.0,proto=1,dev=0xDECA0130,part=0x...
$I,role=INIT,node=0x0F70,pan=0xDECA,hash=0x2C33
$I,txpower=0x...,otp,ant_tx=16436,ant_rx=16436
```

`hash` must match on every board in the cell or they will ignore each other.
`ant_tx` / `ant_rx` are the *effective* antenna delays, which is what section 4
is about.

---

## 3. Talking to a board

**115200 8N1.** The configuration default is 921600, but every board in this
fleet is provisioned to 115200 and the tooling assumes it. The J-Link VCOM does
not carry 921600 reliably in the board-to-host direction, so leave it at
115200.

```sh
screen /dev/cu.usbmodem0007601315901 115200      # ctrl-a k to quit
```

Commands are `$` + payload, terminated CRLF. The `*CS` checksum shown in the
main README is **verified when present but not required**, so you can type
commands bare:

```
$CFG                    dump the whole configuration
$GET,ant_delay_tx       read one parameter
$SET,ant_delay_tx,16436 set one parameter (RAM only)
$SAVE                   commit RAM to non-volatile memory
$LOAD                   discard RAM changes, reload what was saved
$STAT                   counters and sync state
$RANGE,STOP             stop ranging (needed before reconfiguring)
$RANGE,START            resume
```

A change is not persistent until `$SAVE`. If you get it wrong before saving,
`$LOAD` puts it back.

> **Stop ranging first.** A board streaming range records at 21 Hz will bury
> the reply to your command. `$RANGE,STOP`, do the work, `$RANGE,START`.

---

## 4. Calibrating the antenna delay

### What it is

Every board reports a distance that is offset by a fixed amount, caused by the
time a signal spends inside the antenna and RF front end. Correcting it is the
difference between *repeatable* readings and *correct* ones. The fleet is
currently repeatable to 2–3 cm but uncalibrated, so each pair carries an unknown
constant offset.

`ant_delay_tx` and `ant_delay_rx` hold that correction, in device time units.

| value | meaning |
|---|---|
| `16436` | Decawave's generic DWM1001 figure — what every board currently has |
| `65535` | "not calibrated": the board falls back to its own factory value from the chip, or to 16436 if the chip has none |
| anything else | an explicitly calibrated value |

**Set both registers to the same number.** They are not a pair to be split
between; each takes the full one-way delay for its own direction. Halving them
across the two under-compensates by roughly 77 m, which is exactly what
happened once on this hardware before it was corrected.

**Antenna delay is per-board.** It is not part of the configuration hash, so
changing it on one board does not break its compatibility with the rest of the
cell. Boards can be calibrated one at a time, and the anchors do not all need to
be present.

### The scale

One device time unit is 15.650040 ps, which is **4.692 mm** of range — so
roughly **213 units per metre**.

Because both ends of a measurement contribute, the effect depends on how many
boards you change:

| what you change | effect on the reported range |
|---|---|
| one board's delay, by 1 unit | ≈ 4.7 mm |
| both boards' delays, by 1 unit each | ≈ 9.4 mm |

**Increasing the delay decreases the reported range.** If a pair reads longer
than it truly is, the delay is too small.

### Step 0 — check for a factory value first (free)

The radio chip may carry its own calibrated antenna delay from the factory. If
it does, using it is better than any generic constant and costs no measurement
at all. To find out, on one board:

```
$RANGE,STOP
$SET,ant_delay_tx,65535
$SET,ant_delay_rx,65535
$SAVE
```

Reset the board and read the banner:

```
$I,txpower=0x...,ant_tx=?????,ant_rx=?????
```

- If `ant_tx` comes back as something other than **16436**, that is the chip's
  own factory figure. Leave it — that board is now calibrated per-part.
- If it comes back as exactly **16436**, the chip has no stored value and you
  need the measured procedure below.

Record what each board reports before moving on.

### Step 1 — measure the error at a known distance

You need one distance you actually trust. A tape measure is fine; ±5 mm is far
better than the error being chased.

1. Place one tag and one anchor **antenna face to antenna face**, clear line of
   sight, nothing within about half a metre of the path, both at the same
   height. Roughly 5 m apart is a good choice — see the note below on why.
2. Write down the true distance.
3. Let them range for **30 seconds** and take the **mean** reported distance,
   not a single reading. Any of these will give it to you:

```sh
cd host
python3 raw.py                       # live, one line per measurement
python3 capture.py --secs 30 --out /tmp/cal && python3 analyse_capture.py /tmp/cal
```

The second prints a mean and standard deviation per pair. The standard deviation
should be 2–3 cm; if it is much worse, fix the setup before calibrating, because
the reading is not stable enough to calibrate against.

### Step 2 — work out the correction

```
error  = mean reported − true distance          (metres)
units  = error × 1000 / 9.384                   (adjusting BOTH boards)
new    = current delay + units                  (apply to both boards)
```

Worked example — true 5.000 m, reported 5.187 m, both boards at 16436:

```
error = +0.187 m  = +187 mm       (reads long, so the delay is too small)
units = 187 / 9.384 = 20
new   = 16436 + 20 = 16456        on both boards
```

### Step 3 — apply and save

On **each** of the two boards:

```
$RANGE,STOP
$SET,ant_delay_tx,16456
$SET,ant_delay_rx,16456
$SAVE
$RANGE,START
```

Re-measure. The reading should now sit within the standard deviation of the true
distance. One iteration is usually enough; a second will close any remainder.

### Step 4 — confirm the scale is behaving (recommended)

Before trusting a value across the fleet, check that the hardware actually moves
the way the arithmetic says. Change one board by a large, deliberate amount —
say **+213 units**, which should shorten that pair by about **1 m** — measure,
then put it back.

If the observed shift is close to 1 m, the relationship holds and your
calibration is sound. If it is not, stop: the scale is not what is assumed and
any value derived from it will be wrong. This check takes two minutes and it is
the one that catches a misunderstanding before it propagates to ten boards.

### Step 5 — the rest of the fleet

The measurement above fixes the *sum* of two boards' offsets, so it cannot tell
you how much belonged to each. Two ways forward:

**Quick — one value everywhere.** Apply the calibrated number to all boards. It
removes the bulk of the error and takes one measurement. It leaves the
board-to-board differences, which on this fleet showed up as two tags reading the
same anchor up to 28 cm apart.

**Better — one reference board.** Nominate a board as the reference and never
change it. For every other board, set up one surveyed distance to the reference
and adjust **only that board** until it reads true (using 4.692 mm per unit,
since only one end is moving). Nine measurements for a ten-board fleet, and it is
what makes different tags agree on the same anchor.

### Record what you set

Antenna delays live only in each board's flash and are written down nowhere else.
A `$DEFAULT`, a full erase, or a fresh provisioning run will wipe them. Keep a
note — board serial, the value, the distance it was measured at, and the date —
and be aware that **`host/provision.py` currently writes 16436 to every board it
touches**, so it will overwrite a calibration until that is changed.

### A caveat worth knowing

The reported distance also carries a smaller error that varies *with distance*
rather than being constant — roughly 19 cm across the 0.15–6 m span, strongest
at close range. The antenna delay cannot absorb it, because it is not a constant.

Two practical consequences:

- **Calibrate at a distance near where the system will actually operate.** A
  delay fitted at 1 m will be off by a little more than a decimetre at 5 m, and
  the other way round.
- If accuracy matters across a wide span, calibrate at a **near and a far**
  distance and treat the difference between them as this second term rather than
  folding it into the delay.

---

## 5. Board map

| Serial | Label | Role | Index |
|---|---|---|---|
| 760131590 | TAG1 | initiator | 0 |
| 760131633 | TAG2 | initiator | 1 |
| 760131573 | ANC1 | responder | 0 |
| 760131584 | ANC2 | responder | 1 |
| 760131626 | ANC3 | responder | 2 |
| 760217925 | ANC4 | responder | 3 |
| 760217915 | ANC5 | responder | 4 |
| 760217943 | ANC6 | responder | 5 |
| 760217956 | ANC7 | responder | 6 |
| 760217975 | ANC8 | responder | 7 |

All ten share configuration hash `0x2C33`. The serial number is both the J-Link
selector (`-SelectEmuBySN`) and the USB port name — on macOS the tag above
appears as `/dev/cu.usbmodem0007601315901`.
