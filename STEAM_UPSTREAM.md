# Steam upstream changes required by MWManager

Status: 2026-11-16

MWManager runs against the selected GOG installation. The Steam installation is the development source for the
reusable `mwdialog_*` and `vortex_tidy.py` utilities. Changes in this document belong in the Steam utilities first,
then must be copied byte-for-byte into `tools/steam-upstream/` and the MWManager project root.

## Required changes

### 1. Make `--play` store-neutral

`mwdialog_check.launch_game()` currently always sets `SteamAppId`/`SteamGameId`, checks for `steam.exe`, warns when
Steam is not running, and reports the Steam app id. That behavior is correct for the Steam executable but produces a
false warning for MWManager's GOG target.

Smallest acceptable change:

- Detect a Steam installation from local game markers such as `steam_api.dll`, `steam_api64.dll`, or
  `steam_appid.txt`.
- Only set Steam environment variables and check `steam.exe` for a Steam installation.
- Start `Morrowind.exe` directly for GOG without Steam-specific warnings or status text.
- Keep `--steam-appid` for Steam users.

Tests must cover one Steam-shaped folder and one GOG-shaped folder without starting a real process.

### 2. Keep the complete mw* suite out of Lua repair by default

`mwdialog_luafix.OWN_MODS` contains only part of the current suite. `mwguard.json` normally supplies the remaining
names through `exemptMods`, but a missing or unreadable config makes the built-in fallback incomplete.

The built-in set must include all 15 suite mods:

- `mwcollide`
- `mwcompanionsneak`
- `mweffectinspector`
- `mwfloatfix`
- `mwguard`
- `mwhangwatch`
- `mwinfoloop`
- `mwmouseguard`
- `mwperformers`
- `mwpluginpatches`
- `mwsaveguard`
- `mwsoundguard`
- `mwstranded`
- `mwteleportguard`
- `mwwarnsuppression`

`mwmanager` may remain for compatibility. Add a test that removes the config file and verifies that every suite mod is
still skipped unless `--include-own` is used.

### 3. Make the plan/apply exit-code contract explicit and tested

MWManager depends on these meanings from `vortex_tidy.py` and `mwdialog_luafix.py`:

| Code | Meaning | MWManager action |
| --- | --- | --- |
| `0` | The requested operation completed, or there was nothing to do. | Continue. |
| `1` | Some requested actions failed or remain pending. | GUI Tidy starts a fresh plan-and-apply pass; after three in a row it asks (Try Again, then Apply Anyway or Stop). |
| `2` | The reviewed plan is absent or changed, or invocation/preflight is invalid. Nothing from that plan was applied. | Same as `1`: a fresh pass, asking only after three in a row. |

Preserve the `MWMANAGER-PLAN:` and `MWMANAGER-PROGRESS:` machine-readable lines. Add focused CLI tests for all three
codes so a future refactor cannot turn a partial failure into apparent success.

### 4. Isolate the upstream test imports when the suite is vendored

`tools/steam-upstream/tests/conftest.py` loads generic names such as `mwdialog_check` into `sys.modules` during test
collection. When the upstream and MWManager suites run in one Python process, MWManager can then import the snapshot
module instead of the project-root module.

The upstream tests should restore the previous `sys.modules` entries after their session, or load private test module
names and inject dependencies without publishing the generic names globally. Acceptance test:

```text
python -m pytest tests tools/steam-upstream/tests
```

The command must not fail because `mwdialog_check` came from `tools/steam-upstream`.

### 5. Close the generated-files record after reading it

`mwdialog_state.read_generated_record()` must open the record with a context manager. The former bare `open()` left
the handle for garbage collection, emitted `ResourceWarning` during the combined suite, and could briefly prevent a
Windows rewrite or rollback from replacing the record.

### 6. `vortex_tidy`: plan asset repairs at the paths the moves will leave

`assets_check()` checks a package before the plan's moves and names each repair by the file's current path
(`Data Files\textures\x.dds`). `assets_apply()` checks a moved package again, and the same repairs now carry the new
paths (`textures\x.dds`), so they are reported as "N repair(s) appeared after the moves - not in the plan you reviewed,
so not made", and the old-path lines stay in the plan as "NOT applied" (exit `1`). Seen on OAAB_Data: 111 "new"
repairs and 9 lines left, though nothing new was found.

