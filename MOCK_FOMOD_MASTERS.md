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

## Batch install plan (2026-11-16)

Major masters and plugins install in batches (15 at a time). Options that need a mod that isn't installed
must not block the batch: the mod is found, staged and deployed first, then the option goes on top of it.

1. **Record choices and what they wait on** - done. After each option window, every chosen option whose
   plugins need a master that is neither installed nor staged (`missing_masters`) is kept in
   `outputs\mwmanager_pending_options.json` as `{package: {option key: {label, needs, status: "waiting"}}}`;
   a package with none left is dropped from it. Choices themselves are already kept in each package's answer
   file. Options not chosen that need a missing master are kept too (status "not chosen"); when that mod
   is installed later, the user is asked "You just installed X. <package> has an optional component you
   didn't choose before that works with it: ... See it now?" - Yes opens the option window, No marks it
   reviewed (not asked again). Name-only matches (texture/mesh options) are not recorded yet.
2. **Split each package in the batch** - done for holding: a chosen option whose needed master isn't installed
   is held (not activated; kept as "held" in the journal and as chosen in the answer file); when that master
   arrives, held options go in without asking (only unchosen ones get the "See it now?" question). Still to do
   with step 3: putting the needed mod ahead in the batch. Originally: its core and the options whose needs are met deploy now; waiting
   options are held, and each needed mod goes into the batch ahead of the option that needs it (so the
   option's files overwrite it).
3. **Find the needed mod** - lookup done (`needed_mods.py`): every download archive's plugin names are indexed
   once in `outputs\mwmanager_archive_plugins.json` (by size and mtime; 1,532 archives in 97 s, then instant), and
   a needed plugin resolves to the archive holding it (its own mod preferred over a patch bundle); not there:
   `search_on_nexus` opens the embedded browser on a Nexus search that downloads into the archive folder.
   Wired into the plugin batch (`plan_needed_archives` in mwmanager_gui): before a package joins the batch, the
   archives its held options need are inserted right after it, followed by the package again; they stage first
   (staging one makes the option step install the held option into the package), and the package joins the
   batch after them, so its option files overwrite theirs. A needed major master is left to the Major masters
   install (said in the status); a need not in the downloads opens a Nexus search and the batch goes on.
   Originally: first the archive downloads folder (archive listings already exist in the master
   check); for a user who doesn't have it, the embedded Nexus browser opens on a search for it, and the
   download is tidied and staged like any other mod. Not found: the batch goes on; the option stays waiting,
   with a Never button.
4. **Finish held options** - once the needed mod is deployed, the option step runs again for that package
   with the recorded choices preselected (the existing "newly relevant targets" reopen), then deploys.
5. **Clear status in the GUI** - partly: each step is a plain line in the job window ("OAAB_Data: option
   'Epic Plants' needs Epic Plants.esp; installing Epic Plants-1-0.zip first", "... isn't in your downloads;
   opening a Nexus search", "installing option 'Epic Plants' now that what it needs is installed"). A separate
   per-package list is not built yet. Originally: one list for the batch, one plain line per package ("OAAB_Data - core
   installed; 3 options waiting", "Epic Plants - needed by OAAB_Data's 'Epic Plants' option; found in your
   downloads; staging"), and one current step at the top.
