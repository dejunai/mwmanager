"""Builds tests/fixtures/grass_rem_style.esp: a tiny MGE XE groundcover plugin shaped like Remiros' Groundcover for
TOTSP (Rem-TOTSP.esp): no masters, only STAT records whose ids are texture-style names (GRS_Tx_...) with models
under grass\\, and exterior CELL records that place them. Synthetic; no data from the real plugin."""
import struct
from pathlib import Path

def sub(n, d):
    return n + struct.pack("<I", len(d)) + d

def rec(tag, body):
    return tag + struct.pack("<III", len(body), 0, 0) + body

out = rec(b"TES3", sub(b"HEDR", struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 5)))
stats = [(b"GRS_Tx_BM_grass_01.dds0", b"grass\\rem_sol_grass_01b.nif"),
         (b"GRS_Tx_BM_grass_snow_02.dds1", b"grass\\rem_sol_grass_02.nif")]
for name, model in stats:
    out += rec(b"STAT", sub(b"NAME", name + b"\0") + sub(b"MODL", model + b"\0"))
for x, y in ((-12, 23), (-13, 24)):
    body = sub(b"NAME", b"\0") + sub(b"DATA", struct.pack("<Iii", 2, x, y))
    for i, (name, _model) in enumerate(stats, 1):
        body += (sub(b"FRMR", struct.pack("<I", i)) + sub(b"NAME", name + b"\0")
                 + sub(b"DATA", struct.pack("<6f", x * 8192 + 100.0 * i, y * 8192 + 50.0, 0.0, 0.0, 0.0, 0.0)))
    out += rec(b"CELL", body)
target = Path(__file__).resolve().parent / "grass_rem_style.esp"
target.write_bytes(out)
print(target, len(out))
