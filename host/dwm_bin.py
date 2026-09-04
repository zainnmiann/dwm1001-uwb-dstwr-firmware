"""Decoder for the firmware's binary $RNG stream (out_format = BIN).

Why bother: a text record is ~57 bytes, so 8 anchors at 21 Hz costs 9.7 kB/s
against the 11.5 kB/s that 115200 8N1 can carry. At 84% sustained duty the
UART TX ring never fully drains between frame bursts, and the tail of the last
record -- including its CRLF -- gets dropped, which leaves the next frame's
first record with nowhere to begin. Two of eight slots then vanish from every
view and it reads as a dead anchor. A binary record is 25 bytes: 4.3 kB/s,
37% duty, and room for the 16 anchors the slot map could carry.

Framing, from bin_emit() in src/output.c:

    A5 5A <id> <len> <payload...> <crc8>

id 0x01 is a range record, 0x02 a position record. CRC-8 covers the payload
only -- poly 0x07, init 0xFF, no final xor. All multi-byte fields are
little-endian.
"""
import struct

SYNC = b"\xa5\x5a"
ID_RNG = 0x01
ID_POS = 0x02

# sfn u16, frame u8, tag u16, anchor u16, slot u8, d_mm i32, tof_ps u32,
# rxp i16, fpp i16, flags u8, t_us u32  -> 25 bytes
_RNG = struct.Struct("<HBHHBiIhhBI")
RNG_LEN = _RNG.size

FLAG_NAMES = ((0x01, "ss_fallback"), (0x02, "nlos"), (0x04, "resp_lost"),
              (0x08, "report_lost"), (0x10, "desync"), (0x20, "hash_mismatch"))


def crc8(data):
    """Poly 0x07, init 0xFF -- matches crc8() in src/output.c."""
    c = 0xFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def flag_names(flags):
    return [n for bit, n in FLAG_NAMES if flags & bit]


class Decoder:
    """Feed it bytes, get back decoded records.

    Tracks bad_crc and resyncs separately from good records: the whole point of
    moving to binary was that dropped output was being discarded in silence, so
    a decoder that hides its own losses would repeat the mistake.
    """

    def __init__(self):
        self.buf = bytearray()
        self.records = 0
        self.bad_crc = 0
        self.resyncs = 0        # bytes thrown away hunting for the next sync
        # Opening a live port lands mid-record, so the bytes before the first
        # sync word are always discarded and mean nothing. Counting them as
        # loss would cry wolf on every single run, which is how a warning
        # stops being read.
        self.synced = False
        self.startup_bytes = 0

    def feed(self, data):
        """Append bytes; yield one dict per complete, CRC-valid range record."""
        self.buf += data
        out = []
        while True:
            i = self.buf.find(SYNC)
            if i < 0:
                # Keep one byte in case a sync word straddles the boundary.
                if len(self.buf) > 1:
                    if self.synced:
                        self.resyncs += len(self.buf) - 1
                    else:
                        self.startup_bytes += len(self.buf) - 1
                    del self.buf[:-1]
                break
            if i:
                if self.synced:
                    self.resyncs += i
                else:
                    self.startup_bytes += i
                del self.buf[:i]
            if len(self.buf) < 4:
                break
            rid, rlen = self.buf[2], self.buf[3]
            if len(self.buf) < 4 + rlen + 1:
                break                       # payload still arriving
            payload = bytes(self.buf[4:4 + rlen])
            crc = self.buf[4 + rlen]
            del self.buf[:4 + rlen + 1]

            if crc8(payload) != crc:
                self.bad_crc += 1
                continue
            if rid != ID_RNG or rlen != RNG_LEN:
                continue                    # position records etc.
            out.append(self._range(payload))
            self.records += 1
            self.synced = True
        return out

    @staticmethod
    def _range(p):
        (sfn, frame, tag, anchor, slot, d_mm, tof_ps,
         rxp, fpp, flags, t_us) = _RNG.unpack(p)
        return {
            "sfn": sfn, "frame": frame,
            "tag_id": f"{tag:04X}", "anchor_id": f"{anchor:04X}",
            "slot": slot,
            "range_m": d_mm / 1000.0,
            "tof_ps": tof_ps,
            # The firmware carries dBm x 10 to keep the wire integral.
            "rssi": rxp / 10.0,
            "first_path_dbm": fpp / 10.0,
            "flags": flags, "flag_names": flag_names(flags),
            "t_us": t_us,
        }

    def losses(self):
        """Input lost after the stream was synced -- real loss only.

        startup_bytes is reported separately: it is the unavoidable partial
        record from opening a live port, not a fault.
        """
        return {"bad_crc": self.bad_crc, "resync_bytes": self.resyncs,
                "startup_bytes": self.startup_bytes}

    def lost(self):
        """Total real loss, for a one-number health check."""
        return self.bad_crc + self.resyncs
