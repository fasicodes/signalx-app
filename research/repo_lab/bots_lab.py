"""Runs lab.py's protocol on the bots' signal logic (strategies_bots.py + itb.py).
Usage: python bots_lab.py masks|run MARKET TF   |   python bots_lab.py report"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lab  # noqa: E402
import strategies_bots  # noqa: E402

lab.S = strategies_bots
lab.MK = f"{lab.D}/masks_bots"
lab.RES = f"{lab.D}/results_bots"

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "masks":
        lab.masks(sys.argv[2], sys.argv[3])
    elif cmd == "run":
        lab.run(sys.argv[2], sys.argv[3])
    elif cmd == "report":
        lab.report_all()
