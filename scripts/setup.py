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
    parser.add_argument("--mode", choices=("preview", "full"), default="full",
                        help="Preview skips PostgreSQL initialization; full requires it")
    parser.add_argument("--check", action="store_true", help="Check an existing environment without changes")
    parser.add_argument("--install", action="store_true", help="Install project dependencies")
    parser.add_argument("--smoke", action="store_true", help="Run the controlled-event PostgreSQL smoke test")
    parser.add_argument("--create-database", action="store_true")
    parser.add_argument("--preview-source", type=Path, help="Generate a new --config from a local recording after install")
    parser.add_argument("--cpu-torch", action="store_true", help="Install CPU PyTorch wheels on Linux or Windows")
    parser.add_argument("--color", action="append", default=[], help="Register an allowed S1 color")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required.")
    if args.check and (args.install or args.cpu_torch or args.create_database
                       or args.preview_source or args.color or args.smoke):
        parser.error("--check cannot be combined with install, creation or smoke options.")
    if args.mode == "preview" and (args.create_database or args.color or args.smoke):
        parser.error("--create-database, --color and --smoke require --mode full.")
    if args.mode == "full":
        for name in ("DATABASE_URL", "S2_API_KEY", "SP_ENCRYPTION_KEYS",
                     "SP_ACTIVE_ENCRYPTION_KEY", "SP_HMAC_KEY"):
            if not os.environ.get(name):
                parser.error(f"Export {name} for full setup, or use --mode preview for local video.")
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
    install = args.install or args.cpu_torch
    print(f"Setup mode={args.mode}, platform={sys.platform}, Python={sys.version.split()[0]}")
    step = "virtual environment creation"
    try:
        if not python.exists():
            if not install:
                parser.error("No project virtual environment. Rerun with --install or --cpu-torch.")
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
        if install:
            subprocess.run([str(python), "-m", "pip", "install", "-r", "requirements-dev.txt",
                            "-r", "parking_management/requirements.txt"], cwd=root, check=True)
        step = "dependency verification"
        subprocess.run([str(python), "-m", "pip", "check"], cwd=root, check=True)
        step = "required imports"
        modules = "import torch, cv2, ultralytics, yaml, uvicorn, httpx, fastapi"
        if args.mode == "full":
            modules += ", psycopg, sqlalchemy, pydantic_settings"
        subprocess.run([str(python), "-c", modules], cwd=root, check=True)
        if args.preview_source:
            step = "preview configuration generation"
            subprocess.run([str(python), "scripts/make_handover_config.py", "--source", str(source),
                            "--output", str(config)], cwd=root, check=True)
        if args.mode == "full":
            step = "security configuration check"
            subprocess.run([str(python), "-c",
                            "from vehicle_intelligence.security import encrypt_text, plate_hmac; "
                            "encrypt_text('setup-check'); plate_hmac('setup-check')"],
                           cwd=root, check=True)
        step = "model and camera preflight"
        subprocess.run([str(python), "preflight.py", str(config)], cwd=root, check=True)
        if args.mode == "full":
            step = "PostgreSQL initialization"
            options = ["--check"] if args.check else [
                "--config", str(config), *colors,
                *(["--create-database"] if args.create_database else []),
            ]
            subprocess.run([str(python), "scripts/init_database.py", *options], cwd=root, check=True)
            if args.smoke:
                step = "controlled-event PostgreSQL smoke test"
                subprocess.run([str(python), "scripts/smoke_database.py"], cwd=root, check=True)
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
    if args.mode == "preview":
        print("Preview setup complete. PostgreSQL was not initialized. "
              "Run vehicle_intelligence.main with --no-publisher.")
        return
    print("Development environment and database ready. See docs/DEVELOPER_SETUP.md for startup and site resources.")


if __name__ == "__main__":
    main()