A file moved *into* a checked folder is missed the same way: Dr_Data shipped `sounds\dr\*.wav` (not a sound folder),
Tidy moved them to `Sound\dr\`, and the WAV-to-MP3 conversion they then needed was dropped.

**Done 2026-11-16 (Steam source, synced):** `assets_apply()` checks each moved package again after the moves. Reviewed
repairs that still apply are made; reviewed lines whose file the moves took away count as settled; repairs that only
appear after the moves are queued. When every package is done, the queue - and only the queue - is processed. Queued
repairs that fail stay in the plan (exit `1`). Test: `test_repairs_found_after_the_moves_are_queued_and_made`.

### 7. `mwdialog_assets.py`: MWManager's copy has drifted from upstream

The project-root `mwdialog_assets.py` is no longer byte-identical to `tools/steam-upstream/`. Changes to take upstream
(or move into `mwmanager_assets_ext.py`) before the next sync:

- `catalog_has_asset()`: an orphaned animation is not moved to Off when the installed game's BSA catalog
  (`game_catalog.sqlite3`) owns that path. MWManager-specific (the catalog is the manager's); move it to the extension.
- `check_folder()`: every WAV under `sound`/`music` gets `job["mp3"] = True` (converted to a same-stem MP3; Morrowind
  falls back from `.wav` to `.mp3`). Generic; take it upstream.
- `package_bug_report_path()` and the changed `bug_report_path()`: bug reports named per package and per checker.
  Take the helper upstream; the path choice may stay MWManager's.

**Done 2026-11-16:** the WAV rule and `package_bug_report_path()` are in the Steam source (test:
`test_check_folder_converts_every_wav_to_a_same_stem_mp3`); `catalog_has_asset()` moved to `mwmanager_assets_ext.py`,
which drops those orphan findings after the upstream check. All shared files are byte-identical again and
`COPY_MANIFEST.json` hashes are current.

### 8. `mwdialog_luafix`: a status on every reported finding

**Done 2026-11-16 (Steam source, synced):** `format_report(..., done=, refused=)` prefixes each finding with `PATCHED`,
`NOT PATCHED`, `UNCONFIGURED` (should be fixed, no fix exists) or `REPORTED`, and ends each file with `=> PATCHED` or
`=> NOT PATCHED: <why>`. Without `done` the plan text is unchanged. MWManager's `backup_relative_to` was taken upstream
(keyword-only, so tests that set `apply_report.__defaults__` still work).

## MWManager-only behavior — do not put upstream

These remain project extensions:

- GOG installation selection, preflight, elevation, and deployment.
- Guided prerequisite archive detection, `pre-reqs` storage, installation, and configuration launchers.
- Atomic GUI state and load-order saving.
- The unified `mwmanagermenu` MCM entry.
- Manager-owned reports, caches, journals, backups, disabled plugins, and staging paths.
- Store-install identity checks, lossless archival of stale rollback baselines, and fresh baseline capture after a
  repair or reinstall.
- GUI deployment manifests being bridged into the install rollback ledger, including Lua replacements, generated
  plugin writes, BSA registration changes, and regular/major-master deployment operations.
- GUI first-run `--init` asset repair, including journaled `Off/Original Install` moves and manager-owned official ESP backups.
- GUI first-run activation of every shipped ESP and its standalone pre-`--full` sort.
- Generated stand-ins being recorded in both the generated-files list and MWManager's rollback ledger.
- Plugin and major-master archive installation workflows.
- Asset repair always on (no switch since 2026-11-16), including PNG/JPEG conversion under the game image folders,
  Lua-loaded images kept loose and out of BSAs, and TGA/BMP files shadowed by a same-named DDS moved to Off.

Do not replace these with Steam-root defaults during a sync.

## Sync acceptance checklist

1. Implement and test the change in the Steam utility source.
2. Run the Steam/upstream suite without applying changes to live mods.
3. Copy the changed shared files into `tools/steam-upstream/` and the project root unchanged.
4. Refresh `COPY_MANIFEST.json`, `utility_source_manifest.json`, and recorded SHA-256 values.
5. Verify every shared project-root file is byte-identical to its `tools/steam-upstream/` copy.
6. Run the MWManager suite and the upstream suite separately, then run the combined-suite acceptance command above.
7. Test destructive `--apply`, `--fix`, and deployment flows only on throwaway GOG/staging copies before live use.
