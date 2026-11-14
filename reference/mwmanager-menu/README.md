# mwmanagermenu: one Mod Config Menu entry for the mw* suite

MWManager's own MWSE mod (not a copy of anything in the Steam install). `--init` installs
`mods/mwmanagermenu/` to `Data Files\MWSE\mods\mwmanagermenu\` and `metadata/mwmanagermenu-metadata.toml` to
`Data Files\`, together with the suite in `reference/mwse-lua-guards/`.

Each suite mod registers its own settings page, exactly as it does on its own. `mwmanagermenu` loads before them
(load priority 900000, below mwguard) and replaces `mwse.registerModConfig` with a function that keeps a
registration back when it comes from a suite mod's folder. After every mod has registered, it registers one
entry, "MWManager". That entry shows a row of tabs, one per suite mod, and under them the open mod's own page,
built by that mod's own code. Switching tabs or closing the menu calls the open page's own close function, which
is where each mod saves its settings.

- The suite's files are not changed: they stay byte-identical to the Steam install.
- A mod that is not in the suite, and a suite mod this file does not list, keeps its own entry.
- Without `mwmanagermenu` every suite mod has its own entry again.
- The tab list is `SUITE` in `main.lua`; `tests/test_unified_menu.py` checks it against the suite folder and
  runs the menu outside the game under LuaJIT with a stand-in for MWSE.
