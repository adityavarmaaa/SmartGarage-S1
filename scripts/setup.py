"""Set up the shared Vehicle Intelligence and Parking & Bay Management development environment."""
import argparse
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.local.yaml"))
    parser.add_argument("--create-database", action="store_true")
    parser.add_argument("--preview-source", type=Path, help="Generate a new --config from a local recording after install")
    parser.add_argument("--cpu-torch", action="store_true", help="Install CPU PyTorch wheels on Linux or Windows")
    parser.add_argument("--color", action="append", default=[], help="Register an allowed S1 color")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required.")
    if not os.environ.get("DATABASE_URL"):
        parser.error("Export DATABASE_URL for a development PostgreSQL database first.")
    for group in (("S2_API_KEY",), ("SP_ENCRYPTION_KEYS",), ("SP_ACTIVE_ENCRYPTION_KEY",),
                  ("SP_HMAC_KEY",)):
        if not any(os.environ.get(key) for key in group):
            parser.error("Export " + " or ".join(group) + " for the local services.")
    root = Path(__file__).resolve().parents[1]
    config = args.config.resolve()
    if args.preview_source:
        source = args.preview_source.resolve()
        if not source.is_file() or config.exists():
            parser.error("Preview generation requires an existing recording and a new --config path.")
    elif not config.is_file():
        parser.error("Configuration file not found.")
    env = root / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    step = "virtual environment creation"
    try:
        if not python.exists():
            if env.exists():
                parser.error("Existing .venv has no Python executable. Repair it or use a fresh clone.")
            venv.create(env, with_pip=True)
        step = "virtual environment Python version check"
        subprocess.run([str(python), "-c", "import sys; sys.exit(sys.version_info < (3, 10))"], check=True)
        if args.cpu_torch:
            step = "CPU PyTorch dependency installation"
            subprocess.run([str(python), "-m", "pip", "install", "torch", "torchvision",
                            "--index-url", "https://download.pytorch.org/whl/cpu"], check=True)
        step = "project dependency installation"
        colors = [value for color in args.color for value in ("--color", color)]
        subprocess.run([str(python), "-m", "pip", "install", "-r", "requirements-dev.txt",
                        "-r", "parking_management/requirements.txt"], cwd=root, check=True)
        if args.preview_source:
            step = "preview configuration generation"
            subprocess.run([str(python), "scripts/make_handover_config.py", "--source", str(source),
                            "--output", str(config)], cwd=root, check=True)
        for step, command in (
            ("security configuration check", [
                "-c", "from vehicle_intelligence.security import encrypt_text, plate_hmac; "
                "encrypt_text('setup-check'); plate_hmac('setup-check')",
            ]),
            ("model and camera preflight", ["preflight.py", str(config)]),
            ("PostgreSQL initialization", [
                "scripts/init_database.py", "--config", str(config), *colors,
                *(["--create-database"] if args.create_database else []),
            ]),
        ):
            subprocess.run([str(python), *command], cwd=root, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            f"exit status {exc.returncode}" if isinstance(exc, subprocess.CalledProcessError)
            else f"{type(exc).__name__}, errno {exc.errno}"
        )
        if step in {"virtual environment creation", "CPU PyTorch dependency installation",
                    "project dependency installation"}:
            volumes = [("project", root)]
            try:
                volumes.append(("temporary", tempfile.gettempdir()))
            except OSError:
                print("No writable temporary directory found.", file=sys.stderr)
            for label, path in volumes:
                try:
                    free_mib = shutil.disk_usage(path).free // (1024 * 1024)
                except OSError:
                    continue
                print(f"Free space on {label} volume: {free_mib} MiB", file=sys.stderr)
        parser.exit(1, f"Setup failed during {step} ({detail}). See the output above.\n")
    print("Development environment and database ready. See docs/DEVELOPER_SETUP.md for startup and site resources.")


if __name__ == "__main__":
    main()
