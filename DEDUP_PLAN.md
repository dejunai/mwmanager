# Removing the near-duplicate functions between the Steam tools and MWManager

Status: plan, 2026-11-16. Nothing changed yet.

## What is duplicated

MWManager replaces 51 functions of the Steam tools (its layers: `mwmanager_overrides.py`, `mwmanager_state.py`,
`mwmanager_tidy_ext.py`, `mwmanager_assets_ext.py`; the list is `tests/layer_overrides.json`). 27 of them are at least
half the same code as the Steam original, about 3,860 lines; these nine are near-total copies:

| Function (`mwdialog_check.py`) | Lines | Same as Steam | What MWManager changes |
| --- | --- | --- | --- |
| `fix_cell_winners` | 683 | 99% | report file name and folder, plugin header author/description |
| `_main` | 668 | 85% | its own steps, folders and messages (see below) |
| `run_fix` | 466 | 94% | file names (`mwmanager_dummy`), Off folder wording, long-path plan held |
| `run_cell_conflicts` | 419 | 94% | ghost statics written through MWManager's journal (`mge_ghosts`), file names |
| `compute_order` | 287 | 99% | `XE Sky Variations.esp` added to the generated tail |
| `run_full` | 190 | 85% | MWManager's own step order |
| `run_sort` | 174 | 96% | rules file in MWManager's folder; mlox rules fetched/managed (`ensure_mlox_rules`) |
| `fix_doubled` | 84 | 96% | file names |
| `run_repair_sounds` | 75 | 89% | file names / folders |

