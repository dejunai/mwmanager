"""Plugins of the mods "Install mods your options need" deploys (option-choice mods) go into the active load
order as soon as they are deployed, so the checks that follow see them and Generate Patches' save never turns
them off (before, the save moved them to MWManager\\disabled_plugins and --full's first step moved them back:
CR-25 stress run, error K). Left out: a plugin not at the Data Files root, an MGE XE grass plugin (it belongs in
Data Files\\Grass, never in the content list), one listed as disabled (the user turned it off, or a deployment
turned it off for a conflict, such as Siege at Firemoth.esp with Tamriel Rebuilt) and one already in the order.
Nothing here moves a game file: the load-order file is MWManager's own state, and Morrowind.ini follows at the
next sort (journaled there). The change is recorded as a journal run so rollback history explains it."""
from __future__ import annotations

from pathlib import Path


def _is_grass(path: Path) -> bool:
    if path.suffix.casefold() != ".esp":
        return False
    try:
        from vortex_tidy import is_grass_plugin
    except ImportError:
        return False
    return bool(is_grass_plugin(str(path)))


def _read_lines(path: Path) -> list[str]:
    try:
        return [line.strip() for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith(";")]
    except OSError:
        return []


def read_disabled_names(disabled_record: Path) -> set[str]:
    return {line.partition("\t")[0].strip().casefold() for line in _read_lines(disabled_record)}


def plugins_to_enable(data_dir, names, order, disabled, merged=()) -> tuple[list[str], dict[str, str]]:
    """(to enable, {name: why left out}) for the deployed plugin names, in the given order."""
    data_dir = Path(data_dir)
    active = {n.casefold() for n in order}
    disabled = {n.casefold() for n in disabled}
    merged = {n.casefold() for n in merged}
    on_disk = {}
    try:
        on_disk = {p.name.casefold(): p.name for p in data_dir.iterdir()
                   if p.is_file() and p.suffix.casefold() in (".esp", ".esm")}
    except OSError:
        pass
    enable, skipped, seen = [], {}, set()
    for raw in names:
        name = Path(str(raw)).name
        key = name.casefold()
        if key in seen or Path(name).suffix.casefold() not in (".esp", ".esm"):
            continue
        seen.add(key)
        actual = on_disk.get(key)
        if actual is None:
            skipped[name] = "not at the Data Files root"
        elif _is_grass(data_dir / actual):
            skipped[actual] = "MGE XE grass plugin (Data Files\\Grass, not the load order)"
        elif key in disabled:
            skipped[actual] = "listed as disabled (turned off by you or for a conflict)"
        elif key in merged:
            skipped[actual] = "already merged"
        elif key in active:
            skipped[actual] = "already active"
        else:
            enable.append(actual)
    return enable, skipped


def enable_option_plugins(data_dir, names, sort_file, disabled_record, journal_dir=None,
                          merged=()) -> tuple[list[str], dict[str, str]]:
    """Add the deployed option-choice plugins to the load-order file (masters before plugins, after what is
    there). Returns (enabled, skipped)."""
    sort_file = Path(sort_file)
    order = _read_lines(sort_file)
    enable, skipped = plugins_to_enable(data_dir, names, order, read_disabled_names(Path(disabled_record)),
                                        merged)
    if not enable:
        return enable, skipped
    masters = [n for n in enable if n.casefold().endswith(".esm")]
    plugins = [n for n in enable if not n.casefold().endswith(".esm")]
    first_esp = next((i for i, n in enumerate(order) if n.casefold().endswith(".esp")), len(order))
    new_order = order[:first_esp] + masters + order[first_esp:] + plugins
    run = ledger = None
    if journal_dir is not None:
        import mwmanager_journal
        ledger = mwmanager_journal.Journal(Path(journal_dir))
        if ledger.has_baseline():
            run = ledger.start_run("mwmanager_gui.py", {"operation": "enable option-choice plugins",
                                                         "plugins": enable})
    try:
        lines = ["; MWManager active load order. Core masters are fixed at the top.", "", *new_order, ""]
        temporary = sort_file.with_name(sort_file.name + ".tmp")
        temporary.write_bytes("\n".join(lines).encode("utf-8"))
        temporary.replace(sort_file)
    finally:
        if run:
            ledger.finish_run(run)
    return enable, skipped