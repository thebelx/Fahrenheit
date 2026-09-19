# fuck you.

To everyone who called it bullshit before reading a single byte. To everyone who wrote "skid" in a Discord and went back to their game. To everyone who watched the repo land and said nothing, then privately asked someone else whether it was real. To the four or five named in the receipts section, and to the dozens unnamed who did the same thing in smaller rooms. To Sony Interactive Entertainment's legal department, who sent a letter to Angela Redgrave with my aliases on it and a response deadline of October 2. This is for you. Read it or don't. The drive is on the desk either way.

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