**The copies already drift.** MWManager's `run_sort` still writes the rules file with a plain `open(..., "w")`
(Steam's has `atomic_open` since today), and its `fix_cell_winners` lacks the FIPS hashing fix. Every Steam fix to these
functions has to be made twice, and a missed one is silent - today's "keep revived deletions" rule had to be.

## The `--gui` flag idea

A `--gui` switch on the Steam tools, with the function doing "what the GUI wants" when it is set, would remove the
copies, but at a cost:

- MWManager-only behaviour (GOG folders, its journal and rollback ledger, mlox fetching, the XE Sky plugin) would move
  into the Steam tools, which `STEAM_UPSTREAM.md` keeps out of them; every Steam user would carry it.
- Each function would grow `if gui:` branches through its middle: two behaviours in one place, each needing its own
  tests, and harder to read than either copy is now.
- MWManager mostly runs these functions inside its own process (the layers), not through the command line, so a flag
  would have to be threaded through every call anyway.

## Proposed instead: named hooks with Steam defaults

Most differences are **values** (a file name, a folder, a header string), and a few are **one step** done differently.
Both can be named once in the Steam code, with the Steam behaviour as the default, and MWManager replaces only the hook
- the layer mechanism already does exactly this for constants and small functions (`propagate` in
`mwmanager_upstream.py`), and the drift test (`tests/test_layer_report.py`) catches a hook that disappears.

1. **Values** - module-level names the Steam code reads instead of literals:
   `CELL_FIXES_REPORT = "mwdialog_cell_fixes.txt"`, `PATCH_AUTHOR = "mwdialog_check"`, `DUMMY_PREFIX = "mwdialog_dummy"`,
   `ORDER_TAIL_EXTRA = ()` (MWManager: `(XE_SKY_PLUGIN,)`), and every output path through the existing
   `output_file()` / `manager_file()` helpers (MWManager already redefines those).
2. **Steps** - one small function per behaviour difference, Steam default first:
   - `rules_sources(game_dir, args)` - the mlox files `run_sort` reads (MWManager: `ensure_mlox_rules` first);
   - `write_ghost_statics(game_dir, data_dir, plugins)` - Steam: `mwdialog_mgeghosts.write`; MWManager: `mge_ghosts`
     with its journal;
   - `off_label(kind)` - the Off folder wording in `run_fix`'s summary.
3. **Delete the copies** - once a function's differences are all hooks, MWManager's copy goes and it overrides only the
   hooks. `_main` and `run_full` stay MWManager's own: they are MWManager's workflow, not a copy to share.

## Order of work

One function per commit, the smallest differences first, each checked by the existing suites (Steam, vendored upstream,
MWManager) and by `test_layer_report.py` (the replaced-name baseline shrinks as copies go):

1. `compute_order` (1 hook: `ORDER_TAIL_EXTRA`).
2. `fix_cell_winners` (values: report name, patch author/description).
3. `fix_doubled`, `run_repair_sounds` (values).
4. `run_sort` (values + `rules_sources`) - also fixes the drifted rules-file write.
5. `run_cell_conflicts` (values + `write_ghost_statics`).
6. `run_fix` (values + `off_label`; the long-path plan holding is checked separately).

Each step: add the hook in the Steam source with today's Steam behaviour, sync, replace MWManager's copy with the hook
override, confirm a default `mwdialog_check` run on the Steam install gives the same report as before (the Stage 3
baseline method) and MWManager's tests pass.

Expected result: about 2,300 fewer duplicated lines (the seven shared functions above), Steam fixes reaching MWManager
automatically, and nothing MWManager-specific in the Steam tools beyond hook names.

## Handoff: what the implementing agent needs

**Repositories and copies**

- Steam tools (the source of truth): `G:\Program Files\Steam\steamapps\common\Morrowind` - this is the live Steam game
  folder, not a git checkout. Its repo is `github.com/dejunai/morrowind-conflict-tools` (branch `master`): clone it to a
  scratch folder, copy the changed files in, commit, push.
- MWManager: `G:\projects\MWManager` (git, branch `main`, `github.com/dejunai/mwmanager`). It vendors the Steam Python
  files twice, byte-identical: in its root (`mwdialog_*.py`, `vortex_tidy.py`) and in `tools\steam-upstream\`. The
  MWManager-side overrides live in `mwmanager_overrides.py` (the `mwdialog_check` layer), `mwmanager_state.py`,
  `mwmanager_tidy_ext.py`, `mwmanager_assets_ext.py`; `mwmanager_upstream.py` composes them over the Steam modules.

**Per step**

1. Add the hook in the Steam file with today's Steam behaviour as the default (a module-level name, or a small
   function). Nothing else changes in Steam behaviour.
2. Steam tests: `python -X utf8 -m pytest tests -q -p no:cacheprovider` in the Steam folder.
3. Copy the changed Steam file unchanged to MWManager's root and `tools\steam-upstream\`, then refresh its SHA-256 (and
   `Bytes`) in `utility_source_manifest.json` (source/snapshot/working) and `tools\steam-upstream\COPY_MANIFEST.json`.
   `tests/test_game_writes.py` fails if any copy or hash differs.
4. In the MWManager layer: delete the copied function, set/override only the hook (values next to the layer's other
   names; a replacement step function with the same name as the hook).
5. Update `tests/layer_overrides.json`: the deleted copy leaves the "replaced" list, the hook joins it. Rerun with
   `MWMANAGER_UPDATE_LAYER_BASELINE=1 python -X utf8 -m pytest tests/test_layer_report.py` only after checking the
   diff is exactly that.
6. MWManager tests: `python -X utf8 -m pytest tests -q -p no:cacheprovider` (about 90 s) and
   `python -X utf8 -m pytest tools/steam-upstream/tests -q -p no:cacheprovider`.
7. Behaviour check on the Steam install (read-only): `python -X utf8 mwdialog_check.py "Data Files" --dry-run` must
   give the same report as before the step (save one before you start).
8. One commit per step in each repo, ending with the co-author line; push only when the user asks.

**Rules and pitfalls**

- Pushing from this machine: `GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never git push https://dejunai@github.com/dejunai/<repo> <branch>`
  (two stored credential accounts make a plain push hang).
- `mwespreplacements` (in the Steam MWSE mods) is the user's personal mod: never copy it into either repo.
- No fixed Morrowind.exe addresses in Lua.
- Edit Python with the Edit tool or a Python script file; bash heredocs mangle backslashes (`\`, `\n`) in replacement
  text and have broken edits several times.
- MWManager-only behaviour stays in MWManager (`STEAM_UPSTREAM.md`, "MWManager-only behavior"): the hooks carry
  Steam defaults only.
- The game can't be run from a session: say what was not tested in game.
