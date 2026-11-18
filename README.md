# MWManager

A Windows GUI and command-line manager for a Steam/GOG Morrowind install (Morrowind + MWSE + MGE XE): it stages,
tidies and installs mod archives (BAIN/FOMOD options included), sorts the load order, checks plugins, and builds
MWManager's repair patches.

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
| `mwmanager_install.py` | Tidy command line: `--options` → plan → `--apply` (exit 2 = refused, nothing changed) |
| `mwmanager_gui.py` | the GUI (`python mwmanager_gui.py`) |
| `mwmanager_workflow.py` | persistent setup window, progress presentation, safe-stop controls and native conflict picker |
| `mwmanager_recovery.py` | read-only inventory and scoped recovery for interrupted deployments |
| `mwmanager_profiles.py` | save, compare, export and apply manager-state profiles |
| `mwmanager_integrity.py` | deployed-file integrity checks and regression snapshots |
| `mwmanager_install_plan.py` | deterministic read-only plans for staged plugin deployments |
| `mwmanager_update_impact.py` | staged-version file and TES3-record impact comparison |
| `mwmanager_record_report.py` | catalog-based record conflict explanations |

Other modules: `plugin_install.py`, `core_addons.py`, `major_master_deploy.py`, `mock_fomod.py`, `preinstall.py`,
`mwmanager_detect.py`, `mwmanager_journal.py`, `mwse_lint.py`, `nexus_updates.py`, `mwnexus_*.py`,
`bsa_pack.py`, `database/`. More in [`TOOL_SYNC.md`](TOOL_SYNC.md) and [`PREREQUISITES.md`](PREREQUISITES.md).

## Not in this repository

Per-install settings and state (`mwmanager.ini`, GUI settings/state, load order, caches, `outputs/`, `backups/`,
`staging/`, `disabled_plugins/`) are created on first run and ignored by git. Third-party binaries go in `tools/`
yourself: LuaJIT in `tools/luajit/` (used by the Lua check), `texconv.exe` (DirectXTex), and the BSA packer.
`mlox/` rule files are downloaded by `--init`.

`mwmanager_core_hashes.json` is shipped as immutable verification data; first-run code reads it but never
regenerates or clears it.

## GUI setup and staging

First-run setup keeps one progress window through prerequisite checks, sort/Tidy, patch generation, the
catalog, and major-master staging. Its checklist follows the saved setup checkpoints. Use **File > Resume
setup** after a pause; optional community masters can also be explicitly skipped and added later.

- **Automatic Tidy:** installer choices remain interactive, but the GUI builds and applies the plan with
  `--apply --yes` without asking for approval of individual moves or repairs. Output scrolls in the shared log.
  Existing asset-repair settings still apply. A changed/refused plan or partial apply stops deployment.
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
- **Legacy staging:** stages created before processing-complete records existed must pass Reopen staged
  options once before deployment. This prevents an interrupted extraction from being mistaken for a
  fully processed package just because its ESM exists.
- **Preflight reuse:** a successful preflight may be reused within the current session while the observed
  core/prerequisite file metadata is unchanged. Changed files, unreadable metadata, or a new session
  require preflight again. This is a performance cache, not a content-integrity guarantee.
- **Conflict decisions:** GUI patch generation uses a native picker instead of opening a console.
  Skip leaves a pair unchanged; Finish review saves choices already made and returns to patch generation.
- **Archive trust checks:** scanned ZIP/7-Zip/RAR payloads are classified before extraction; unsafe paths,
  reparse/link entries, encryption, excessive expansion, high compression ratios and blocked payloads stop install.
- **Recovery center:** interrupted regular and major-master deployment state, ready plugin stages and journal
  problems are listed in one place; only a selected, validated recovery item is changed.
- **Profiles and review tools:** the Tools menu can capture manager-state profiles, inspect deployment integrity
  and catalog record conflicts, preview staged installs, compare staged updates, and compare integrity snapshots.
  Profiles restore manager choices only; they do not implicitly redeploy or remove game files.
- **Automatic drift notice:** a low-overhead startup check hashes tracked deployed files in the background and
  reports missing, changed or unverifiable files without blocking the interface.
- **Regression corpus:** `python tools\archive_corpus.py OUTPUT_DIR` creates a deterministic set of safe and
  adversarial archives used to exercise path, payload, link and compression safeguards.

## Tests

```
python -m pytest -q -p no:cacheprovider tests
```

The tests only use temporary folders; they never touch a game install, Vortex, or MWManager's own state.
