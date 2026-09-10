# Map Menu Editor

[Back to README](../README.md)

1. Select a supported original or exported `ui_mp.ff` and inspect its entries.
2. Keep the 16 stock rows. Add up to 16 custom entries with a map ID, display name
   and description. Names support up to 64 ASCII characters.
3. Select a custom entry and choose **Set loading picture**. Open the corresponding
   ported PS3 `_load.ff`; the tool prepares a preview up to 512 pixels per edge.
4. Export a separate `ui_mp.ff`, review its reports and install it with the map
   FastFiles. Retain a backup of the original menu.

Custom entries appear in a yellow Custom Maps submenu. Pictures are embedded in
the exported file. New exports can be reopened with their names, descriptions and
pictures; older 22.1.12/22.1.13 exports need their untouched original once.
Supported UI revisions are selected by exact decompressed hash; see
[regional support](REGIONAL_SUPPORT.md).

The exporter rebuilds allocations, packed references and stream sizes. It verifies
readback, Adler-32 checksums and complete 64-KiB decompression frames. These checks
cover the saved file; they do not simulate an entire multiplayer session.

For direct custom private-match startup, the launch script saves and disables
`useSvMapPreloading`, then queues restoration after the map command. This startup
path is confirmed on BLES. BLUS execution and restoration need further console
testing. Stock private maps retain `xpartygo`; local menus retain their native
StartServer path. No EBOOT patch or console-side map-list JSON is required.

The native loader validation tools require an external compatible decrypted ELF
and original UI fixtures. They do not execute the PS3 renderer or network session.
