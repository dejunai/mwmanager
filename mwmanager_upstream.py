"""MWManager on top of the Steam tools (github.com/dejunai/morrowind-conflict-tools), used unchanged.

The upstream files in this folder (mwdialog_check.py and its mwdialog_* modules, mwdialog_assets.py,
vortex_tidy.py, mwdialog_luafix.py, mwdialog_paths.py...) are byte-identical to tools\\steam-upstream\\ (the
sync procedure in TOOL_SYNC.md refreshes both). compose() imports them and lays MWManager's behaviour over them:

  mwmanager_state.py      state and records in the MWManager folder, backup routing (Off\\, MWSE\\backups)
  mwmanager_overrides.py  the scanner functions MWManager runs differently
  mwmanager_init.py       --init, catalog, MWSE / MCP / MGE XE checks, mlox rules, core masters
  mwmanager_assets_ext.py the asset checker's MWManager settings (on top of upstream mwdialog_assets)
  mwmanager_tidy_ext.py   MWManager's Tidy rules and installer steps (on top of upstream vortex_tidy)
  mwmanager_install.py    the Tidy command line: options -> plan -> --apply

Each scanner layer runs inside the mwdialog_check namespace, the way a subclass body extends its base: a name it
defines replaces upstream's there, and is then copied into every upstream module that had imported the original,
so upstream code calls MWManager's version. Upstream lookups of the scanner by name
(sys.modules["mwdialog_check"].TOP_ARGS, vortex_tidy's sibling loader, mwdialog_assets' import) find the
composed module.
"""
import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER_LAYERS = ("mwmanager_state.py", "mwmanager_overrides.py", "mwmanager_init.py")
ASSET_LAYER = "mwmanager_assets_ext.py"
TIDY_LAYER = "mwmanager_tidy_ext.py"
_SCALARS = (str, bytes, int, float, bool, tuple, frozenset, type(None))
_composed = None
_tidy = None


def upstream_modules():
    """The loaded upstream modules a layer's names are copied into."""
    return [m for name, m in list(sys.modules.items())
            if m is not None and (name.startswith("mwdialog_") or name in ("vortex_tidy",))
            and os.path.dirname(os.path.abspath(getattr(m, "__file__", "") or "")) == HERE]


LAYER_REPORT = {}                 # layer file -> {"replaced": [...], "added": [...]}


def _same(a, b):
    return a is b or (isinstance(a, _SCALARS) and type(a) is type(b) and a == b)


def propagate(home, before, names):
    """Copy home's new value of each name into the upstream modules that held the value it replaced (for a name
    home did not have before: the value the upstream modules have, all of them the same one)."""
    mods = [m for m in upstream_modules() if m is not home]
    for name in names:
        new = vars(home)[name]
        if name in before:
            old = before[name]
        else:
            # a module whose name means something else (another type: vortex_tidy's DUMMY_SOURCES is a file name,
            # mwdialog_media's the stand-in table) is not a copy and must not stop the others from being replaced
            held = [vars(m)[name] for m in mods if name in vars(m) and type(vars(m)[name]) is type(new)]
            if not held or any(not _same(h, held[0]) for h in held):
                continue
            old = held[0]
        for mod in mods:
            d = vars(mod)
            if name in d and _same(d[name], old):
                d[name] = new


def run_layer(module, path):
    """Execute a layer file inside `module`'s namespace and spread what it changed."""
    ns = vars(module)
    with open(path, encoding="utf-8") as f:
        code = compile(f.read(), path, "exec")
    before = dict(ns)
    exec(code, ns)
    changed = [k for k, v in ns.items() if not k.startswith("__") and (k not in before or before[k] is not v)]
    propagate(module, before, changed)
    # which upstream names the layer replaced and which it added: an override whose upstream name was renamed
    # or removed in a sync shows up as "added" (tests/test_layer_report.py compares this with a baseline)
    LAYER_REPORT[os.path.basename(path)] = {"replaced": sorted(k for k in changed if k in before),
                                            "added": sorted(k for k in changed if k not in before)}
    return changed


