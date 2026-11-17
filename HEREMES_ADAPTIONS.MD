# HEREMES_ADAPTIONS

Working notes for Hermes when reviewing or changing Dwight's Morrowind tools. Keep the filename as requested.

## Project map and working rules

- **Core tools / live install:** `G:\Program Files\Steam\steamapps\common\Morrowind`. This is also the active Steam game folder, not a disposable clone. Its Python utilities include plugin checking/repair, load-order handling, Vortex staging tidy, Lua fixes, and asset checks.
- **End-user GUI:** `G:\projects\MWManager`. It contains the Windows/Tkinter manager and its extension modules, tests, setup workflow, and a vendored copy of the core tools under `tools\steam-upstream`.
- MWManager's README says the root `mwdialog_*.py` and `vortex_tidy.py` copies should remain byte-identical to upstream. Use its sync/manifest tests when changing upstream tooling; do not edit a mirrored copy alone.
- Preserve existing user changes. At the time this note was written, MWManager was on `main` ahead of `origin/main` by 3 commits with modified files across core modules and `tools/steam-upstream`; inspect `git status` and diffs before any work. Do not reset, clean, stage, or overwrite these changes without explicit direction.
- Treat the Morrowind directory as live user data. Prefer tests using temporary fixtures; never run apply/fix/cleanup against the active game or Vortex staging unless Dwight explicitly asks and has reviewed the plan.

## Review observations

### Core tools

1. **Dry-run can create persistent load-order state (confirmed, medium).** `mwdialog_loadorder.py:74–92` creates `mwdialog_load_order.txt` from `Morrowind.ini` when the file is absent. `mwdialog_check.py:2216–18` calls this path without a dry-run guard. The generated file becomes authoritative on later runs, so subsequent edits to the INI may be ignored. **Suggestion:** avoid creating the file in dry-run; make initialization explicit or defer it to a mutating run. Test with a temporary game-folder copy and a missing load-order file.

2. **Vortex move can outlive its undo record (confirmed failure window, medium).** In `vortex_tidy.py:589–92`, `os.rename(src, dst)` precedes the journal append and flush. If journal I/O fails after the rename, the file has moved but `--undo` may have no operation to reverse. **Suggestion:** use a write-ahead intent/commit record or a transaction scheme that can recover from a failed journal write. Test by injecting an I/O failure after the rename in a temporary staging folder.

3. **Malformed but valid conflict-answer JSON can crash reporting (confirmed, medium).** `mwdialog_state.py:221–26` returns `answers` without validating it or its entries; `mwdialog_check.py:4729–47` later calls `.get()` on those values. **Suggestion:** validate the mapping and each entry on load, warn, and ignore malformed entries instead of aborting the conflict report.

### MWManager GUI and first-run flow

1. **Documentation does not match the first-run path.** `PREREQUISITES.md:16–18` says approving detected paths opens Options and preflight starts after saving there. In `mwmanager_gui.py:1441–57`, approval saves the paths and starts preflight directly. **Suggestion:** align the documentation with the implementation or restore the promised review step.

2. **First-run live changes lack a separate preview/decision point (UX recommendation, medium).** `mwmanager_gui.py:1162–77` enables installed shipped plugins, saves the load order, then invokes Tidy with `--apply --yes` before the initial full check. The README discloses automatic Tidy, but the in-app sequence does not pause for a review of proposed live-file changes. This is especially consequential when a user chooses to keep existing mods. **Suggestion:** show a concise change summary and an explicit apply/defer choice before mutating the game folder; retain a clear rollback path.

3. **Load-order toggles are mouse-only (confirmed, medium accessibility issue).** The list renders enabled state as checkbox glyphs and toggles only through a `<Button-1>` handler (`mwmanager_gui.py:2722–47`). **Suggestion:** support Space/Enter and expose a meaningful accessible checked state or an equivalent keyboard control.

4. **Options validates a weaker game-path condition than first-run detection.** The detected-path dialog requires both `Morrowind.exe` and `Data Files` (`mwmanager_gui.py:1416–21`), while Options checks only for `Data Files` (`4648–50`). A bad path can be saved and setup then fails later. **Suggestion:** reuse the same install validator in both flows.

5. **Blank Archive Downloads path creates a second, easy-to-miss prompt.** The first dialog allows it (`mwmanager_gui.py:1433–45`); setup then opens another folder picker (`1046–79`). Cancelling leaves setup waiting and the user must find **File > Resume setup**. **Suggestion:** validate in the first dialog or leave a visible retry action in the waiting state.

## Verification notes from the review

- MWManager: `python -m pytest -q -p no:cacheprovider tests` — **340 passed, 4 skipped**.
- Core tools: the full run reported **368 passed, 20 skipped, 1 failed**. The lone failure was `tests/test_logshim.py::test_v1_shim_is_upgraded_in_place`; its fixture launches `git show`, and the Hermes-hosted Windows environment denied launching that Git executable with `WinError 5` before the test assertion. Excluding that fixture: **368 passed, 20 skipped, 1 deselected**. Treat this as an environment/test-fixture limitation, not evidence that the core suite is fully green.
- The GUI was reviewed from source and tests; it was not launched against the live game during that review.

## Suggested order of work

1. Fix dry-run side effects and journal write ordering in the core tools; add isolated regression tests.
2. Harden conflict-answer loading against malformed JSON shapes.
3. Unify game-install path validation and add keyboard operation for load-order toggles.
4. Add an in-app first-run change preview and align the first-run documentation.
5. Make the Archive Downloads retry path obvious and directly actionable.
