#!/usr/bin/env python3
"""mwmanager_install.py - MWManager's Tidy for its per-mod staging folders, on top of upstream vortex_tidy.py.

Steps (each a separate run, so the plan can be reviewed - or the run cancelled - in between):
  1. python mwmanager_install.py --staging DIR --only MOD --options    choose the mod's BAIN/FOMOD options
  2. python mwmanager_install.py --staging DIR --only MOD              plan: writes the plan file, changes nothing
  3. python mwmanager_install.py --staging DIR --only MOD --apply      enacts the reviewed plan; exit 2 = refused
                                                                       (no plan, or it changed: review it again)
Also: --undo (put back the last --apply), --data-files PATH (organize a Data Files folder's documents; --init).
`import mwmanager_install` gives the composed vortex_tidy module (see mwmanager_upstream.compose_tidy).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mwmanager_upstream  # noqa: E402

if __name__ == "__main__":
    sys.exit(mwmanager_upstream.run_tidy_cli())
sys.modules[__name__] = mwmanager_upstream.compose_tidy()
