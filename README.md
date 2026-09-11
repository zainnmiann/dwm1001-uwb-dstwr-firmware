# UWB DS-TWR — Initiator-Driven Fixed-Slot Architecture (DW1000)

Firmware for Decawave/Qorvo **DWM1001-DEV** modules implementing revision 0.1 of
the *UWB DS-TWR Protocol Specification*.

| spec | implemented in |
|---|---|
| §3 timing hierarchy | `timing.c`, `role_initiator.c` |
| §4 ranging estimator, eq (4)–(7) | `ranging.c` |
| §5 message formats | `proto.h` |
| §6 initiator frame synchronization, eq (8)–(10) | `sync.c` |
| §7 air-time model and delay tables, eq (11)–(14) | `timing.c` |
| §8 parameter set, persistence, consistency rules | `cfg.c` |
| §9 serial interface | `serial.c`, `output.c` |
| §10 node state machines | `role_initiator.c`, `role_responder.c` |
| §11 loss handling | flags in `output.h`, counters in `app.h` |
| §12 DW1000 library mapping | `radio.c` |

The DW1000 driver is the official **Decawave DW1000 API v04.00.06**, vendored
byte-for-byte from [`Decawave/dwm1001-examples`](https://github.com/Decawave/dwm1001-examples)
(commit `1b2a46e`). `make check-driver` verifies nothing under `vendor/deca_driver`
has drifted.

---

## 1. Architecture

Design invariants, straight from §1:

* Initiators transmit **Poll** and **Final**. Responders transmit **Response**
  and **Report**. Fixed, not configurable.
* Frame and slot length are constant for a configuration. Slot ownership is
  static and configured, never negotiated.
* Every transmission after the Poll is a delayed transmit at a precomputed
  device-time instant. No CSMA, no backoff, no discovery.
* **No beacon and no reference node.** Initiators derive their frame alarm from
  the overheard Polls of other initiators.

```
superframe T_F = N_I * T_f
| frame 0 | frame 1 | frame 2 | ... | frame N_I-1 |

one frame:
  Poll | Resp 0 .. Resp N_R-1 | Final | Rep 0 .. Rep N_R-1 | guard
   ^        ^                    ^        ^
   initiator responders          initiator responders
```

Transmission instants, all through delayed transmit:

```
t_B_T,resp,s = t_B_R,poll  + T_r0 + s*delta_r          (1)
t_A_T,final  = t_A_R,resp,N_R-1 + delta_r              (2)
t_B_T,rep,s  = t_B_R,final + T_p0 + s*delta_p          (3)
```

Because (1) is a delayed transmit, the responder knows `t_B_T,resp` *before* it
builds the frame, so the Response carries it — which is what makes the
initiator's single-sided fallback (7) possible from the Response alone.

### Who computes what

The **responder** owns all four quantities of (6) once the Final arrives. It
computes `tau` and sends it in its Report slot. The **initiator** consumes the
Report and applies the antenna-delay correction locally. Only if the Report is
lost does the initiator fall back to (7) and set flag bit 0.

```
T_rd1 = t_A_R,resp,s - t_A_T,poll     T_rp1 = t_B_T,resp - t_B_R,poll     (4)
T_rd2 = t_B_R,final  - t_B_T,resp     T_rp2 = t_A_T,final - t_A_R,resp,s  (5)

tau = (T_rd1 T_rd2 - T_rp1 T_rp2) / (T_rd1 + T_rd2 + T_rp1 + T_rp2)       (6)
```

The residual clock-induced bias is `(e_A + e_B)/2 * tau`, under a millimetre for
|e| < 40 ppm — which is exactly why `T_r0` and `T_p0` are sized purely from
processing time here and not from clock stability.

### Synchronization without a manager (§6)

```
Lambda(k,m) = (k - m) mod N_I                                             (8)
t_next      = t_R - tau_hat - T_ISR + Lambda(k,m) T_f                     (9)
```

Every later Poll supersedes the earlier estimate. Cold start listens for `2 T_F`;
hearing nothing, the node starts at its local epoch and lets the others align to
it. `Lambda = 0` from a foreign source address is a duplicate frame index: it
raises a configuration fault and suppresses transmission. Losing every peer for
`sync_mmax` frames drops to free-run on the tracked drift `beta`, and an alarm
error beyond `T_g/2` sends the node back to listening.

Responders keep no frame alarm at all and never transmit unsolicited. A
responder that misses a Poll stays silent for that frame, leaving the remaining
slots untouched.

---

## 2. Where the spec is ambiguous, and what was done

Four things in revision 0.1 could not be implemented as literally written. Each
is resolved conservatively and flagged in the code.

**§5 message totals.** The field matrix and the "Total at N_R = 4" row disagree.
Summing the marked fields gives Poll 17, Resp 26, Final 46, Report 23; the totals
row says 17 / 25 / 44 / 23. Poll and Report agree, Resp and Final are 1 and 2
bytes short. The field matrix is unambiguous and self-consistent, so it is what
is implemented.

**§5 mask width vs §8.1 range.** The Response reception mask is one byte, but
`resp_count` runs to 12, which one byte cannot address. The field is one byte for
`N_R <= 8` — keeping the §5 byte counts exact at the tabulated `N_R = 4` — and two
bytes above that.

**§7.1 model vs §7.2 table.** Equation (11) includes `eta_RS = 1 + 48/330`, but
Table A's data column is plain `8(n_b+2)/R` with no coding overhead (and the
110 kbps rows carry a further ~3.4% that neither form explains). Equation (11) is
normative and is the larger of the two, so it is implemented as written — being
conservative is the safe direction for slot sizing. Table A is shipped verbatim
as reference data, dumpable with `$TABLEA` for comparison against measurement.

