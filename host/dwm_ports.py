"""Platform-independent discovery of DWM1001-DEV boards.

Device names differ per OS -- /dev/cu.usbmodem* on macOS, /dev/ttyACM* on Linux
(Jetson, Raspberry Pi) -- so matching on the name is fragile. Every DWM1001-DEV
exposes its UART through an on-board SEGGER J-Link OB, so match that by USB
VID:PID instead and the same code works everywhere.
"""
from serial.tools import list_ports

JLINK_VID = 0x1366        # SEGGER
JLINK_PID = 0x0105        # J-Link OB


def dwm_ports():
    """Every attached DWM1001-DEV, as pyserial ListPortInfo objects."""
    found = [p for p in list_ports.comports()
             if p.vid == JLINK_VID and p.pid == JLINK_PID]
    if found:
        return found
    # Fallback for platforms that do not report USB VID/PID on serial ports.
    return [p for p in list_ports.comports()
            if "usbmodem" in p.device or "ttyACM" in p.device]


def normalise_sn(sn):
    """J-Link serials appear as '000760131635' (USB) or 760131635 (JLinkExe)."""
    return str(sn or "").lstrip("0")


def port_for_serial(serial_no):
    """The device path for one board, or None."""
    want = normalise_sn(serial_no)
    for p in dwm_ports():
        if normalise_sn(p.serial_number) == want:
            return p.device
    return None