def _set_module_names(per_module):
    """Set names in other upstream modules (importing them); returns what they were."""
    import importlib
    old = {}
    for modname, names in per_module.items():
        mod = importlib.import_module(modname)
        old[modname] = {k: getattr(mod, k) for k in names}
        for k, v in names.items():
            setattr(mod, k, v)
    return old


def compose():
    """Import the upstream scanner and lay MWManager over it, once per process. Returns the scanner module."""
    global _composed
    if _composed is not None:
        return _composed
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    for modname in list(sys.modules):
        mod = sys.modules.get(modname)
        if mod is not None and getattr(mod, "__file__", "") and (modname.startswith("mwdialog_") or modname in ("vortex_tidy",)):
            if os.path.dirname(os.path.abspath(mod.__file__)) != HERE:
                del sys.modules[modname]
    import mwdialog_check as scanner
    if os.path.dirname(os.path.abspath(scanner.__file__)) != HERE:
        raise ImportError(f"mwdialog_check came from {scanner.__file__}, not the MWManager folder")
    import mwdialog_assets                         # loaded now so its names get the layers too
    for layer in SCANNER_LAYERS:
        run_layer(scanner, os.path.join(HERE, layer))
    _set_module_names(scanner.module_state_names(scanner.MANAGER_DIR))
    scanner.hook_upstream_modules()
    run_layer(mwdialog_assets, os.path.join(HERE, ASSET_LAYER))
    scanner.MWMANAGER_COMPOSED = True
    _composed = scanner
    return scanner


def compose_tidy():
    """compose(), then upstream vortex_tidy with MWManager's Tidy layer over it, once per process."""
    global _tidy
    if _tidy is not None:
        return _tidy
    scanner = compose()
    mod = sys.modules.get("vortex_tidy")
    if mod is not None and getattr(mod, "__file__", ""):
        if os.path.dirname(os.path.abspath(mod.__file__)) != HERE:
            del sys.modules["vortex_tidy"]
    import vortex_tidy as tidy
    if os.path.dirname(os.path.abspath(tidy.__file__)) != HERE:
        raise ImportError(f"vortex_tidy came from {tidy.__file__}, not the MWManager folder")
    vars(tidy).update(scanner.tidy_state_names(scanner.MANAGER_DIR))
    run_layer(tidy, os.path.join(HERE, TIDY_LAYER))
    tidy.MWMANAGER_COMPOSED = True
    _tidy = tidy
    return tidy


@contextlib.contextmanager
def use_manager_dir(manager_dir):
    """Point every MWManager folder and record path at `manager_dir` for the duration (tests use a temporary
    folder this way); the upstream modules get the same paths."""
    scanner = compose()
    ns = vars(scanner)
    names = scanner.state_names(str(manager_dir))
    before = {k: ns[k] for k in names}
    ns.update(names)
    propagate(scanner, before, list(names))
    old_modules = _set_module_names(scanner.module_state_names(str(manager_dir)))
    tidy_before = None
    if _tidy is not None:
        tidy_names = scanner.tidy_state_names(str(manager_dir))
        tidy_before = {k: getattr(_tidy, k) for k in tidy_names}
        vars(_tidy).update(tidy_names, MANAGER_DIR=str(manager_dir))
    try:
        yield scanner
    finally:
        if tidy_before is not None:
            vars(_tidy).update(tidy_before, MANAGER_DIR=scanner.MANAGER_DIR)
        _set_module_names(old_modules)
        now = {k: ns[k] for k in names}
        ns.update(before)
        propagate(scanner, now, list(names))


def run_tidy_cli(argv=None):
    """python mwmanager_install.py ...: MWManager's Tidy (options -> plan -> --apply)."""
    return compose_tidy().mwmanager_main(argv)


def run_cli(argv=None):
    """python mwmanager.py ...: the scanner's main(), then --play's launch."""
    scanner = compose()
    return scanner.finish_requested_play(scanner.main(argv))
