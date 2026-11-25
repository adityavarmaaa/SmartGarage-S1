import json
import sys

import pytest
import yaml

from scripts import run
from vehicle_intelligence.common import config


def test_full_runner_respects_explicit_publishing_disable(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"publisher": {"enabled": False}, "cameras": [{}]}))
    monkeypatch.setattr(config, "load_config", lambda path: yaml.safe_load(path.read_text()))
    monkeypatch.setattr(config, "camera_configs", lambda cfg: [cfg])
    monkeypatch.setattr(run, "run_command", lambda *a, **kw: pytest.fail("A process was started"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    output = tmp_path / "runs"
    monkeypatch.setattr(sys, "argv", ["run.py", "--mode", "full", "--config", str(path), "--output-root", str(output)])

    assert run.main() == 1

    report_path, = output.glob("*/execution-summary.json")
    report = json.loads(report_path.read_text())
    assert report["publishing"] is False
    assert "Publishing is disabled" in report["error"]
