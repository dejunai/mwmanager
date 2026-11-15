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

## Tests

```
python -m pytest -q -p no:cacheprovider tests
```

The tests only use temporary folders; they never touch a game install, Vortex, or MWManager's own state.
