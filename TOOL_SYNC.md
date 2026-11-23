# Steam utility sync before runs

The current Lua suite deploys under `MWSE\mods\mwsuite`, with a bootstrap that preserves unqualified module
imports. Earlier dated notes below describe their original flat layout. MWManager archives old flat copies;
Vortex-staged flat suite folders must be moved and redeployed before an upgrade.

## Checkout line-ending policy

Shared Python helpers are checked out with LF through `.gitattributes`, and each
root helper must remain byte-identical to its vendored copy. `COPY_MANIFEST.json`
retains the original Steam/Windows byte fingerprints; validation accepts either
the exact checkout bytes or those bytes normalized to CRLF. It does not ignore
other content changes or rewrite the upstream fingerprints to hide divergence.

Before running MWManager or one of its Python utilities, compare the tracked tools and tidy configuration in `utility_source_manifest.json` with their Steam copies:

- `mwdialog_check.py` → `mwmanager.py`
- `mwdialog_assets.py` → `mwdialog_assets.py`
- `vortex_tidy.py` → `vortex_tidy.py`
- `vortex_tidy_blacklist.txt` → `mwmanager_vortex_tidy_blacklist.txt`

Use SHA-256 to detect changes; timestamps alone are not reliable. Copy blacklist changes to the configured renamed sidecar file and retain the Steam snapshot. If a Steam source changed, copy it into `tools/steam-upstream/`, review and merge its changes into the working MWManager code, preserve manager-specific paths and behavior, and update the manifest only after the merge is complete. Do not overwrite the renamed/extended `mwmanager.py` with the Steam script. Keep unrelated Steam scripts and `tools/steam-upstream/not-related/` out of this tracked set.

The 2026-11-11 sync found updates to `mwdialog_check.py`, `mwdialog_assets.py`, `vortex_tidy.py`, and the dialog conflict blacklist. The Steam versions are saved in `tools/steam-upstream/`, their changes are merged into MWManager, and the manifest fingerprints were refreshed. The merge retains MWManager-specific staging, backup, progress, Lua-logo mipmap handling, and package ownership behavior.


## Root Python utilities reviewed — 2026-11-11

All nine root-level Steam `.py` scripts are captured in `tools/steam-upstream/`, with hashes and relevance notes in `utility_source_manifest.json` and `STEAM_UTILITY_REVIEW.md`. The upstream changes to the three core scripts remain feature-level merge work; do not replace `mwmanager.py`, `mwdialog_assets.py`, or `vortex_tidy.py` wholesale. The long-path, parser-backed Lua lint, and mismatched-master utilities are candidate integrations. The two mod-specific patch builders were discarded from the MWManager snapshot at the user’s direction.


## Steam utility refresh — 2026-11-11, 17:36

The Steam root added `mwdialog_planfile.py` and updated `mwdialog_luafix.py` and `vortex_tidy.py`. Current snapshots and hashes are in `tools/steam-upstream/` and `utility_source_manifest.json`; pre-refresh versions are preserved in `previous-2026-11-11-1736/`. The plan guard and stricter Lua checks are relevant candidates, but are not activated in MWManager. The two mod-specific patch builders reappeared upstream and remain excluded from MWManager snapshots and manifests by user direction.


## Steam utility sync — 2026-11-13, 03:40 (Pacific)

