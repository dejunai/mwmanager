# MWManager prerequisites

MWManager targets the original Windows Morrowind engine and its `Data Files` directory. OpenMW installations are not supported.

## First window and preflight

On first run MWManager reads the Windows registry (read-only) before anything else: GOG, Steam (including
every Steam library), Epic and other uninstall entries for Morrowind, and Vortex (uninstall entry, App Paths
or the `nxm://` handler). Mod Organizer 2 is recognised but not supported yet. The first window lists every
Morrowind install it found, so someone with both a Steam and a GOG copy picks the one to manage (or browses
to another). Below that is **Archive Downloads**, the folder MWManager reads mod archives from. It is
manager-agnostic: pick a detected manager (Vortex now, read from Vortex's own settings; Mod Organizer 2 is
shown but not supported yet; more managers plug into `MOD_MANAGER_DETECTORS`) or "None / unmanaged" and
browse to any folder you keep archives in. `mwmanager.ini` stores it as `archive_downloads_dir` (an older
`downloads_dir` is still read) and the settings JSON stores the source as `archive_source`
(`vortex`, `unmanaged`, ...; an older `uses_vortex` is still read). On first run, approving
saves those paths and starts preflight right away. `File > Detected installs…` reopens the window later;
approving it then opens the options window with the chosen paths, and nothing changes until that window
is saved. `python mwmanager_detect.py` prints the same detection.

Preflight performs these checks before offering any prerequisite installation:

1. **Archive support.** MWManager accepts ZIP, 7z and RAR packages. It uses full 7-Zip from `PATH`, the
   standard Windows install folder, or `MWManager\tools\7zip`. If none is found, preflight offers to download
   the current official Windows installer and installs it privately under that tools folder. The installer is
   retained as `tools\7zip-installer.exe`.
2. **Morrowind.exe** exists. It is never hashed: it differs by store and the Code Patch rewrites it.
3. **Morrowind.esm, Tribunal.esm, Bloodmoon.esm and their three BSAs** exist and match built-in SHA-256
   values, which are the same on every store. A modified file is a warning you can continue past.
