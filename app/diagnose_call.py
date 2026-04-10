"""
diagnose_call.py — Run this TWICE: once while idle, once during a Teams call.
Compare the output to find what changes between idle and in-call states.

Usage:
    python app/diagnose_call.py          # run idle first
    # join a Teams call
    python app/diagnose_call.py          # run again during call
"""

import ctypes
import subprocess
import os
import psutil
from ctypes.util import find_library

# ── CoreAudio setup ────────────────────────────────────────────────────────────
_ca = ctypes.CDLL(find_library("CoreAudio"))
_cf = ctypes.CDLL(find_library("CoreFoundation"))
_cf.CFStringGetCString.restype = ctypes.c_bool

_SYS_OBJ  = ctypes.c_uint32(1)
_GLOB     = 0x676c6f62
_INPT     = 0x696e7074
_EL       = 0
_DEVLIST  = 0x64657623
_DEVNAME  = 0x6c6e616d
_RUNNING  = 0x68737273  # DeviceIsRunningSomewhere
_STREAMS  = 0x73746d23  # Streams (input scope)
_DEFIN    = 0x64496e20  # DefaultInputDevice
_DEFOUT   = 0x64506e20  # DefaultOutputDevice ('dPn ')  -- actually 0x644f7574 'dOut'
_DEFOUT2  = 0x644f7574  # kAudioHardwarePropertyDefaultOutputDevice
_UTF8     = 0x08000100

# Additional device properties to check
_PROC_MUTE   = 0x70726d74  # kAudioDevicePropertyProcessMute ('prmt')
_MUTE        = 0x6d757465  # kAudioDevicePropertyMute        ('mute')
_VOLUME      = 0x766f6c6d  # kAudioDevicePropertyVolumeScalar ('volm')
_NCHAN       = 0x6368616e  # kAudioDevicePropertyStreamConfiguration ('chan')
_SAMPLE_RATE = 0x6e737274  # kAudioDevicePropertyNominalSampleRate ('nsrt')


class _Addr(ctypes.Structure):
    _fields_ = [('sel', ctypes.c_uint32), ('scope', ctypes.c_uint32), ('elem', ctypes.c_uint32)]


def get_device_ids():
    a = _Addr(_DEVLIST, _GLOB, _EL)
    sz = ctypes.c_uint32(0)
    _ca.AudioObjectGetPropertyDataSize(_SYS_OBJ, ctypes.byref(a), 0, None, ctypes.byref(sz))
    count = sz.value // 4
    buf = (ctypes.c_uint32 * count)()
    _ca.AudioObjectGetPropertyData(_SYS_OBJ, ctypes.byref(a), 0, None, ctypes.byref(sz), buf)
    return list(buf)


def get_name(dev_id):
    a = _Addr(_DEVNAME, _GLOB, _EL)
    sz = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    cfstr = ctypes.c_void_p(0)
    ret = _ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(dev_id), ctypes.byref(a), 0, None, ctypes.byref(sz), ctypes.byref(cfstr)
    )
    if ret != 0 or not cfstr.value:
        return ''
    buf = ctypes.create_string_buffer(256)
    _cf.CFStringGetCString(cfstr, buf, 256, _UTF8)
    _cf.CFRelease(cfstr)
    return buf.value.decode('utf-8', errors='ignore')


def has_input_streams(dev_id):
    a = _Addr(_STREAMS, _INPT, _EL)
    sz = ctypes.c_uint32(0)
    ret = _ca.AudioObjectGetPropertyDataSize(ctypes.c_uint32(dev_id), ctypes.byref(a), 0, None, ctypes.byref(sz))
    return ret == 0 and sz.value > 0


def is_running_somewhere(dev_id):
    a = _Addr(_RUNNING, _GLOB, _EL)
    val = ctypes.c_uint32(0); sz = ctypes.c_uint32(4)
    ret = _ca.AudioObjectGetPropertyData(ctypes.c_uint32(dev_id), ctypes.byref(a), 0, None, ctypes.byref(sz), ctypes.byref(val))
    return val.value if ret == 0 else -1


def get_uint32_prop(dev_id, sel, scope=None):
    if scope is None:
        scope = _GLOB
    a = _Addr(sel, scope, _EL)
    val = ctypes.c_uint32(0); sz = ctypes.c_uint32(4)
    ret = _ca.AudioObjectGetPropertyData(ctypes.c_uint32(dev_id), ctypes.byref(a), 0, None, ctypes.byref(sz), ctypes.byref(val))
    return val.value if ret == 0 else -1


def get_default_input():
    return get_uint32_prop(1, _DEFIN)


def get_default_output():
    return get_uint32_prop(1, _DEFOUT2)


