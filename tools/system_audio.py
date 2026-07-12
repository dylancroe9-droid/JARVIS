"""
System audio detector — tells us when ANY app is outputting sound to the
default speakers (Music, Safari/Chrome video, YouTube, podcasts, etc).

Uses CoreAudio's `kAudioDevicePropertyDeviceIsRunningSomewhere` selector
on the default output device. Returns True/False instantly.

Used by the mic gating to switch to wake-word mode whenever sound is
playing, not just when Apple Music is playing.
"""

from __future__ import annotations

import ctypes
import ctypes.util


# ── CoreAudio framework ──────────────────────────────────────────────────────
_ca_path = ctypes.util.find_library("CoreAudio") or \
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
try:
    _ca = ctypes.CDLL(_ca_path)
except Exception:
    _ca = None


# ── Constants (FourCC codes from <CoreAudio/AudioHardware.h>) ────────────────
def _fourcc(s: str) -> int:
    return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])

kAudioObjectSystemObject                     = 1
kAudioHardwarePropertyDefaultOutputDevice    = _fourcc("dOut")
kAudioObjectPropertyScopeGlobal              = _fourcc("glob")
kAudioObjectPropertyElementMain              = 0
kAudioDevicePropertyDeviceIsRunningSomewhere = _fourcc("gone")


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope",    ctypes.c_uint32),
        ("mElement",  ctypes.c_uint32),
    ]


if _ca is not None:
    _ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    _ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,                        # inObjectID
        ctypes.POINTER(_AudioObjectPropertyAddress),  # inAddress
        ctypes.c_uint32,                        # inQualifierDataSize
        ctypes.c_void_p,                        # inQualifierData
        ctypes.POINTER(ctypes.c_uint32),        # ioDataSize
        ctypes.c_void_p,                        # outData
    ]


def _get_default_output_device() -> "int | None":
    if _ca is None:
        return None
    device = ctypes.c_uint32(0)
    addr = _AudioObjectPropertyAddress(
        kAudioHardwarePropertyDefaultOutputDevice,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
    err = _ca.AudioObjectGetPropertyData(
        kAudioObjectSystemObject,
        ctypes.byref(addr),
        0, None,
        ctypes.byref(size),
        ctypes.byref(device),
    )
    return device.value if err == 0 and device.value else None


def is_system_audio_playing() -> bool:
    """
    Returns True iff the default output device has at least one client
    currently producing audio. Catches Music, Safari/Chrome video,
    YouTube, QuickTime, VLC, podcasts — anything.
    """
    if _ca is None:
        return False
    device = _get_default_output_device()
    if not device:
        return False
    is_running = ctypes.c_uint32(0)
    addr = _AudioObjectPropertyAddress(
        kAudioDevicePropertyDeviceIsRunningSomewhere,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain,
    )
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
    err = _ca.AudioObjectGetPropertyData(
        device,
        ctypes.byref(addr),
        0, None,
        ctypes.byref(size),
        ctypes.byref(is_running),
    )
    return err == 0 and bool(is_running.value)
