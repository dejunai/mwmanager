# MWManager MWSE utilities

The source package is `mods/MWManager/`: one top-level MWSE mod entry with one MCM entry and nine utility tabs.
Tab order: mwquickfixes, mwhangwatch, mwinfoloop, mwsoundguard, mwperformers, mwcollide, mwfloatfix, mwstranded, mwguard.
`mwquickfixes` combines six individually toggleable quick fixes, all enabled by default. `mwinfoloop` defaults to enabled.
The setup installer copies this package into the configured game's `Data Files/MWSE/mods/MWManager/` folder,
then archives any previous standalone utility folders under `MWManager/off/legacy-standalone/`.
The source snapshots of those former folders are retained under `legacy-standalone/`.