4. **Morrowind.ini** exists, in any state. Start the game's launcher once if it is missing.
5. **MWSE** ([Nexus](https://www.nexusmods.com/morrowind/mods/45468)): `MWSE.dll` must exist. `MWSE-Update.exe`
   writes the installed build's commit to `mwse-version.txt`; preflight compares it with the build the
   updater would install and highly recommends running the updater immediately after MWSE installation.
6. **Morrowind Code Patch with MCP Beta: Skunk Works** ([Code Patch](https://www.nexusmods.com/morrowind/mods/19510),
   [Skunk Works](https://www.nexusmods.com/morrowind/mods/26348)). Required. The check is
   `mcpatch\describe.json` (`description.json` is accepted too): its first category must be `"Beta"`.
7. **MGE XE**. Fresh installs use only the
   [Greatness7 G7 fork](https://www.nexusmods.com/morrowind/mods/59957); the Hrnchamd Nexus fork is not offered.
   G7 keeps its settings in `mgeXE.toml`, written the first time `MGEXEgui.exe` runs, so that file confirms it.
   A different build already installed is accepted, but preflight recommends G7 without stopping.

**Guided install order: MWSE, Code Patch, Skunk Works, then MGE XE.** For each missing component MWManager
searches both the selected Archive Downloads folder and `MWManager\pre-reqs`. The prompt offers the newest
matching archived version or a fresh download. Known Nexus-41102 MGE XE archives are excluded. Fresh downloads
are saved under `pre-reqs\<component>\`; Nexus
remains visible for CAPTCHA/security checks. ZIP/7z/RAR packages are path-checked before extraction, retained in
`pre-reqs`, and portable payloads are copied into the selected Morrowind folder. Executable installers are
launched only after confirmation.

After Skunk Works is installed, MWManager opens the Code Patch interface with category-specific guidance and
reminds the user to click **Apply chosen patches**. After MGE XE, it opens `MGEXEgui.exe` once to generate the
Greatness7 TOML and keeps recommended settings visible: VSync off; shaders on for a current GPU; Very High
shaders with Depth of Field on for an above-average system; frame limiter 300; Pause rendering in menus,
Crosshair autohide, Allow yes to all, and Allow screenshots enabled; and Light Attenuation Constant 0.382,
Linear 0 with per-pixel lighting, and Quadratic 2.619. The first
game launch then records the final configured Code Patch, MGE XE and MWSE setup in `mgeXE.log` and `MWSE.log`.

**Confirm your setup works: launch Morrowind once.** `mgeXE.log` and `MWSE.log` only appear after the game
has started once. When they are missing, or older than the installed Code Patch, MGE XE or MWSE (for example
right after `MWSE-Update.exe` ran), preflight stops before the starting-state snapshot and any deploy and asks
you to launch Morrowind once, wait for the main menu, and quit. This proves the setup starts before MWManager
changes anything. The prompt has four buttons:

- **Launch Morrowind** starts the selected install's `Morrowind.exe`, only when you click it. MWManager never
  waits on, watches or closes the game; quit it yourself at the main menu.
- **Re-check** re-reads the logs and reports the MGE XE and MWSE versions and the Code Patch features found.
- **Skip** continues after a warning; the setup is then recorded as not confirmed.
- **Stop** ends preflight.

MWManager does not bypass Nexus or GitHub download controls. It opens the official page, lets the user complete
the download, retains the selected file under `pre-reqs`, and then processes it locally.

After preflight passes, the first run offers to record a snapshot of the install as it is (a fresh install
or an existing playthrough with mods) in `journal\baseline`. Rollback returns the game folder to that
snapshot. From the command line: `mwmanager.py "<game>\Data Files" --capture-baseline`, `--verify`,
`--rollback --dry-run`.

The baseline is bound to a store-install marker (the GOG hash database when present, otherwise a stable
store/launcher file). If repair, verify, or reinstall replaces that marker, preflight refuses to reuse the old
baseline. With confirmation it moves the complete prior baseline, ledger, inputs, and preserved originals under
`journal\history`, then records the selected install again. Nothing is discarded.

Every GUI deployment also joins `journal\install.jsonl`: regular mod files, major-master files, replaced Lua,
Morrowind.ini archive registrations, generated/repaired ESPs, generated assets, official-file moves, and the
pre-`--full` MWSE Lua suite. Originals are copied into the journal's content-addressed `originals` folder before
they are changed. The component-specific deployment manifests remain for uninstall/redeploy, but are not used as
a substitute for the install rollback ledger.

## Tools

These are used by the repair workflows, not by preflight.

The managed 7-Zip copy comes from [7-zip.org](https://www.7-zip.org/), is licensed under the GNU LGPL, and
remains replaceable in `tools\7zip`; source and license information are available from that site.

1. **texconv.exe** — Microsoft DirectXTex texture converter, required by asset repair for DDS conversion and mipmap generation. The current MWManager package includes it under `tools` with its MIT license notice. If it is missing, initialization offers a window button to the [official texconv page](https://github.com/microsoft/DirectXTex/wiki/Texconv).
2. **FFmpeg** — required by `--full` to normalize audio: WAV to 16-bit PCM, game sounds to mono, audio to 44.1 kHz, and MP3 files without oversized metadata tags. MWManager does not bundle a third-party FFmpeg build. The distribution plan is to ship a specifically built LGPL-only FFmpeg with the exact source, build recipe, notices, and replaceable shared libraries needed for compliance; until that build is prepared and verified, missing FFmpeg opens a window button to the official [FFmpeg download page](https://ffmpeg.org/download.html), which lists Windows builds. Put `ffmpeg.exe` in MWManager `tools` or add it to `PATH`; MWManager does not place tools in the game folder.

On the command line (`--init`, `--full`), MWManager also checks MWSE and skips the updater when the installed build marker matches upstream. Declining an update or an updater error does not block the workflow. A dry run performs the read-only freshness check and reports whether it would prompt, without launching the updater.

## Deployment preflight

The GUI runs the same preflight (above) before plugins or major masters are deployed. Its overrides (a
modified core file, MGE XE not confirmed as G7, MWSE update declined or unknown) are recorded in
`outputs/mwmanager_preinstall_audit.json`. Preflight no longer scans for plugins, MWSE.log, mlox rules,
write access or drive letters; staging-drive and path-length checks happen in the deploy steps themselves.

## Interactive Nexus browser

The FOMOD option helper uses a visible, persistent Microsoft Edge WebView2 window for Nexus pages. This lets you sign in and complete Nexus CAPTCHA/security challenges inside MWManager's browser, then return the rendered page text to the option details panel. The browser profile is stored under `%LOCALAPPDATA%\MWManager\NexusBrowser\webview2-profile`; it is separate from your regular Edge profile. MWManager does not read or copy browser cookies. Install the browser packages with `python -m pip install -r requirements-nexus-browser.txt`. Microsoft Edge WebView2 Runtime is also required; it is normally installed with Microsoft Edge. The package notices are in `THIRD-PARTY-NEXUS-BROWSER.txt`.


## MWManager files

MWManager keeps reports, caches, and journals in its `outputs` folder, and keeps plugin, INI, and asset backups in `backups`. Required game content remains in Morrowind's `Data Files` folder. Keep the `tools` folder beside the manager for utilities such as `texconv.exe`.

MWManager records SHA-256 fingerprints for `Morrowind.esm`, `Tribunal.esm`, `Bloodmoon.esm`, `Morrowind.bsa`, `Tribunal.bsa`, and `Bloodmoon.bsa` during initialization, using existing catalog hashes as the initial reference when available. The two DLC BSA hashes without catalog references are captured from the installed game on the first successful initialization. Later `--init` and `--full` runs warn if one differs and recommend verifying/restoring the game files through GOG Galaxy. The warning does not block use. The MCP-patched executable is excluded because MCP intentionally alters it. On a first setup without prior catalog hashes, verify the game installation before initialization: a first-use fingerprint detects later changes but cannot establish that a file was already clean.

FFmpeg's upstream project uses LGPL by default, while enabling optional GPL or nonfree components changes redistribution requirements. Our eventual bundled build must be built without GPL/nonfree components, include matching source and build instructions, and account for LAME and every other linked library. Until those checks are complete, the downloader stays user-directed. See [FFmpeg's licensing guidance](https://ffmpeg.org/legal.html) and [LAME's license information](https://lame.sourceforge.io/about.php).

After catalog initialization succeeds, `--init` runs the one-time Data Files document tidy: loose root-level documents move into `Data Files/docs`; a document whose filename matches an installed ESP/ESM stays unprefixed, and unmatched documents receive `No Known Mod - ` before the original filename. Completion is recorded per game installation in MWManager `outputs`. The tidy is journaled for undo. After it succeeds (or has already completed), `--init` continues as `--full --yes`, processing the three core ESMs, the existing independent load-order entries, and the shipped standalone ESPs as ordinary plugins and building the applicable MWManager patches. `--init --dry-run` shows the document and full-processing plans without changing files. During `--init`, MWManager also lints Lua files in the installed `Data Files\MWSE` core folder and writes findings to `outputs/mwmanager_core_mwse_lua_report.json`. The pass applies conservative repairs to installed core Lua, saves originals under `backups/mwse-core`, and writes a sendable report to `outputs/mwmanager_core_mwse_bugreport.txt`. `--init --dry-run` prints the lint plan without creating the report.

The GUI first run installs the 20-mod mw* MWSE Lua suite after core integrity and existing-plugin decisions, and after the clean baseline is recorded, but before the initial `--full` cycle. CLI `--init` installs the same suite. Its source is `reference/mwse-lua-guards/` (byte-identical to the Steam install's copies), plus MWManager's `mwmanagermenu`, which presents their settings under one Mod Config Menu entry. Each mod goes to `Data Files/MWSE/mods/mwsuite/<name>/` and its metadata file to `Data Files/`. Older flat suite folders and MWManager's earlier consolidated package are moved to `Data Files/MWSE/backups/mods/`. Move and redeploy any Vortex-staged flat suite folders first, so redeployment cannot restore duplicate copies. Settings in `Data Files/MWSE/config/` are left as they are. `--init --dry-run` lists these moves without making them.

Before the GUI's initial `--full`, all eight installed shipped ESP add-ons and XE Sky Variations are enabled, then `mwmanager_install.py --init --game-data-files <path> --apply --yes` tidies the live initial installation and applies safe plugin and asset repairs. A standalone `--sort --yes --no-follow-up` then sorts that complete shipped load order before `--full`. Tidy's per-file asset facts are committed to `outputs/mwmanager_asset_cache.sqlite` before `--full`, so the following scan reuses unchanged model, texture, sound, BSA, and reference results; repaired or replaced files are checked again. Damaged shipped assets are retained under `Data Files/Off/Original Install/<feature>/<original path>`; any shipped asset moved there is also preserved and recorded in the install journal. Originals of shipped ESPs patched in place are retained under MWManager's `backups/plugins/Official Add-Ons/`. The following `--full` also enables asset repair so missing vanilla assets receive the normal recorded stand-ins before its final error gate. Every generated stand-in is listed in `outputs/mwmanager_generated_files.txt` and journaled as a new install file, so rollback deletes it; generation is refused until an install baseline exists. GUI-launched subprocesses are hidden, and a failed full report is shown in MWManager rather than external Notepad.


## Major community masters

`mwmanager_core_addons.json` is the editable, ordered list of major community master ESMs and download-name keywords. The built-in seed list starts with Patch for Purists (`Patch for Purists.esm`, right after `Bloodmoon.esm`; `--sort` places it there through the `[NearStart]` rule in `mwmanager_rules.txt`) and includes Tamriel Data, Tamriel Rebuilt (`TR_Mainland.esm`), OAAB Data, AATL Data, Sound Data, and Dr_Data. Edit the JSON directly, or use `mwmanager.py "<Data Files>" --core-master-list`, `--core-master-add "New_Master.esm" --core-master-keywords "archive name"`, and `--core-master-remove "New_Master.esm"`. During `--init`, installed masters in this list are placed after the three vanilla ESMs and before previously listed dependent plugins; their order in the JSON sets their priority. Add future masters to the list as they are identified. The same initialization scans the configured Vortex downloads folder and writes a reviewable plan to `outputs/mwmanager_core_addon_plan.txt` and `.json`. The scan records archive SHA-256 hashes, blocks archives that cannot be inspected or contain unsafe paths/Windows filename collisions, and reports paths supplied by more than one selected master archive. Review blocked archives and possible asset overwrites before installing; the scanner never extracts archives. Plugin master/record checks and conflict reporting run through `--full` after the masters are staged; it resolves asset references against registered BSAs. Companion BSA registration and unsupported-folder repacking remain a post-extraction step described below.

The download scan compares archives by the exact plugin filenames they contain. When multiple archives contain the same plugin, it selects the highest version identifiable in the Vortex/Nexus filename, using the embedded upload date and then file modification time to break ties. Older archives are listed as ignored. If an archive is still selected for another plugin it contains, its older duplicate plugin is explicitly marked to ignore in the plan.

The current plan stage only inventories downloaded packages. The installation workflow must register each installed companion BSA in `Morrowind.ini`; before registration it must inspect the BSA and, for vanilla-incompatible top-level folders, unpack and repack the archive with those folders removed using `tools/bsapack.dej.exe`. The initial folder policy is editable in `mwmanager_bsa_unsupported_folders.txt`.


## Packing large staged mods

After a mod is extracted into its own folder under the MWManager staging `Data Files`, run `bsa_pack.py <mod-folder> <staging-Data-Files> --ini <staging-Morrowind.ini>`. The first-run option `Offer BSA when this many assets are found` is stored as `[Staging] offer_bsa_when_assets_found` in `mwmanager.ini`; it defaults to 10,000 and accepts integers of at least 50. At that threshold MWManager asks whether to pack the compatible `Meshes`, `Textures`, and `Icons` assets with `tools/bsapack.dej.exe`. Declining leaves the files loose. Folders listed in `mwmanager_bsa_unsupported_folders.txt` stay loose. After a successful and validated build, the BSA is registered in the staging INI, the original INI is backed up to `<mod>/Off/bsa-pack/<plugin>/ini/Morrowind.ini.bak`, and the loose assets are moved to `<mod>/Off/bsa-pack/<plugin>/<original path>`. Run this only after a mod has been extracted into MWManager staging, never against the live GOG or Vortex download folders.

## Numeric BAIN/FOMOD options

When `vortex_tidy.py --apply` tidies a staged mod without `fomod/ModuleConfig.xml`, it scans its `Extras` folders for numbered choices such as `00 - Core` and `10 - Optional patch`. The Tk selector locks every `00` option on, makes repeated numbers mutually exclusive, and applies the remaining selected groups in numeric order. It previews text documentation from the option folders and the mod's `Docs` folder. An option plugin's additional `MAST` entries and plugin references in its docs are checked against the staged `Data Files`; matching compatibility options are selected by default. Selected payloads are activated in the mod folder, with replaced originals in `Off/fomod-options/<plugin>/<original path>`.

The compatibility journal is `outputs/mwmanager_compatibility_journal.json`. When a later plugin appears in staged `Data Files`, rerun tidy for that mod (or the staging set); the selector will revisit previously recorded options that target the new plugin. Use `--only "<mod folder pattern>"` to process one mod. Pass `--game-data-files` when staged `Data Files` is not at the MWManager default. `--undo` also restores files activated by the mock selector.

## OpenMW content in vanilla staging

`vortex_tidy.py` converts compatible `.omwaddon` plugins to `.esp` and rewrites matching `MAST` names in staged plugin headers. It also raises an older staged plugin HEDR version to the highest available version among its masters. These are header-only changes; record-level incompatibilities are left for MWManager's main plugin repair pass. A shared cache in `outputs/mwmanager_tidy_backup_cache.json` records one original header backup per staged plugin file, reused across later tidy passes and restored by `--undo`. OpenMW scripts and resources (`.omwscripts`, `scripts`, `l10n`, `animations`, and `playlists`) are moved under `Off/OpenMW/<plugin name>/`, outside the vanilla game payload. Normal maps are moved under `Off/normal maps/<plugin name>/`. A legacy root-level `OpenMW/` folder from an earlier tidy run is migrated into the same plugin-owned Off tree. When a package has OpenMW scripts but no `.esp` or `.esm`, tidy alerts the user and retains its `.omwaddon` and scripts under `Off/OpenMW-only/<plugin name>/` for possible future MWSE conversion. Remove the mod through Vortex if you choose to uninstall it.

## Asset filename pass

Before the other asset checks, tidy scans asset paths (including `DistantLand`) for non-ASCII characters. It renames paths to ASCII and updates same-length references in the mod's own plugins when no active or staged plugin names the original path. Paths named by another plugin stay protected. When an unreferenced non-ASCII file already has its normalized ASCII counterpart, tidy moves the redundant source to `Off/redundant/<plugin>/<original path>` and omits it from findings and repair plans. The remaining texture, mesh, and sound checks then run against the cleaned paths.

## Bug reports for plugin authors

When tidy applies asset checks, it writes or updates a report at `<staged mod>/bugreports/<plugin name>.bugreport.txt`. The report identifies each offending file and records successful changes to that file, including where its original was saved. It excludes orphan judgments and findings or repairs that depend on accommodating another plugin. The header reads Vortex's `meta.ini` for the mod display name, author, version, and Nexus/custom page URL, and includes the plugin header author, description, and masters. Existing reports are updated in place and old centralized tidy sections are cleared. Reports are drafts for review and manual posting; MWManager does not post to Nexus forums.

## MWSE Lua review

After tidy and option selection, `vortex_tidy.py` scans Lua files under each staged `MWSE` folder. It automatically guards narrow, common nil-risk patterns (reference `data` reads, optional event fields, and keybind `keyCode` reads), normalizes numeric priorities outside `-10000..10000` to `10000`, and redirects `mwse.log()` calls inside recurring frame callbacks to `Data Files\MWSE\logs\<Module>.log`. Candidate edits are checked with `luac` or `luac5.1` before writing when a checker is installed. Changed source is backed up under its staged package's `Off\LuaRepair\` tree. Findings and exact edits are recorded in `outputs/mwmanager_mwse_lua_report.json` and the sendable `outputs/mwmanager_mwse_bugreport.txt`. Nil guards are intentionally limited to recognizable safe cases; frame scheduling, UI lifecycle, and other behavior-dependent findings remain documented for review. Dry runs report proposed changes without editing Lua files.

The nil and frame scheduling findings are review recommendations: Lua source alone cannot establish that a reference is always available or that an every-frame callback is unnecessary. Camera control, smoothing, animation, and other same-frame input work should remain per-frame when required by its behavior.
