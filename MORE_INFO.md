# MWManager: more information

This is the detailed companion to [`README.md`](README.md). First-run checks, tools you install yourself, and OpenMW-in-staging behaviour stay in [`PREREQUISITES.md`](PREREQUISITES.md). The Steam tools MWManager runs unchanged have their own [`tools/steam-upstream/README.md`](tools/steam-upstream/README.md) and [`tools/steam-upstream/docs/MORE_INFO.md`](tools/steam-upstream/docs/MORE_INFO.md).

MWManager needs **Python 3.9 or newer**.

## mlox rules

On first-run preflight (and `--init`), if the community mlox rule files are missing, MWManager offers:

- **Download the latest mlox rules (from the mlox project on GitHub)** -- saves the official `mlox_base.txt` and `mlox_user.txt` into `mlox\`.
- **Keep using my own mlox rules** -- leaves `mlox\` as it is.

Later, **Tools > Update rules…** (and `--update-rules`) fetch those community files only if GitHub has a newer copy, then prune conflicting rules in place: they are commented out in the live `mlox_base.txt` / `mlox_user.txt`, not copied into a local override. If `mwmanager_rules.txt` already exists, obsolete entries there are cleaned up in the same pass. Personal rules belong in `mlox\mlox_my_rules.txt`; a download never overwrites that file. Community files are not backed up (the originals stay on GitHub). `mlox_my_rules.txt` keeps at most one rolling backup when MWManager has to change it.

See also [`PREREQUISITES.md`](PREREQUISITES.md#mlox-load-order-rules).

## Tools > Update MWSE…

**Tools > Update MWSE…** compares `mwse-version.txt` in the install with the official MWSE build-automatic release and, if you confirm, runs `MWSE-Update.exe`. After a successful (or already-current) update it re-applies MWSE core KnownBug patches -- today KB3 and KB4 on `MWSE\core\lib\tes3\exclusionspage.lua` (`apply_core_known_bugs`, the same work as luafix `--core-only`).

The same official updater is also offered during `--init` and `--full`. First-run preflight has its own prompts (**Update MWSE now (highly recommended)** after MWSE is installed, and **MWSE update available** if the build is outdated) and then runs the updater if you confirm; those preflight prompts do not use the `offer_update_on_full` flag. `--play` does a quick local check: if the MWSE build recorded in `mwse-version.txt` or a previously patched core file has changed, the KnownBug patches are re-applied before the game starts. If a patch no longer matches the file, or cannot be applied, Play stops the same way it does for a missing master. `--play-anyway` still starts the game.

These switches live in `mwmanager.ini` under `[MWSE]`. All three default to true. There is no toggle in the Options window; edit the ini if you need to change them.

| Key | Default | What it does |
| --- | --- | --- |
| `check_on_play` | true | Check and re-apply core KnownBug patches on Play |
| `apply_core_patches` | true | Actually write those patches (off = check only) |
| `offer_update_on_full` | true | Offer the MWSE updater during `--full` and `--init` |

Implementation is in `mwmanager_mwse_core.py`.

## Core game files on Play

On Play, MWManager also checks the six shipped files against known-good SHA-256 values:

- `Morrowind.esm`, `Tribunal.esm`, `Bloodmoon.esm`
- `Morrowind.bsa`, `Tribunal.bsa`, `Bloodmoon.bsa`

Unchanged files stay silent. If any have changed, a warning strongly suggests restoring them from a clean install or Steam/GOG verify, but it does not block Play. You can **Continue anyway**, or **Don't warn again**.

**Don't warn again** is a button, not a checkbox. It mutes only the current hash of each altered file, stored in `outputs\core_game_file_mute.json`. A further change to that file brings the warning back. The last check is recorded in `outputs\core_game_file_state.json`.

The check is on by default. Turn it off with this ini key (again, not in the Options window):

```
[Integrity]
check_core_game_files = false
```

Implementation is in `mwmanager_core_game_files.py`. This is a different check from the first-run / `--init` hashes of MCP, executable, DLLs, `MGEXEgui.exe` and the BSA archives described in [`PREREQUISITES.md`](PREREQUISITES.md#known-file-hashes).

## Lua tools: no downgrade

When `--play` or a sync deploys the first-party MWSE suite from `lua tools\` into `Data Files`, a missing file is copied in, and an older deployed file is replaced (with a backup). A deployed copy that is *newer* than the copy in `lua tools\` is left alone -- the tools will not put an older version over a newer one. That decision uses the SHA-256 history in `generated\mwdialog_lua_manifest.json` when it is known; modification times are used only when that history is not available. Files under `MWSE\config\` (your settings) are only copied when they are missing.

This is the Steam tools' `deploy_lua_tools` behaviour; MWManager uses it unchanged.

## Review major masters

### Planned masters

If you untick a master in **Review major masters**, options in the same batch that need that master are held until it is selected again. They are not offered as if the master were coming later in the same batch.

### Tamriel_Data HD / SD

When both the HD (Nexus 59927) and SD (Nexus 44537) Tamriel_Data archives are available, the Review major masters list can swap the deployed files in place with **Use HD**, **Use SD**, or **Use other variant**. Tamriel Rebuilt, Project Cyrodiil and Skyrim: Home of the Nords stay deployed, in the same load order. The swap is implemented in `swap_master_variant.py`.

### Solstheim Tomb of the Snow Prince

Selecting or enabling Solstheim Tomb of the Snow Prince (SOTSP) warns that it replaces Bloodmoon's land.

### Translation.txt

When a staged Tamriel Data package includes `Docs\Translation.txt` (or the older `Tamriel Data (HD) - Translation.txt` name), MWManager can copy it into the tools folder for use as a rename table. A newer Tamriel Data or Tamriel Rebuilt archive may prompt to run `--full` so that table stays current.

## Vortex paths

When the Archive Downloads source is Vortex, MWManager reads Vortex's download and install paths from its `state.v2` LevelDB under `%APPDATA%\Vortex`. The reader is in `mwdialog_paths.py`, uses only the Python standard library, and is read-only -- no native LevelDB module and no running Vortex process are required.

## Hang watch settings

The first-party hang-watch module (`mwhangwatch`) has no Mod Config Menu tab of its own. Its settings live in mwguard's **Guard & Hang Watch** page (the unified MW Conflict Tools menu). Those settings and their config file are unchanged; only the tab they appear in is hosted there.

## Personal mods and Nexus IDs

`python nexus_ids.py set NAME personal` marks an archive, staged folder or MWSE mod as your own (or hand-adjusted). Personal mods are never update- or currency-checked, never searched on Nexus, shown as personal, and listed as not distributable in an exported profile. Manual answers live in `nexus_id_overrides.json` under `%LOCALAPPDATA%\MWManager` (so they outlast a reset of the MWManager folder); a copy in the MWManager folder (`--manager`) wins over that file. `python nexus_ids.py personal` lists them.

If a package name or metadata does not yield a Nexus id, MWManager may still resolve one from Vortex records (download list, installed-mod list, staged `meta.ini`) or, only when that is not enough, a public Nexus search that accepts a match only for an exact file name or a close size-and-name match. An unverified id is shown as unknown until you pick a source.

## OpenMW Converter

MWManager itself supports the original Windows Morrowind engine only (OpenMW *installs* are not supported).

A separate **OpenMW Converter** project is aimed at turning OpenMW-oriented content toward MWSE:

- Repository: [https://github.com/dejunai/openmw-mwse](https://github.com/dejunai/openmw-mwse)
- Status: experimental and in early development
- Access: the repository is private for now
- Documentation: see [that project's own README](https://github.com/dejunai/openmw-mwse)

This is not the same as tidy's handling of `.omwaddon` files and OpenMW resources already in a vanilla staging folder (`Off\OpenMW\`, optional conversion to `.esp`). That behaviour is described in [`PREREQUISITES.md`](PREREQUISITES.md#openmw-content-in-vanilla-staging).

## Tests and CI

```
python -m pytest -q -p no:cacheprovider tests
```

The tests only use temporary folders; they never touch a game install, Vortex, or MWManager's own state. They are meant to pass on Python 3.9 and newer (compatibility helpers live in `py_compat.py`).

GitHub Actions (`.github/workflows/tests.yml`) does not run on push or pull request. The workflow is started by hand (`workflow_dispatch`). The matrix includes Python 3.9, 3.12 and 3.14.

## Tk and the GUI

Tk is single-threaded. Background work is started with `run_in_background`; the worker must not touch Tk widgets or stored `settings`. UI updates are marshalled onto the Tk thread through a queue and `after()`. Window lifetime helpers live in `tk_lifetime.py`.

## Credit

dej (dejunai)
