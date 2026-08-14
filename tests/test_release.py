import subprocess
import tarfile
import zipfile
import hashlib
from pathlib import Path


def test_release_builder_produces_executable_archive(tmp_path):
    repo = Path(__file__).parents[1]
    output = tmp_path / "output"
    result = subprocess.run([str(repo / "tools/build-release"), str(output)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    archive = output / "ai-0.1.1.tar.gz"
    assert archive.exists()
    extract = tmp_path / "extract"
    extract.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(extract, filter="data")
    run = subprocess.run([str(extract / "bin/ai"), "--version"], text=True, capture_output=True)
    assert run.returncode == 0
    assert run.stdout.strip() == "ai 0.1.1"
    with zipfile.ZipFile(extract / "bin/ai") as app:
        assert all("__pycache__" not in name and not name.endswith(".pyc") for name in app.namelist())
        assert app.namelist() == sorted(app.namelist())
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in app.infolist())


def test_installed_dry_run_does_not_change_home(tmp_path, monkeypatch):
    from ai import cli
    from ai.components import precheck
    from ai.runtime import Runtime

    home = tmp_path / "home with spaces"
    home.mkdir()
    monkeypatch.setattr(precheck, "reconcile", lambda runtime: None)
    monkeypatch.setattr(cli, "COMPONENTS", [("Test", lambda runtime: None)])
    monkeypatch.setattr(cli, "Runtime", lambda **kwargs: Runtime(home=home, **kwargs))
    before = hashlib.sha256(repr(list(home.rglob("*"))).encode()).hexdigest()
    assert cli.main(["--dry-run"]) == 0
    after = hashlib.sha256(repr(list(home.rglob("*"))).encode()).hexdigest()
    assert after == before
    assert list(home.iterdir()) == []
