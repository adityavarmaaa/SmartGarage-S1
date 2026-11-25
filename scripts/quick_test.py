"""Check preview, API documentation, disposable PostgreSQL integration and regression tests."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from run import run_command, start_api


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--source", type=Path, help="Local sample video for the preview fixture")
    sources.add_argument("--config", type=Path, help="Existing YAML with its own video and calibration")
    parser.add_argument("--output-root", type=Path, default=Path("runs"))
    parser.add_argument("--preview-only", action="store_true", help="Explicitly skip the PostgreSQL smoke test")
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        parser.error("Run with the project virtual environment after installing dependencies.")

    directory = args.output_root.resolve() / uuid4().hex
    directory.mkdir(mode=0o700, parents=True)
    steps = {}
    env = os.environ.copy()
    env.update(SMART_PARKING_API_PUBLISH="0", SMART_PARKING_OUTPUT_DIR=str(directory / "api"),
               SMART_PARKING_UPLOAD_DIR=str(directory / "uploads"), YOLO_CONFIG_DIR=str(directory / "yolo"))
    print(f"Quick-test directory: {directory}", flush=True)

    def command(name, command_args, command_env=env):
        log = directory / f"{name}.log"
        try:
            run_command(command_args, log, command_env)
        except (OSError, subprocess.CalledProcessError):
            steps[name] = {"status": "failed", "log": log.name}
            return False
        steps[name] = {"status": "passed", "log": log.name}
        return True

    command("dependencies", ["-m", "pip", "check"])
    preview_root = directory / "preview"
    source_arg = ["--source", str(args.source.resolve())] if args.source else ["--config", str(args.config.resolve())]
    if command("video_preview", ["scripts/run.py", "--mode", "preview", *source_arg,
                                  "--output-root", str(preview_root)]):
        run_dir = next(preview_root.iterdir())
        env["SMART_PARKING_CONFIG"] = str(run_dir / "run.yaml")
        try:
            with ExitStack() as stack:
                start_api(stack, "vehicle_intelligence.api", "/health", env, directory)
            steps["vehicle_intelligence_api"] = {"status": "passed", "log": "vehicle_intelligence.api.log"}
        except (ImportError, OSError, ValueError):
            steps["vehicle_intelligence_api"] = {"status": "failed", "log": "vehicle_intelligence.api.log"}
    else:
        steps["vehicle_intelligence_api"] = {"status": "blocked", "reason": "Preview configuration failed."}

    if args.preview_only:
        steps["controlled_postgresql"] = {"status": "skipped", "reason": "Requested preview-only checks."}
    elif not env.get("DATABASE_URL"):
        steps["controlled_postgresql"] = {"status": "blocked", "reason": "Set a local development DATABASE_URL with CREATEDB."}
    else:
        command("controlled_postgresql", ["scripts/smoke_database.py"])

    # The suite uses isolated storage. The real PostgreSQL check above is separate.
    test_env = {**env, "DATABASE_URL": f"sqlite:///{(directory / 'regression.db').as_posix()}"}
    command("regression", ["-m", "pytest", "-q", "-p", "no:cacheprovider",
                           f"--junitxml={directory / 'regression.xml'}"], test_env)
    status = ("failed" if any(step["status"] == "failed" for step in steps.values()) else
              "blocked" if any(step["status"] == "blocked" for step in steps.values()) else "passed")
    result = {"status": status, "scope": "preview-only" if args.preview_only else "controlled-integration",
              "real_recording_to_postgresql": "not_tested", "checks": steps}
    report = directory / "quick-test.json"
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for name, step in steps.items():
        print(f"{name}: {step['status']}")
    print(f"Overall: {status}. Report: {report}")
    return {"passed": 0, "failed": 1, "blocked": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
