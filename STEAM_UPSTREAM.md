# Steam upstream changes required by MWManager

Status: 2026-11-15

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
| `1` | Some requested actions failed or remain pending. | Halt deployment and keep the plan for review. |
| `2` | The reviewed plan is absent or changed, or invocation/preflight is invalid. Nothing from that plan was applied. | Halt and require a new review. |

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
- Asset repair being disabled unless the user explicitly enables it.

Do not replace these with Steam-root defaults during a sync.

## Sync acceptance checklist

1. Implement and test the change in the Steam utility source.
2. Run the Steam/upstream suite without applying changes to live mods.
3. Copy the changed shared files into `tools/steam-upstream/` and the project root unchanged.
4. Refresh `COPY_MANIFEST.json`, `utility_source_manifest.json`, and recorded SHA-256 values.
5. Verify every shared project-root file is byte-identical to its `tools/steam-upstream/` copy.
6. Run the MWManager suite and the upstream suite separately, then run the combined-suite acceptance command above.
7. Test destructive `--apply`, `--fix`, and deployment flows only on throwaway GOG/staging copies before live use.
