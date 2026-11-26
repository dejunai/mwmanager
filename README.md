# MWManager

A Windows GUI and command-line manager for a Steam/GOG Morrowind install (Morrowind + MWSE + MGE XE): it stages,
tidies and installs mod archives (BAIN/FOMOD options included), sorts the load order, checks plugins, and builds
MWManager's repair patches.

MWManager runs on **Python 3.9 or newer**. More detail on recent features is in [`MORE_INFO.md`](MORE_INFO.md).
First-run checks, tools and OpenMW-in-staging behaviour are in [`PREREQUISITES.md`](PREREQUISITES.md).

## Depends on morrowind-conflict-tools

MWManager is built on the Steam tools in
[dejunai/morrowind-conflict-tools](https://github.com/dejunai/morrowind-conflict-tools) and runs them
**unchanged**. The copy it uses is vendored at the commit named in [`UPSTREAM.json`](UPSTREAM.json):

- `tools/steam-upstream/` is the upstream file set (`COPY_MANIFEST.json` lists every file and its SHA-256);
- every `mwdialog_*.py` and `vortex_tidy.py` in the repository root is a byte-identical copy of the same file
  there (checked by `tests/test_game_writes.py`). Don't edit them: to update, copy the new upstream files into
  both places, update `UPSTREAM.json` and `COPY_MANIFEST.json`, and run the tests.

MWManager's own behaviour is in extension modules that `mwmanager_upstream.py` lays over the upstream modules:

| File | What it is |
| --- | --- |
| `mwmanager.py` | entry point; `import mwmanager` is the composed `mwdialog_check` module |
| `mwmanager_upstream.py` | `compose()`, `compose_tidy()`, `use_manager_dir()` (tests), the command-line runners |
| `mwmanager_state.py` | state, records, caches and backups in the MWManager folder; the asset-repair switch |
| `mwmanager_overrides.py` | the scanner functions MWManager runs differently |
| `mwmanager_init.py` | `--init`, catalog, MWSE / MCP / MGE XE checks, mlox rules, core masters |
| `mwmanager_assets_ext.py` | asset checker settings and MWManager-only asset functions |
| `mwmanager_tidy_ext.py` | Tidy rules for the per-mod staging folders, the BAIN/FOMOD option step |
| `mwmanager_install.py` | Tidy command line: `--options` -> plan -> `--apply` (exit 2 = refused, nothing changed) |
| `mwmanager_gui.py` | the GUI (`python mwmanager_gui.py`) |
| `mwmanager_workflow.py` | persistent setup window, progress presentation, safe-stop controls and native conflict picker |
| `mwmanager_batch.py` | durable major-master batch state, serialized option events and extraction/Tidy worker pipeline |
| `mwmanager_recovery.py` | read-only inventory and scoped recovery for interrupted deployments |
| `mwmanager_profiles.py` | save, compare, export and apply manager-state profiles |
| `mwmanager_integrity.py` | deployed-file integrity checks and regression snapshots |
| `mwmanager_install_plan.py` | deterministic read-only plans for staged plugin deployments |
| `mwmanager_update_impact.py` | staged-version file and TES3-record impact comparison |
| `mwmanager_record_report.py` | catalog-based record conflict explanations |
| `mwmanager_mwse_core.py` | Tools > Update MWSE, the --play KnownBug re-patch, and the `[MWSE]` ini keys |
| `mwmanager_core_game_files.py` | the --play check of the six shipped ESM/BSA files, and the `[Integrity]` ini key |

Other modules: `plugin_install.py`, `core_addons.py`, `major_master_deploy.py`, `mock_fomod.py`, `preinstall.py`,
`mwmanager_detect.py`, `mwmanager_journal.py`, `mwse_lint.py`, `nexus_updates.py`, `mwnexus_*.py`,
`bsa_pack.py`, `swap_master_variant.py`, `database/`. More in [`MORE_INFO.md`](MORE_INFO.md),
[`TOOL_SYNC.md`](TOOL_SYNC.md) and [`PREREQUISITES.md`](PREREQUISITES.md).

## Not in this repository

Per-install settings and state (`mwmanager.ini`, GUI settings/state, load order, caches, `outputs/`, `backups/`,
`staging/`, `disabled_plugins/`) are created on first run and ignored by git. Third-party binaries go in `tools/`
yourself: LuaJIT in `tools/luajit/` (used by the Lua check), `texconv.exe` (DirectXTex), and the BSA packer.
`mlox/` rule files can be downloaded during first-run preflight, or by `--init`, from the community mlox-rules project on GitHub. After first run, Tools > Update rules… and `--update-rules` fetch only if newer, then comment out conflicting rules in the live mlox files and, if it already exists, obsolete rules in `mwmanager_rules.txt`. Put personal rules in `mlox/mlox_my_rules.txt`; that file is never overwritten by a download. Community files are not backed up (the originals stay on GitHub); `mlox_my_rules.txt` keeps at most one rolling backup when MWManager has to change it.

`mwmanager_core_hashes.json` is shipped as immutable verification data; first-run code reads it but never
regenerates or clears it.

## Author-facing bugreports

Plugin bugreports under `outputs/bugreports/` contain only the plugin's own defects and compatibility issues
with its declared master chain. Unrelated mod interactions and their repairs stay in the main diagnostic
report. A master mismatch and its stale-reference details become one compatibility summary line per plugin;
independent defects remain detailed. Regeneration removes stale scanner findings without removing other
tools' sections. Package-wide Lua, packing and asset reports stay with their staged package; when it contains
multiple plugins, they are not attributed to the first plugin. Asset repairs are reported, except size-only
optimisations (textures or splash screens over 2048 shrunk, splashes re-encoded smaller); missing mipmaps
and splash alpha channels are bugs.

## GUI setup and staging

First-run setup keeps one progress window through prerequisite checks, sort/Tidy, patch generation, the
catalog, and major-master staging. Its checklist follows the saved setup checkpoints. Use **File > Resume
setup** after a pause; optional community masters can also be explicitly skipped and added later.

- **Automatic Tidy:** installer choices remain interactive, but the GUI builds and applies the plan with
  `--apply --yes` without asking for approval of individual moves or repairs. Output scrolls in the shared log.
  Existing asset-repair settings still apply. A changed/refused plan or partial apply stops deployment.
- **2048 limits:** the setup and Options windows offer *Reduce textures larger than 2048 to 2048* and *Reduce
  splash screens wider than 2048 to 2048* (`mwmanager.ini` `[Staging]` `reduce_textures_2048` / `reduce_splash_2048`,
  default true). Off keeps full size; mipmap, alpha and non-TGA splash repairs still apply.
- **Readable progress:** the window is resizable; explicit stage/archive counts use determinate progress.
  Scrolling up pauses automatic following. The Follow output checkbox jumps back to the latest output.
- **Safe stopping:** Stop after current operation requests a cooperative stop. File operations and running
  utilities finish their current unit before stopping at a checkpoint; this is not an instant thread kill
  or a rollback of completed work. Long scans check between reported operations. A partially processed
  master stays marked as needing processing, not ready for deployment.
- **Major masters:** Find / stage downloads scans new archives; Reopen staged options processes existing
  staging without extracting again. Staging and deployment are separate. Deployment lists the selected
  masters and asks about loose assets before running preflight, then deploys only that selection in the
  configured priority order. Shared-package assets may serve more than one selected master.
- **Review major masters:** if you untick a master here, options that need it are held until that master is
  selected again (they are not offered as if the master were coming later in the same batch). When both the
  HD and SD Tamriel_Data archives are available, **Use HD** / **Use SD** swaps the deployed files in place
  and keeps TR, Sky and PC deployed in the same load order. Selecting or enabling Solstheim Tomb of the Snow
  Prince warns that it replaces Bloodmoon's land.
- **Queued major-master staging:** archive extraction runs ahead of the option queue while answered packages
  enter the Tidy/apply worker. Option and Tidy subprocesses are serialized because they share journals;
  archive extraction continues in parallel. Installers discovered after option activation
  are persisted and presented next without interrupting the active dialog. Per-package stages, option answers,
  and the batch manifest recover independently after a stop or crash. Cancellation waits for both workers
  to stop before releasing the mutation lock. Rescans compare option choices, not their save timestamps.
- **Option requirements:** the selector lists plugin masters and curated asset-only patch dependencies before
  their descriptions. Curated informational requirements do not themselves queue or block an installation.
  An unverified Nexus ID is shown as unknown until the user picks a source.
  <!-- REMOVE BEFORE SHIPPING: developer-only option editor documentation. -->
  Developers can set `[Dev] edit_option_requirements = true` in `mwmanager.ini` to edit a displayed Nexus ID
  or add an informational requirement for any shown option. Edits are saved per package and option in
  `outputs/mwmanager_option_requirement_overrides.json` and appear immediately in the details pane.
  `[Dev] edit_option_insights = true` enables an Insights button that saves option-specific text directly to
  `mock_fomod_notes.json`; closing the editor saves and refreshes the description.
- **Legacy staging:** stages created before processing-complete records existed must pass Reopen staged
  options once before deployment. This prevents an interrupted extraction from being mistaken for a
  fully processed package just because its ESM exists.
- **Preflight reuse:** a successful preflight may be reused within the current session while the observed
  core/prerequisite file metadata is unchanged. Changed files, unreadable metadata, or a new session
  require preflight again. This is a performance cache, not a content-integrity guarantee.
- **Conflict decisions:** GUI patch generation uses a native picker instead of opening a console.
  Detected moved-land incompatibilities require choosing one plugin family; the picker lists dependents to
  set aside (even when the game and manager are on different drives) and explains when an active merge must
  first be undone. Skip or Finish leaves an unresolved
  incompatibility active and stops patch generation. Builds recheck after sorting, unreadable plugins stop
  the check, and a failed build does not launch the game. Other conflicts can still be skipped. Detection is
  not exhaustive: variable-driven scripts and Lua dependencies may need separate review.
- **Archive trust checks:** scanned ZIP/7-Zip/RAR payloads are classified before extraction; unsafe paths,
  reparse/link entries, encryption, excessive expansion, high compression ratios and blocked payloads stop install.
- **Recovery center:** interrupted regular and major-master deployment state, ready plugin stages and journal
  problems are listed in one place; only a selected, validated recovery item is changed.
- **Profiles and review tools:** the Tools menu can capture manager-state profiles, inspect deployment integrity
  and catalog record conflicts, preview staged installs, compare staged updates, and compare integrity snapshots.
  Profiles restore manager choices only; they do not implicitly redeploy or remove game files.
  **Tools > Update rules…** and **Tools > Update MWSE…** are described under Playing and updates below.
- **Automatic drift notice:** a low-overhead startup check hashes tracked deployed files in the background and
  reports missing, changed or unverifiable files without blocking the interface.
- **Vortex paths:** when the Archive Downloads source is Vortex, MWManager reads Vortex's download and install
  paths from its `state.v2` LevelDB under `%APPDATA%\Vortex` (read-only; no Vortex process is required).
- **Regression corpus:** `python tools\archive_corpus.py OUTPUT_DIR` creates a deterministic set of safe and
  adversarial archives used to exercise path, payload, link and compression safeguards.

## Playing and updates

**Tools > Update MWSE…** (and the same official updater during `--init` and `--full`; preflight has its own prompts) compares `mwse-version.txt`
with the official build-automatic release, runs `MWSE-Update.exe` if you confirm, then re-applies KnownBug
patches on MWSE core Lua files (today KB3 and KB4 in `exclusionspage.lua`). `--play` does a quick local check
and re-applies if the MWSE build or a patched core file has changed. If a patch no longer matches or cannot
be applied, Play stops the same way it does for a missing master; `--play-anyway` still starts the game.
These are controlled in `mwmanager.ini` under `[MWSE]` (all default true; there is no Options-window toggle):

- `check_on_play` -- check and re-apply on Play
- `apply_core_patches` -- actually write the KnownBug patches
- `offer_update_on_full` -- offer the updater during `--full`

On Play, MWManager also hashes the six shipped files (`Morrowind.esm`, `Tribunal.esm`, `Bloodmoon.esm`,
`Morrowind.bsa`, `Tribunal.bsa`, `Bloodmoon.bsa`) against known-good values. Unchanged files stay silent. If
any have changed, a warning strongly suggests restoring them but does not block Play. **Don't warn again**
mutes only the current hash of each altered file; a further change brings the warning back (stored in
`outputs/core_game_file_mute.json`). Turn the check off with `[Integrity] check_core_game_files = false`.

When Play (or sync) deploys the MWSE Lua suite from `lua tools\` into `Data Files`, it will not put an older
copy over a newer deployed file (hash lineage in the deploy manifest; modification times only when that
history is unknown). Your `MWSE\config\` settings are only copied when the file is missing.

## OpenMW Converter

MWManager is for the original Windows Morrowind engine (OpenMW installs are not supported). A separate
**OpenMW Converter** project, [dejunai/openmw-mwse](https://github.com/dejunai/openmw-mwse), is aimed at
turning OpenMW-oriented content toward MWSE. It is experimental and in early development; its repository
is private for now. See [that project's own README](https://github.com/dejunai/openmw-mwse) for current
status. This is not the same as tidy's handling of `.omwaddon` files and OpenMW resources in a vanilla
staging folder, which is described in
[`PREREQUISITES.md`](PREREQUISITES.md#openmw-content-in-vanilla-staging).

## Tests

```
python -m pytest -q -p no:cacheprovider tests
```

The tests only use temporary folders; they never touch a game install, Vortex, or MWManager's own state.
They are meant to pass on Python 3.9 and newer. GitHub Actions does not run them on every push or pull
request; the workflow is started by hand (`workflow_dispatch`).

## Credit

dej (dejunai)
