# Mock FOMOD: options waiting on unregistered masters

Status: proposal, 2026-11-16. No code yet.

## Problem

Major masters are installed first, so their option window offers patches for mods that are not installed yet.
Examples from OAAB_Data: options for Epic Plants, Glass Glowset and Animated Containers. Today the user must
either install an option blind (its plugin then has a missing master) or skip it and remember it later. Nothing
remembers it for them.

## Detection

1. **Plugin masters (certain).** For each option folder, read every `.esp`/`.esm` header (`header_masters`).
   A master that is neither staged nor in Data Files nor in the game catalog makes the option *waiting on*
   that master, named exactly.
2. **Name hints (probable).** Options with no plugin (meshes or textures only) have no masters. Match the
   option folder name, minus its number prefix, against:
   - staged package names;
   - archive names in the downloads folder;
   - the option's readme excerpt (`_option_excerpts`).

   A match is shown as "probably for X", never as certain.
3. Options with neither signal are ordinary options and are not tracked.

## Option window

- A waiting option is shown unselected, with a badge: "Needs Epic Plants.esp (not installed)" or
  "Probably for Glass Glowset".
- The user may still select it. A selected option with a missing master gets the usual missing-master warning.

## Running list: `pending_options.json` (manager-owned, next to the other outputs)

One entry per package and option:

| Field | Meaning |
| --- | --- |
| `package` | staging folder name |
| `option` | option folder path under `Extras/<plugin>/` |
| `masters` | missing master names (empty for a name hint) |
| `hint` | matched mod name for a name hint, else null |
| `status` | `waiting`, `ready`, `installed` or `never` |

- Written when the option window closes, for every waiting option not selected.
- Re-checked after every install and on Full Check: an entry whose masters are all now present (or whose
  hinted mod is now staged) becomes `ready`.
- `installed` when that option is later activated; `never` when the user dismisses it.

## Acting on a ready entry

MWManager says: "OAAB_Data has an option for Epic Plants. Install it now?". Yes reopens the package's option
window with only that option preselected. This works at any time because the package's files stay in
`Extras/<plugin>/` (see the kept-core design). The install is journaled like any option, so `--undo --only`
still removes it.

## Limits

- An option for a mod the user never installs stays `waiting`; the Never button removes it from prompts.
- Name hints can be wrong. They only suggest, never install.
- A master supplied inside a BSA-only package counts as present only if its plugin is staged.

## Tests

- An option whose ESP needs a missing master is recorded as `waiting` with that master.
- Staging the master turns it `ready`; activating the option turns it `installed`.
- A texture-only option whose name matches a staged package is recorded as a hint.
- `never` entries are not prompted again.