**§9 example checksums.** None of `$CFG*4A`, `...*7C`, `...*3D` matches an XOR
over the payload between `$` and `*` — they compute to `42`, `01`, `0E`. The
stated rule is normative and is what is implemented; the printed values read as
placeholders.

**§7.4's `T_proc = 60 us` is an example, not a requirement.** The section is
titled *"Worked reference point"* and says *"we obtain"*, and `T_proc` / `T_ISR` /
`T_margin` appear nowhere in §8.1's parameter table — they are design-time
variables, not configurable keys. §7.2 and §13 step 5 are explicit that the
tables are to be filled *from measurement*: *"Fill Table A by measurement, then
recompute Table B and (14)."* So this firmware exposes them as extension keys
(`proc_us` and friends), ships them at measured rather than illustrative values,
and provides `$MEASURE` to take the device-time captures §7.2 names. Copying
60/15/25 verbatim would contradict §13 step 5.

**§12's "absent" facilities are all present.** It lists five to be added as thin
register accesses; in API v04.00.06 **all five are already exposed**, so no
register-level additions were needed (see the header of `radio.c`).

**Polled, not interrupt-driven — and that conforms.** `T_ISR` and `J_ISR` appear
only as additive budget terms in eq (9), (10) and (12); the words
"interrupt-driven" and "polled" appear nowhere, and §12's list of *required*
library functions omits interrupt handling entirely. A polled design that folds
its own service latency into `T_proc` and satisfies eq (10) and (14) is
conformant.

---

## 3. Timing, computed

Reproducing §7.4's worked reference point (C1, `N_R = 4`, `T_proc = 60`,
`T_ISR = 15`, `T_margin = 25`, `T_g = 200`):

| | spec | this build |
|---|---|---|
| `delta_r` | ~300 us | 295 us |
| `delta_p` | ~290 us | 291 us |
| `T_f` (14) | ~3.7 ms | 3.861 ms |
| configured | 4.0 ms | 4.0 ms |

`delta_r` differs by 5 us because the real Response frame is 26 B, not Table A's
32 B assumption. §7.5's rate table reproduces exactly — 250 / 125 / 62.5 / 41.7 /
31.3 / 20.8 / **15.6 Hz** across `N_I`, and the `D=2` and `D=4` columns with it.

At the deployment size you asked about, **16 initiators x 9 responders** on C1:

| | `T_f` | `T_F` | rate at `D=1` | Final |
|---|---:|---:|---:|---:|
| `report_enable=1` | 6808 us | 108.9 ms | **9.18 Hz** | 72 B |
| `report_enable=0` | 3889 us | 62.2 ms | **16.07 Hz** | 72 B |

Dropping the Report group nearly doubles the rate. The cost is that the initiator
never receives `tau` and falls back to (7) for every measurement — accurate
ranging moves off the initiator and onto whatever consumes the responders'
output. That is the trade §8.1 puts behind one flag.