# ── IORegistry check ───────────────────────────────────────────────────────────
def check_ioreg_audio_engines():
    try:
        result = subprocess.run(
            ['ioreg', '-c', 'IOAudioEngine', '-r', '-l'],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        engines = []
        current = {}
        for line in lines:
            line = line.strip()
            if 'IOAudioEngine' in line and '<class' in line:
                if current:
                    engines.append(current)
                current = {'raw': line}
            if '"IOAudioEngineState"' in line:
                current['state'] = line
            if '"IOAudioEngineDescription"' in line:
                current['desc'] = line
            if '"IOAudioEngineCoreAudioDeviceID"' in line:
                current['devid'] = line
        if current:
            engines.append(current)
        return engines
    except Exception as e:
        return [{'error': str(e)}]


# ── Teams process info ─────────────────────────────────────────────────────────
def get_teams_info():
    teams_procs = []
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cpu_percent', 'num_threads', 'num_fds']):
        name = (proc.info.get('name') or '').lower()
        exe  = (proc.info.get('exe')  or '').lower()
        if 'teams' in name or 'teams' in exe:
            teams_procs.append(proc.info)
    return teams_procs


# ── lsof: check if Teams has opened audio device files ────────────────────────
def get_teams_audio_fds():
    try:
        teams_pids = []
        for proc in psutil.process_iter(['pid', 'name']):
            if 'teams' in (proc.info.get('name') or '').lower():
                teams_pids.append(str(proc.info['pid']))
        if not teams_pids:
            return []
        result = subprocess.run(
            ['lsof', '-p', ','.join(teams_pids), '-n'],
            capture_output=True, text=True, timeout=5
        )
        audio_lines = [l for l in result.stdout.splitlines()
                       if any(kw in l.lower() for kw in ['audio', 'coreaudio', 'mediasrv', 'ipc', 'socket', 'pipe'])]
        return audio_lines[:30]  # limit output
    except Exception as e:
        return [f'error: {e}']


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("=" * 70)
    print("TEAMS CALL DIAGNOSTIC")
    print("=" * 70)

    # 1. CoreAudio devices
    print("\n── CoreAudio Devices ──────────────────────────────────────────────")
    default_in  = get_default_input()
    default_out = get_default_output()
    print(f"  Default input:  [{default_in}] {get_name(default_in)}")
    print(f"  Default output: [{default_out}] {get_name(default_out)}")
    print()

    all_ids = get_device_ids()
    for dev_id in all_ids:
        name     = get_name(dev_id) or f'<device {dev_id}>'
        has_in   = has_input_streams(dev_id)
        running  = is_running_somewhere(dev_id)
        mute_in  = get_uint32_prop(dev_id, _MUTE, _INPT)
        mute_out = get_uint32_prop(dev_id, _MUTE, 0x6f757470)  # output scope
        flag_in  = "  [INPUT]" if has_in else ""
        print(f"  [{dev_id:3d}] {name:<40s}  IsRunningSomewhere={running}  "
              f"mute_in={mute_in}  mute_out={mute_out}{flag_in}")

    # 2. IOAudioEngine states
    print("\n── IOAudioEngine States (IOKit) ───────────────────────────────────")
    engines = check_ioreg_audio_engines()
    for e in engines:
        if 'error' in e:
            print(f"  ERROR: {e['error']}")
        else:
            desc  = e.get('desc',  '').strip()
            state = e.get('state', '').strip()
            devid = e.get('devid', '').strip()
            print(f"  {desc}")
            print(f"    state={state}  devid={devid}")

    # Also run raw ioreg for IOAudioEngineState values
    print("\n  Raw IOAudioEngineState values:")
    try:
        result = subprocess.run(
            ['ioreg', '-c', 'IOAudioEngine', '-r', '-d', '2'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if 'IOAudioEngineState' in line or 'IOAudioEngineDescription' in line or 'IOAudioEngineCoreAudioDeviceID' in line:
                print(f"    {line.strip()}")
    except Exception as e:
        print(f"  ioreg error: {e}")

    # 3. Teams process
    print("\n── Teams Process Info ─────────────────────────────────────────────")
    teams = get_teams_info()
    if teams:
        for t in teams:
            print(f"  {t}")
    else:
        print("  Teams NOT running")

    # 4. Teams audio file descriptors
    print("\n── Teams Audio/IPC File Descriptors (lsof) ───────────────────────")
    fds = get_teams_audio_fds()
    if fds:
        for f in fds:
            print(f"  {f}")
    else:
        print("  (none or Teams not running)")

    # 5. macOS privacy / microphone indicator (system log, last 10s)
    print("\n── System Log: mic access events (last 10s) ───────────────────────")
    try:
        result = subprocess.run(
            ['log', 'show', '--last', '10s', '--predicate',
             'subsystem == "com.apple.coreaudio" OR subsystem == "com.apple.audio" OR '
             'eventMessage CONTAINS "microphone" OR eventMessage CONTAINS "kTCCServiceMicrophone"',
             '--style', 'compact'],
            capture_output=True, text=True, timeout=10
        )
        lines = [l for l in result.stdout.splitlines() if l.strip()][-20:]
        for l in lines:
            print(f"  {l}")
    except Exception as e:
        print(f"  log error: {e}")

    print("\n" + "=" * 70)
