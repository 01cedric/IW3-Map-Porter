# Regional support — 22.2.9

Exact-file UI editing and automatic revision inspection are available.
It does not claim universal PS3 compatibility.

| Area | Supported now | Remaining |
| --- | --- | --- |
| Tested BLES reference UI (title ID/update not recorded) | Exact hash and reopen/export; custom online startup confirmed | Cannot assign this test to a specific BLES ID |
| Supplied BLUS-30072 UI | Captured native allocations, mapped editing fields, automatic export/reopen | PS3 gameplay testing; update number unknown |
| BLJS-10031 / BLJM-60173 | Product IDs catalogued; metadata inspector accepts actual software ID from PARAM.SFO | Original UI/FF files and actual decrypted executables required |
| Disc / update 1.40 metadata | Read APP_VER and CATEGORY from supplied PARAM.SFO automatically | Cannot establish which executable a console actually launched |
| ELF BSP diagnostics | Discover unique error-string/TOC call from supplied PPC64 ELF | Unmatched or ambiguous patterns produce no breakpoint recommendation |
| Native loader/linker emulator | Existing verified executable layout | Other disc/update layouts are not automatically rebased |
| Online private match | Custom startup fix confirmed on the tested BLES setup | BLUS execution, joining and stock-map restoration need console tests |

## Catalogued European IDs

BLES-00115, BLES-00148, BLES-00149, BLES-00154, BLES-00155 and BLES-00156
are catalogued identifiers, not six independently validated UI profiles. The
reference profile has no recorded title ID. It must not be advertised as a
verified profile for every BLES release. Other BLES/BLUS UI hashes are rejected
unless an existing profile matches. This does not establish map incompatibility
on every untested release.

## Inspect an installation file

From the extracted portable folder:

```bat
runtime\python\python.exe tools\inspect_ps3_revision.py "D:\Game\PS3_GAME\USRDIR\EBOOT.ELF" --output revision.json
runtime\python\python.exe tools\inspect_ps3_revision.py "D:\Game\PS3_GAME\PARAM.SFO"
```

The inspector accepts decrypted ELF, PARAM.SFO, and supported PS3 fastfiles.
It does not modify game files or decrypt EBOOT.BIN. It reads the nearest ancestor
PARAM.SFO; keep disc and installed-update folders separate to preserve provenance.
An APP_VER of 01.40 is metadata, not permission to reuse another executable's offsets.
Compare reported original instruction bytes with live debugger memory before trapping.

The UI editor performs detection automatically during Inspect and Export. Its
run folder contains ps3-revision.json, including the source file and recovered
original UI identity. Unknown hashes are rejected before rebuilding.

## Profile evidence

- Existing reference zone SHA-256: `0a23c3b27a8989773f091789952c1274c674b4f7ad8c9a9c8e82cda6912fe349`.
- Supplied BLUS zone SHA-256: `31a7d82e8cc53724f4d5d73396a05389feba25c7294552858ea4db5f81ca4bc4`.
- BLUS native capture: 537,068 allocations, 92,642 fixups, byte-exact no-op replay.
- The BLUS test export passed the reference native loader: 102 root assets, 181 stock-menu items, 89 custom-menu items. Export/reopen/re-export preserves zone bytes.
- Capture and export validation use the existing reference loader, not a supplied BLUS executable.
- An identical file hash can reuse a profile regardless of its packaging ID.
- A different hash cannot reuse a profile solely because its title/update label matches.

## Regional catalog sources

[SerialStation's CoD4 release inventory](https://serialstation.com/games/82e94799-d6c8-4c04-bb54-a203017a6ed3)
lists Japanese product IDs alongside BLJS-10013 software identity.
[Trisaster's title list](https://www.trisaster.de/page/index.php?topic=109)
and [VGCollect's PAL list](https://vgcollect.com/forum/index.php?topic=9790.0)
provide the European IDs catalogued in this release. These lists are inventory
sources, not asset-format or executable-compatibility evidence.

For an unverified revision, supply its original ui_mp.ff, PARAM.SFO, and decrypted
executable. Map dependency verification additionally needs its retail support zones.
