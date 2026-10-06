"""Open the local credential form using a Python installation with tkinter."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from school_mcp.mail.setup import run

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="")
    run(parser.parse_args().address)
