#!/usr/bin/env python3
"""mwmanager.py - MWManager's scanner and fixer for a Morrowind "Data Files" folder.

The work is done by the upstream scanner (mwdialog_check.py and its modules, unchanged) with MWManager's layers
on top; see mwmanager_upstream.py. `import mwmanager` gives that composed scanner module.

Usage examples (Windows):
  python mwmanager.py "C:\\Games\\Morrowind\\Data Files"
  python mwmanager.py "C:\\Games\\Morrowind\\Data Files" --only MyMod.esp
  python mwmanager.py "...\\Data Files" --init        (first run: catalog, MWSE, core checks)

Load order, in priority:
  --plugins A.esm B.esp ...   exactly these files, in this order
  (automatic)                 mwmanager_sort_order.txt in the MWManager folder (made from Morrowind.ini's
                              [Game Files] at --init; --play writes it back)
  --ini PATH                  another Morrowind.ini
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mwmanager_upstream  # noqa: E402

if __name__ == "__main__":
    sys.exit(mwmanager_upstream.run_cli())
sys.modules[__name__] = mwmanager_upstream.compose()
