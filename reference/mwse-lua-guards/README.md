# The mw* MWSE Lua suite

MWManager installs the user's mw* MWSE Lua suite as it is in the Steam Morrowind install. Every file here is a
byte-identical copy of the Steam file; `SOURCE_MANIFEST.json` lists each one with its source and SHA-256
(`tests/test_game_writes.py` checks the copies against it). Do not edit these files here: change them in the
Steam install, then copy them again and refresh the manifest.

- `mods/<name>/` - one folder per suite mod, installed by `--init` to `Data Files\MWSE\mods\<name>\`:
  mwguard, mwhangwatch, mwfloatfix, mwstranded, mwcollide, mwperformers, mwinfoloop, mwsoundguard,
  mwsaveguard, mwwarnsuppression, mwcompanionsneak, mwpluginpatches, mweffectinspector, mwmouseguard,
  mwteleportguard (15 mods). `dejespreplacements` (formerly mwespreplacements) is the user's own replacement for plugins in one load order;
  it is not part of the suite and MWManager does not ship it.
- `metadata/<name>-metadata.toml` - the MWSE metadata files that go beside them in `Data Files\` (load priority).

Each mod registers its own settings page and keeps its settings in `Data Files\MWSE\config\<name>.json`, which
`--init` does not touch. `mwguard.json`'s `exemptMods` lists every suite mod. In the game the pages appear under
one Mod Config Menu entry, "MWManager", one tab per mod: that is `reference/mwmanager-menu/` (MWManager's own
`mwmanagermenu` mod, installed with the suite), which collects the pages without changing the suite's files.

`--init` also moves MWManager's earlier Lua packaging out of `MWSE\mods` to `Data Files\MWSE\backups\mods\`
(where MWSE never loads Lua from): the consolidated `MWSE\mods\MWManager\` mod (with its `mwquickfixes` tab and
`off\legacy-standalone\` archive) and a standalone `mwquickfixes` folder. The quick fixes now live in the suite:
CorruptSaveGuard is mwsaveguard, SuppressBodyPartWarnings and SuppressShutdownWarnings are mwwarnsuppression,
CompanionSneak is mwcompanionsneak, DeferredDialogueTeleport is mwteleportguard and MouseBleedFix is
mwmouseguard. Standalone folders of those older names are the user's own and `--init` leaves them alone.
