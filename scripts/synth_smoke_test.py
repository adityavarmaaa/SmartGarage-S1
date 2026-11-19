"""Launcher. The script itself stays in tools/ so path-based imports keep working."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "tools" / "synth_smoke_test.py"
    runpy.run_path(str(target), run_name="__main__")