Equation (10) wants `T_g >= 18 us` at 20 ppm and `N_I = 16`; the configured
200 us default is set by service jitter, not the crystal, exactly as §6 says.

`$TABLEB` now reports eq (10), (12), (13) and (14) as explicit pass/fail. Only
(14) is a hard rule (§8.2 rule 1, error code 3); the others are advisory, because
a configuration can be legal yet still run tighter slot pitches than the measured
processing budget supports.

### Timing is measured, not assumed

The shipped defaults are §8.1's literal values (`T_f` 4000, `T_r0` 400,
`delta_r` 300, `T_p0` 400, `delta_p` 290, `T_g` 200). Whether they hold on a
given board is a question for §13 step 5, not for this table:

```
$MEASURE            # what the critical paths actually cost, vs their budgets
$SET,proc_us,<max observed>
$AUTOTIME           # recompute Table B and (14) from that
$TABLEB             # confirm eq (10)/(12)/(13)/(14)
```

An earlier build of this firmware could not meet `T_r0 = 400 us` at all — every
delayed transmit missed its slot. The cause was not the protocol but the SPI
driver: `port_platform.c` registers a completion handler, and with EasyDMA
disabled the SDK takes its per-byte interrupt path, so a 25-byte
`dwt_writetxdata` cost ~25 interrupts at the lowest priority. `SPI1_USE_EASY_DMA`
is now 1 (`config/sdk_config.h`), making each transfer one DMA burst and one
interrupt. `$MEASURE` is how you confirm what that bought on your hardware.

---

## 4. Build and flash

Needs `arm-none-eabi-gcc` (tested with Arm GNU Toolchain 13.2.rel1) and `make`.
nRF5 SDK 14.2.0 is vendored.

```sh
make                  # -> _build/dwm1001_tdma_twr.hex / .bin
make check-driver     # verify vendor/deca_driver == upstream
make flash            # nrfjprog
make flash-jl         # JLinkExe, no nrfjprog needed
make erase            # required when coming from factory PANS firmware
```

Current image: ~39 kB flash, ~7 kB RAM. Configuration occupies 708 B of the last
flash page, so it survives a reflash.

Selecting one board out of several, coming from the factory PANS image, and
talking to a board over serial by hand are covered in
[FLASHING_AND_CALIBRATION.md](FLASHING_AND_CALIBRATION.md).

---

## 5. Serial interface (§9)

`$` + payload + `*` + XOR checksum over the payload, CRLF terminated. Default
921600 8N1 on the DWM1001-DEV J-Link VCOM. The checksum is verified on input when
present and always emitted on output.

```
$SET,<key>,<value>*CS      set one parameter (RAM)
$GET,<key>*CS              read one parameter
$CFG*CS                    dump the full configuration
$SAVE*CS                   commit RAM configuration to non-volatile memory
$LOAD*CS                   reload from non-volatile memory
$DEFAULT*CS                restore factory defaults
$ANCHOR,<i>,<id>,<slot>,<x>,<y>,<z>,<ant>,<en>*CS
$TAG,<j>,<id>,<frame>,<div>,<en>*CS
$LIST,ANCHOR|TAG*CS
$RANGE,START*CS            needed when autostart = 0
$RANGE,STOP*CS             stop ranging, radio to idle
$STAT*CS                   frames, losses, timeouts, desync events
```

Extensions, needed to work §7 on real hardware:

```
$TABLEA*CS      Table A as published, plus the model and measured values
                for the loaded configuration
$TABLEB*CS      equations (12)-(14) evaluated now, required vs configured T_f
$RATE*CS        the §7.5 rate table for the loaded configuration
$MEASURE*CS     section 7.2 device-time captures: what the Poll->Response,
                Final->Report and alarm->Poll paths actually cost on this
                hardware, against the T_r0 / T_p0 budgets they must fit
$AUTOTIME*CS    write the Table B recommendation into the timing keys
$RESET*CS       reboot
```

Responses are `$OK,<key>,<value>*CS` and `$ERR,<code>,<key>,<reason>*CS` with the
§9.2 codes: 1 unknown key, 2 out of range, 3 inconsistent with (14), 4 illegal
preamble code for channel/PRF, 5 read-only while ranging is active, 6
non-volatile write failure.

### Ranging stream (§9.4)