- Backup of the previous snapshot, this file and `utility_source_manifest.json`:
  `_backup\pre-upstream-sync-20261113\` (61 files, `SHA256SUMS.txt`).
- `tools\steam-upstream\` now mirrors the public repo's file set (dejunai/morrowind-conflict-tools @50a2a8c):
  35 files copied from the Steam Morrowind root, all SHA-256 verified against their sources, plus the
  repo's 19 `tests\` files (the Steam folder has no tests). Every file, size, hash and source is listed in
  `tools\steam-upstream\COPY_MANIFEST.json`.
- All 29 code/launcher/doc files in Steam are byte-identical to the public repo; only user-state configs
  (load order, rules, answers, added masters, cell winners, generated list, normal maps) and `mwguard.json` are newer.
- The snapshot files replaced today were kept in `previous-2026-11-13-presync\`; `previous-2026-11-11-*\`
  stay as merge bases. `not-related\` is unchanged, and `chargen_ship_patch.py` / `stranded_doors_patch.py`
  stay out of the snapshot root by earlier direction (their `not-related\` copies match Steam).
- `utility_source_manifest.json` now uses relative paths (`<Steam Morrowind>\...`, `tools\steam-upstream\...`).

### UPSTREAM_DECISION (analysis only, nothing in the project changed)

Recommendation: a hybrid. Shared modules come from upstream unchanged; MWManager-only behaviour moves to
small `mwmanager_*` modules that import them; `mwmanager.py` stays a fork for now (merge already resolved)
and shrinks over time. Wholesale copy-over (A) breaks the installer and the MWManager state layout; porting
everything into the forks (B) repeats a 1,300-line conflict merge every sync.

Plan (each step: back up, box tests, PC tests, hash-verified deploy, no --apply/--fix/--full on live data):
1. Copy upstream verbatim: mwdialog_text, mwdialog_planfile, mwdialog_lua, mwdialog_luafix,
   mwdialog_longpaths, mwdialog_mastercheck (mwdialog_paths is already identical).
2. mwdialog_assets: take upstream; move vortex_mod_metadata / nexus_id / bug_report_name into
   mwmanager_assets_ext.py; express the MCM-logo and mesh-only mipmap choices as upstream mip rules.
   Gate asset repairs (Generate runs --all, which turns on --fix) behind an explicit option first.
3. mwmanager.py: deploy the resolved three-way merge (/workspace/mwmanager_resync), then move the --init /
   catalog / MWSE / core-master code into mwmanager_init.py; ask upstream for a state-folder option so the
   manager_file/output_file plumbing can go.
4. vortex_tidy: upstream verbatim for tidy; the FOMOD/options/Data Files installer becomes
   mwmanager_install.py calling it; the GUI runs options, then plan, then --apply, and shows exit 2 as a refusal.
5. Test install flows on a throwaway copy of the game and staging (real --apply there only), then live with the user.


## Permanent exclusions and the game-install rule — 2026-11-13

- `chargen_ship_patch.py` and `stranded_doors_patch.py` build a patch for one named pair of plugins; they are
  not part of MWManager and stay out of the snapshot root and every manifest for good (copies remain in
  `tools\steam-upstream\not-related\` only).
- MWManager targets a fresh GOG install that holds only shipped files. It writes nothing into the game install
  except what the game needs to run the result: deployed mod files, generated plugins and stand-ins, plugin
  dates, Morrowind.ini load-order/archive entries, and MWSE config files that installed MWSE mods read. State,
  caches (including the asset cache), plans, logs, records, reports, backups and disabled plugins live in
  MWManager's folder.
- Exception (2026-11-13): `Data Files\Off\` in the game install holds backups of the game's own shipped files
  when MWManager replaces or repairs them - core ESMs and BSAs, the official add-on ESPs (Siege at Firemoth and
  the rest), and loose files in the shipped asset folders that no staged mod deployed and MWManager did not
  generate. Backups of anything else (generated patches, mod files) go to the staged mod's `Off\` or
  `MWManager\backups\Data Files\Off\`. Disabled plugins, shipped ones included, go to
  `MWManager\disabled_plugins\`; plugins disabled into the game's `Off\Disabled Plugins` by older versions
  can still be re-enabled. The shipped add-ons' readmes are moved to `Data Files\Docs\Official Add-Ons\`.
  When TR_Mainland.esm is installed, the shipped `Siege at Firemoth.esp` it replaces is moved (hash-checked) to
  `MWManager\Off\Official Add-Ons\` by the rule in `mwmanager_plugin_overrides.json`.
- Exception (2026-11-13): originals of MWSE core Lua that `--init` repairs (`MWSE\core`, `MWSE\lib`) are kept in
  the install at `Data Files\MWSE\backups\<path under MWSE>` (e.g. `MWSE\core\initialize.lua` ->
  `MWSE\backups\core\initialize.lua`); the first original is kept and never overwritten. Copies kept by older
  versions in `MWManager\backups\mwse-core\` stay there untouched and are still read. MWManager's retired Lua packaging
  (the consolidated `MWSE\mods\MWManager` mod, `mwquickfixes`) is archived to `MWSE\backups\mods\<name>`. MWSE only runs Lua from `MWSE\core`, `MWSE\mods` (any `main.lua`
  at any depth) and the legacy `MWSE\lua`, and `require` only searches core, core\lib, lib, mods and lua, so
  `MWSE\backups` is never loaded. Staged mods' Lua is repaired by `mwdialog_luafix` (through Tidy); its
  originals stay in `outputs\mwdialog_backup\luafix\<run>\` because `--undo` restores from them.

## Step 1: shared helper modules — 2026-11-13

`mwdialog_text`, `mwdialog_planfile`, `mwdialog_lua`, `mwdialog_luafix`, `mwdialog_longpaths`,
`mwdialog_mastercheck` and `mwdialog_paths` in the project root are byte-identical to `tools\steam-upstream\`
(checked by `tests\test_game_writes.py`). Do not edit them; MWManager passes its own paths instead
(`vortex_tidy.py` gives the Lua fixer `tools\luajit\luajit.exe`, a journal and backups under `outputs\`).

## Steam utility sync — 2026-11-14 (Pacific)

- Backup of the replaced snapshot files, the old `COPY_MANIFEST.json`, this file, `utility_source_manifest.json`
  and every project-root file changed below: `_backup\pre-upstream-sync-20261114-065922\` (`SHA256SUMS.txt`,
  `ROLLBACK.ps1`, `DEPLOYED_SHA256SUMS.txt`).
- `tools\steam-upstream\` now mirrors the public repo's file set (dejunai/morrowind-conflict-tools @f9ce677):
  76 files copied from the Steam Morrowind root, all SHA-256 verified against their sources. New since
  2026-11-13: the 15 modules split out of `mwdialog_check.py` (tes3, bsa, media, backup, loadorder, records,
  state, cellmods, datatree, console, masters, patchfile, redundant, generated, restore), `MWDIALOG_REFACTOR.md`,
  the MWSE Lua mods (`Data Files\MWSE\mods\mwguard`, `mwsoundguard`, `mwhangwatch`) and the install's own
  `tests\` (now shipped in the Steam folder, including `test_split_modules.py` and `test_mastercheck_cache.py`).
- The snapshot files replaced today are in `previous-2026-11-14-presync\`; `chargen_ship_patch.py` /
  `stranded_doors_patch.py` stay excluded (copies in `not-related\`).
- Step 1 refreshed: the seven shared modules in the project root are byte-identical to the new snapshot again
  (`mwdialog_luafix`, `mwdialog_mastercheck` and `mwdialog_paths` changed upstream). `mwdialog_mastercheck` now
  stores its cache paths as `game:` / `staging:` / `downloads:` tagged relative paths; MWManager does not call it.
- `mwdialog_paths.read_vortex_settings()` now remembers its answer for a run. MWManager stays open for a long
  time, so it asks for a fresh read without changing the module: `mwmanager_detect.detect_vortex` passes
  `refresh=True`, and `mwmanager_gui.detect_vortex_downloads` calls `clear_vortex_settings_cache()` first
  (`tests\test_upstream_paths.py`). The one-shot CLI reads (`--init`, `nexus_updates.py`) keep the memo.
- `mwmanager.py` stayed a fork of the pre-split `mwdialog_check.py` until the integration below.

## Integration: MWManager runs the Steam tools unchanged — 2026-11-14 (Pacific)

The fork is gone. Every upstream file in `tools\steam-upstream\` that MWManager runs is also in the project root,
byte-identical (`tests\test_game_writes.py` checks every `mwdialog_*.py` and `vortex_tidy.py` against
`COPY_MANIFEST.json`): `mwdialog_check.py` and its 15 split modules, `mwdialog_assets.py`, `vortex_tidy.py`,
`mwdialog_luafix.py`, `mwdialog_lua.py`, `mwdialog_planfile.py`, `mwdialog_paths.py`, `mwdialog_text.py`,
`mwdialog_longpaths.py`, `mwdialog_mastercheck.py`. Never edit them; a sync copies the new upstream files into
both places, then runs the tests.

MWManager's own behaviour is in extension modules, laid over the upstream modules by `mwmanager_upstream.py`
(each layer runs inside the upstream module's namespace, like a subclass body; what it replaces is copied into the
upstream modules that imported the original, so upstream code calls MWManager's version):

| File | What it is |
| --- | --- |
| `mwmanager.py` | entry point; `import mwmanager` is the composed `mwdialog_check` module |
| `mwmanager_upstream.py` | `compose()`, `compose_tidy()`, `use_manager_dir()` (tests), the CLI runners |
| `mwmanager_state.py` | state, records, caches and backups in the MWManager folder (absolute names, so upstream's `os.path.join(game_dir, NAME)` lands there), the asset-repair switch |
| `mwmanager_overrides.py` | the scanner functions MWManager runs differently (`World` subclass, fix, sort, cell conflicts, full, CLI) |
| `mwmanager_init.py` | `--init`, catalog, MWSE / MCP / MGE XE checks, mlox rules, core masters |
| `mwmanager_assets_ext.py` | asset checker settings and MWManager-only asset functions |
| `mwmanager_tidy_ext.py` | Tidy rules for MWManager's per-mod staging, the BAIN/FOMOD option step, Data Files docs |
| `mwmanager_install.py` | the Tidy command line: `--options` → plan → `--apply` (exit 2 = refused) |

Behaviour changes:

- Tidy is upstream's plan / `--apply`: plan mode writes `outputs\mwmanager_vortex_tidy_plan.txt` and changes
  nothing; `--apply` enacts it only if it is still the plan that was reviewed (exit 2 otherwise, nothing changed).
  The GUI runs options + plan and automatically applies the validated plan; command-line plan/apply behavior is
  unchanged.
- Asset repairs are off by default: `--fix` / Generate change plugins and patches but no asset file in Data Files
  (stand-ins, texture reduction, text keys, asset health, short paths) unless `--asset-repair` is given or
  `mwmanager.ini` has `[Repair]` `asset_repair = true`. Tidy's asset check is a report unless the switch is on.
- The Lua check is upstream's (`tools\luajit\` is passed); its patches are journaled in Tidy's journal, originals
  under `backups\luafix\`. The old `outputs\mwmanager_luafix_journal.jsonl` is still undone by `--undo`.
- Tidy journal entries carry their staging package. `--undo --only <package>` reverses that package's moves,
  asset repairs, long-path records, Lua repairs, and FOMOD choices across its Tidy runs without touching other
  staged packages. Removing a tracked deployment uses this package rollback automatically.
- Upstream reads "Data Files next to the script"; MWManager rebinds that to `--game-data-files` at compose time.

## Steam utility sync: refactor 2 and review fixes — 2026-11-14, 08:30 (Pacific)

- Upstream: dejunai/morrowind-conflict-tools @30be84a (Cursor's refactor 2 plus the review fixes; see
  `tools\steam-upstream\MWDIALOG_REFACTOR2.md` section 9). Backup of every file this sync replaced:
  `_backup\pre-r2-sync-<stamp>\` (`SHA256SUMS.txt`, `ROLLBACK.ps1`).
- Copied into both the root and `tools\steam-upstream\`: `mwdialog_check.py`, `mwdialog_mastercheck.py`,
  `mwdialog_records.py`, `mwdialog_cellmods.py`. Snapshot only: `README.md`, `MWDIALOG_REFACTOR2.md`, the mwguard,
  mwperformers and mwstranded `main.lua`, and the upstream tests `test_check.py`, `test_mastercheck.py`,
  `test_paths.py`, `test_major_masters.py`. `COPY_MANIFEST.json` and `UPSTREAM.json` refreshed; user-state
  records keep their earlier snapshot.
- What changes for MWManager: leveled lists keep other mods' levels; a same-size master is "other version" at
  most unless the misses are overwhelming; large plugins find their downloaded original (archive cache v2, rebuilt
  once); Patch for Purists is a major master.
- MWManager changes: `Patch for Purists.esm` is first in `core_addons.BUILTIN_MASTERS` and
  `mwmanager_core_addons.json` (it loads right after `Bloodmoon.esm`, before Tamriel_Data, TR, OAAB, ...);
  the sort rule is in the rules text (see the next section);
  `mwmanager_gui.detect_vortex_downloads` also calls `mwdialog_mastercheck.clear_caches()`. Test:
  `tests\test_core_master_order.py`.

## Patch for Purists sort rule moved to the rules text — 2026-11-14, 08:45 (Pacific)

- Upstream @08ca20d has no built-in sort rule: `mwdialog_check.rules.txt` (Steam root, and the repo's copy) has
  `[NearStart]` / `Patch for Purists.esm` near its top. `mwdialog_check.py` is back to the version without
  `builtin_rules()` (root and snapshot); the snapshot's `mwdialog_check.rules.txt` is refreshed from the Steam root;
  `tests\test_major_masters.py` is replaced by `tests\test_rules_pfp.py`.
- MWManager: `mwmanager_overrides.py` no longer inserts a built-in rule; `mwmanager_rules.txt` has the same
  `[NearStart]` block. Patch for Purists stays first in the major-master list (the `--init` / GUI superset).
  Backup: `_backup\pre-pfp-rule-<stamp>\`.

## [NearStart] / [NearEnd] as [Order] rules — 2026-11-14, 09:10 (Pacific)

- Upstream @874c069: a `[Near*]` block in the user's rules file also orders its own plugins (`[NearStart]` as
  listed, `[NearEnd]` reversed); mlox's files keep mlox's meaning. `mwdialog_check.py` (root and snapshot),
  the snapshot's `CHANGES.md`, `README.md`, `mwdialog_check.rules.txt` (cleaned copy from the Steam root) and
  `tests\test_near_order.py` are refreshed.
- MWManager's own `compute_order` in `mwmanager_overrides.py` has the same change (only for the first rule set,
  `mwmanager_rules.txt`); its `--cell-conflicts` rule checks use upstream's `ordering_rules()`. New
  `tests\test_mwm_near_order.py`. `mwmanager_rules.txt` had no `[Order]` entry repeating a `[Near*]` one.

## The mw* MWSE Lua suite replaces mwquickfixes — 2026-11-14 (Pacific)

- `reference\mwse-lua-guards\` now holds the user's mw* suite exactly as it is in the Steam install: 13 mods
  under `mods\<name>\` (mwguard, mwhangwatch, mwfloatfix, mwstranded, mwcollide, mwperformers, mwinfoloop,
  mwsoundguard, mwsaveguard, mwwarnsuppression, mwcompanionsneak, mwpluginpatches, mweffectinspector) and their
  six `*-metadata.toml` files under `metadata\`. `SOURCE_MANIFEST.json` records each file's Steam source and
  SHA-256; `tests\test_game_writes.py` checks the copies against it and checks that mwguard's default
  `exemptMods` names every suite mod (the Steam `mwguard.json` lists all 13).
- Gone: the consolidated `mods\MWManager\` package (one MCM, nine tabs, its `mwquickfixes` tab with
  CorruptSaveGuard, DeferredDialogueTeleport, SuppressBodyPartWarnings, SuppressShutdownWarnings, CompanionSneak,
  MouseBleedFix) and the `legacy-standalone\` snapshots. CorruptSaveGuard is now mwsaveguard, the two warning
  suppressions are mwwarnsuppression, CompanionSneak is mwcompanionsneak. DeferredDialogueTeleport and
  MouseBleedFix are standalone mods in the Steam install, not part of the suite, and MWManager no longer ships them.
- `--init` (`mwmanager_init.install_mwse_lua_guards`) installs each suite mod to `Data Files\MWSE\mods\<name>\`
  and the metadata files to `Data Files\`, moves `MWSE\mods\MWManager\` and `MWSE\mods\mwquickfixes\` to
  `MWSE\backups\mods\`, and moves files a suite mod no longer has out of its folder to the same place, so
  nothing that is not live stays under `MWSE\mods`. It no longer archives a `mouseBleedFix` folder.
- `tools\steam-upstream\` and `COPY_MANIFEST.json` are unchanged (public repo @874c069): the suite's new Lua is
  not in the public repo yet.
- Upstream note (not changed; `mwdialog_luafix.py` stays byte-identical): its built-in `OWN_MODS` still lists only the
  first eight suite mods plus `mwmanager`. It reads `exemptMods` from `mwguard.json`, which names all 13, so this only
  matters where `mwguard.json` is missing.

## Suite sync: the reviewed mw* suite and one Mod Config Menu entry — 2026-11-14, 11:30 (Pacific)

- Upstream: dejunai/morrowind-conflict-tools @2f5249c (dead-code cleanup, master-check speed-up, the whole mw*
  Lua suite with its review fixes). Backup of every file this sync replaced or edited:
  `_backup\pre-suite-sync-20261114-112848\` (`SHA256SUMS.txt`, `ROLLBACK.ps1`).
- `tools\steam-upstream\` mirrors the public repo's code, docs, tests and Lua suite again, each file copied from
  the Steam root and hash-verified: 47 new files, 14 replaced (kept in `previous-2026-11-14-suite\`).
  User-state records keep their earlier snapshot. `COPY_MANIFEST.json` and `utility_source_manifest.json` are
  refreshed.
- Copied into the root as well (byte-identical, checked by `tests\test_game_writes.py`): `mwdialog_assets.py`,
  `mwdialog_check.py`, `mwdialog_luafix.py`, `mwdialog_mastercheck.py`, `mwdialog_paths.py`,
  `mwdialog_planfile.py`, `mwdialog_text.py`, `vortex_tidy.py`. Upstream removed functions nothing called;
  MWManager's extension modules needed no change.
- `reference\mwse-lua-guards\` is copied again from the Steam install: 15 mods. New: `mwmouseguard` and
  `mwteleportguard` (the former MouseBleedFix and DeferredDialogueTeleport), and `mwwarnsuppression\locate.lua`.
  Changed: mwsaveguard (a rejected save is quarantined, not deleted; the options menu stays shut while a save is
  deferred), mwwarnsuppression (patch sites are found in the running executable, so GOG, Steam and code-patched
  builds are handled or left alone), mwguard, and the settings saving of mwteleportguard, mwmouseguard,
  mweffectinspector and mwpluginpatches. `dejespreplacements` (formerly mwespreplacements) is the user's own replacement for plugins in one
  load order: it was removed from the public repo (@f094ef6) and from the upstream snapshot, it is not in MWManager's suite, and
  `--init` does not install it.
- One Mod Config Menu entry. `reference\mwmanager-menu\` holds MWManager's own `mwmanagermenu` mod. It loads
  before the suite, keeps back each suite mod's settings-page registration, and registers one entry,
  "MWManager", with a tab per suite mod; a tab shows that mod's own page, and closing or switching runs that
  mod's own save. The suite's files stay byte-identical to Steam (the earlier consolidated `mods\MWManager\`
  package carried edited copies instead). `--init` installs it with the suite (`mwmanager_init.LUA_SOURCES`).
  `MWSE\mods\MWManager\` is still a retired folder name and is still archived.
- Tests: `tests\test_unified_menu.py` (the tab list equals the suite; the menu run under LuaJIT with a stand-in
  for MWSE: suite pages go under one entry, other mods keep theirs, switching and closing call the mod's own
  close). `tests\test_game_writes.py`: 15 suite mods, the menu mod is installed, and the "no quickfix files"
  check no longer walks the ignored `_backup\` and `backups\` folders (it failed on them before this sync).
- Not checked: the menu and the Lua changes have not been run inside the game.

## Suite refresh — 2026-11-15 (Pacific)

- Rechecked every tracked Steam-root Python utility against `COPY_MANIFEST.json`: no Python file changed.
- Refreshed the changed mwcompanionsneak, mweffectinspector, mwpluginpatches, mwsaveguard and
  mwwarnsuppression Lua files and the three changed metadata files in both `tools/steam-upstream/` and
  `reference/mwse-lua-guards/`; both manifests carry the new hashes.
- Added the reviewed `mwpluginpatches` modules `mabrDist.lua` and `personalEffectsFix.lua`. Every changed or
  added Lua file parses under the bundled LuaJIT. `dejespreplacements` and mutable Steam configuration/state
  remain excluded for the reasons above.

## Late Steam refresh — 2026-11-15 (Pacific)

- A second hash comparison found later edits in ten Python files. The six runtime modules were refreshed in
  both the project root and `tools\steam-upstream`; `diag\impact.py` and the three upstream tests were refreshed
  in the snapshot. The changes are an asset-scan allocation improvement, safer stream cleanup, narrower tests,
  and unused-import cleanup.
- Refreshed `noAccidentalTheftBookFix.lua`, `personalEffectsFix.lua`, and `mwsaveguard\main.lua` in both the
  upstream snapshot and the distributable Lua reference suite. The copy, suite-source, and utility manifests
  carry the new hashes.

## Steam sync to morrowind-conflict-tools 143ad96 — 2026-11-17 (Pacific)

- Refreshed `mwdialog_check.py` and `mwdialog_luafix.py` (root and `tools\steam-upstream\`), `UPSTREAM.json`, `COPY_MANIFEST.json`
  and `utility_source_manifest.json`.
- Ownership repair now runs in MWManager through the inherited check and `--fix` (`fix_cell_winners`): door owners copied
  with a cloned door (public place: the owner comes off; a home's one resident: it is theirs) and containers/objects
  in a home owned by someone from another town (given to the one resident; never taken off). Everything else is
  reported only. The per-run list of changed and questionable owners is `outputs\mwmanager_door_owners.txt`
  (`DOOR_OWNERS_LIST` in `mwmanager_state.py`); each change is also a line in the fix log.
- New suite mod `mwquicktoggle` (a quick key unequips its item when already equipped; vanilla keys and QuickKeys
  Hotbar Extended) in the snapshot; luafix lists it as one of ours. Not yet in the shipped suite
  (`reference\mwse-lua-guards\`): like mwexitguard, it first needs adding to mwguard's exempt list and the menu.

## Steam sync to morrowind-conflict-tools 804340f — 2026-11-18 (Pacific)

- Refreshed `mwdialog_check.py` and `mwdialog_mastercheck.py` (root and `tools\steam-upstream\`), their tests,
  `UPSTREAM.json` and `COPY_MANIFEST.json`.
- `--cell-conflicts`: an .esm vs an .esp pair now offers `[1] keep <winner> winning` (remembered; nothing changes).
- Ownership: owners `--fix` changes stay WARN; the ones it leaves alone are INFO (still listed in
  `outputs\mwmanager_door_owners.txt`).
- A patch made for a different master version still has its edits to references the master no longer has dropped,
  so `--full` no longer stops on them; remapping, moving, size sync and cell renames stay blocked for it.

## Steam sync to morrowind-conflict-tools 8926bf7 — 2026-11-22 (Pacific)

- Refreshed (root and `tools\steam-upstream\`): `mwdialog_check.py`, `mwdialog_cellmods.py`, `mwdialog_footing.py`,
  `mwdialog_luafix.py`, `mwdialog_records.py`, `mwdialog_redundant.py`; new `mwdialog_mergeplan.py` and
  `mwdialog_pathgrid.py` (`mwdialog_check` imports it; the root copy was missing). Snapshot also gains their tests and
  the repo's `diag\` scripts; `mwfloatfix` is refreshed in the snapshot and the shipped suite. Backup:
  `_backup\pre-mergeplan-sync-20261122-151655\`.
- New state redirected to `outputs\`: `mwmanager_pathgrid_cache.json`, `mwmanager_pathgrid_fixes.txt`,
  `mwmanager_doubled_deleted.txt`, `mwmanager_object_cache.json`, `mwmanager_merge_plan.txt` / `.json`.
- `--merge-plan` / `--plan-size` added to MWManager's command line (read-only; Tools → Merge recommendations… is the
  GUI way). Tools → Auto-merge plugins… runs `--auto-merge --yes` with its follow-up `--fix --multipatch`.
- `--fix` checks pathgrids, reusing `mwmanager_pathgrid_cache.json` for unchanged cells. The float list now also
  sees our own patches' deletions (a dock Cell Fixes deletes still lists the items left on it).
## Steam sync to morrowind-conflict-tools dd6c46d — 2026-11-22 (Pacific)
- Refreshed (root and `tools\steam-upstream\`): `mwdialog_check.py`, `mwdialog_mergeplan.py`; snapshot also gains the
  updated `tests\test_automerge_objects.py` and `RESUME.md`. Backup: `_backup\pre-scriptmerge-sync-20261122-154139\`.
- `--auto-merge` (and Tools → Auto-merge plugins…) now takes plugins with scripts; cells, land and pathgrids still
  keep a plugin out. `--auto-merge` / `--merge-plan` help in `mwmanager_overrides.py` updated to match.
## Steam sync to morrowind-conflict-tools 6e19094 — 2026-11-22 (Pacific)
- Refreshed (root and `tools\steam-upstream\`): `mwdialog_check.py`; snapshot gains the updated
  `tests\test_automerge_objects.py`.
- New user-edited record `AUTO_MERGE_BLACKLIST` → `mwmanager_auto-merge.blacklist.txt` (MWManager folder): one plugin
  per line, `;`/`#` comments; `--auto-merge` never merges what it lists. Help in `mwmanager_overrides.py` updated.
