#!/usr/bin/env python3
"""Build Sheldinner Practice into a single self-contained executable.

Usage (run from the repository root):
    python packaging/build.py            # build the one-file binary
    python packaging/build.py --no-clean # reuse the PyInstaller cache

Requires PyInstaller (pip install -r requirements.txt).
The resulting self-contained binary is written to dist/SheldinnerPractice.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = Path(__file__).resolve().parent / "sheldinner.spec"


def main() -> None:
    if not SPEC.is_file():
        print(f"ERROR: spec file not found: {SPEC}", file=sys.stderr)
        sys.exit(1)
    clean = "--no-clean" not in sys.argv
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    cmd += ["--distpath", str(REPO / "dist"),
            "--workpath", str(REPO / "build"),
            str(SPEC)]
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=REPO, check=True)
    out = REPO / "dist" / ("SheldinnerPractice.exe" if sys.platform == "win32"
                           else "SheldinnerPractice")
    print(f"\nDone. Self-contained binary at: {out}")


if __name__ == "__main__":
    main()
