import errno
import subprocess
import sys

import pytest

from scripts import setup


@pytest.mark.parametrize("failure", ["venv", "pip"])
def test_setup_reports_storage_and_failed_stage(monkeypatch, tmp_path, capsys, failure):
    config = tmp_path / "preview.yaml"
    config.touch()
    monkeypatch.setattr(setup, "__file__", str(tmp_path / "scripts" / "setup.py"))
    monkeypatch.setattr(sys, "argv", ["setup.py", "--config", str(config), "--cpu-torch"])
    for variable in ("DATABASE_URL", "S2_API_KEY", "SP_ENCRYPTION_KEYS",
                     "SP_ACTIVE_ENCRYPTION_KEY", "SP_HMAC_KEY"):
        monkeypatch.setenv(variable, "test-placeholder")

    def create_environment(path, *, with_pip):
        assert with_pip
        if failure == "venv":
            raise OSError(errno.ENOSPC, "No space left on device")
        path.mkdir()

    def run(command, **kwargs):
        if command[1:3] == ["-m", "pip"]:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(setup.venv, "create", create_environment)
    monkeypatch.setattr(setup.subprocess, "run", run)

    with pytest.raises(SystemExit) as raised:
        setup.main()

    error = capsys.readouterr().err
    assert raised.value.code == 1
    assert "Free space on project volume:" in error
    assert "Free space on temporary volume:" in error
    expected_stage = "virtual environment creation" if failure == "venv" else "CPU PyTorch dependency installation"
    assert f"Setup failed during {expected_stage}" in error
    assert "test-placeholder" not in error