```
$RNG,<n>,<frame_idx>,<tag_id>,<anchor_id>,<slot>,<d_mm>,<tof_ps>,<rxp_dbm>,<fpp_dbm>,<flags>,<t_local_us>*CS
```

Flags: bit0 SS-TWR fallback used, bit1 suspected NLOS (`P_RX - P_FP > 10 dB`),
bit2 Response lost, bit3 Report lost, bit4 desynchronized, bit5 configuration
hash mismatch.

`out_format` selects `CSV` (above), `JSON` (one object per line, unframed) or
`BIN` (`A5 5A | id | len | payload | crc8`). `out_mask` gates range (bit0),
diagnostics (bit1), position (bit2) and raw timestamps (bit3, emitted as `$TS`).

---

## 6. Bring-up (§13)

The spec's order is the right one, and steps 4 and 5 are the ones that actually
matter here.

1. Lock the PHY configuration; calibrate `delta_A` and `delta_B_s` at a surveyed
   baseline (`ant_delay_tx` / `ant_delay_rx`, and the per-responder `ant` trim in
   the anchor list). Step-by-step, with the arithmetic and the units:
   [FLASHING_AND_CALIBRATION.md &sect;4](FLASHING_AND_CALIBRATION.md#4-calibrating-the-antenna-delay).
   Not yet done on this fleet — every board carries the generic 16436.
2. Verify the §12 facilities on two nodes — already present in the driver, so
   this reduces to confirming delayed TX/RX behave.
3. One initiator, one responder. Record bias and standard deviation vs distance.
4. One initiator, `N_R` responders. **On a logic analyser**, confirm no Response
   or Report slot overlaps.
5. Fill Table A by measurement (`$SET,air_resp_ns,...`), then `$TABLEB` and
   `$AUTOTIME` to recompute (12)–(14).
6. Two initiators. Verify the rearm rule (9) and measure the residual alarm error
   against `T_g` (`$STAT` reports `err_us` and `beta_ppb`).
7. `N_I` initiators; free-run, recovery, duplicate-index and hash-mismatch faults.
8. Serial interface, non-volatile storage, autostart, output stream.

### Provisioning a 16 x 9 cell

`frame_dur_us` defaults to 4000, which fails rule 1 at `N_R = 9` — `$SAVE` will
return `$ERR,3` until the timing is recomputed. That is the consistency check
doing its job.

```
# every node
$SET,resp_count,9
$SET,init_count,16
$AUTOTIME
$SAVE

# responder s
$SET,role,RESP
$SET,node_id,0x0100
$SET,slot_index,0
$SAVE
$RESET

# initiator k
$SET,role,INIT
$SET,node_id,0x0001
$SET,frame_index,0
$SAVE
$RESET
```

Slot and frame indices must be contiguous from 0. All radio and timing
parameters must be identical on every node — the configuration hash carried in
the Poll enforces this at run time, and a mismatched Poll is dropped with
`hash_mismatch` counted.

A third role, `LSTN`, disables frame filtering and reconstructs the ranging
stream from overheard Reports — a passive gateway that takes no part in the
schedule.

---

## 7. Not implemented

Both omissions are exercises of permission the spec grants explicitly, not
shortfalls against it.

**The position solver.** §9.4's `$POS` line and `out_mask` bit 2 are wired up and
the anchor list carries `x,y,z`, but no multilateration runs on the node. The
spec marks it optional three times — *"An **optional** position line"*,
*"**when** the solver runs on the node"*, and §10.1's *"optional solver -> $POS"*.
`out_position()` is the hook.

**BLE and WiFi transports.** §8.1 says the non-USB transports *"are optional and
are selected by the developer according to the available hardware"*.
`iface_cfg` / `iface_data` accept and store all four values so configuration
round-trips, but the DWM1001 carries no WiFi radio and BLE would need a
SoftDevice. Selecting either emits `$ERR,2` at boot and
falls back to UART. `USB` and `UART` both resolve to the J-Link VCOM — the module
has no USB device controller of its own.

---

## 8. Licensing

`vendor/deca_driver` is Decawave/Qorvo — see `vendor/terms_of_use.txt` and
`vendor/deca_driver/disclaimer.txt`. `vendor/nRF5_SDK_14.2.0` is Nordic
Semiconductor — see its `license.txt`. Both are redistributed unmodified.