## Steam sync to morrowind-conflict-tools 51c00a8 — 2026-11-22 (Pacific)
- `mwpersonaleffects` `config.lua` / `mcm.lua` (snapshot and `reference\mwse-lua-guards\`): the MCM saved a config table
  holding a function, so MWSE failed to encode `mwpersonaleffects.json`; it now saves a plain copy via `config.save()`.
  Snapshot gains `tests\test_suite_config_save.py`. Backup: `_backup\pre-pe-save-sync-*\`.

## Steam sync to morrowind-conflict-tools f6efcb0 — 2026-11-22 (Pacific)
- New (root and `tools\steam-upstream\`): `mwdialog_anthology_override.py` (Cell Fixes moves objects and door arrivals
  of mods made for land another mod moved, e.g. Anthology Solstheim / Tomb of the Snow Prince +7,+6) and
  `mwdialog_cellorder.py` (cell-filter load-order rules).
- Refreshed: `mwdialog_check.py`, `mwdialog_footing.py` (moved-island rescue), `mwdialog_mastercheck.py` (INFO severities).
- Snapshot tests: new `test_anthology_override.py`, `test_cell_filter_order.py`; updated `test_footing.py`,
  `test_mastercheck.py`. `COPY_MANIFEST.json` and `UPSTREAM.json` refreshed. Backup: `_backup\pre-landmove-sync-*\`.
