from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
from pathlib import Path


def _release(tmp_path: Path, version="0.1.1"):
    server = tmp_path / f"server/{version}"
    payload = tmp_path / f"payload-{version}/bin"
    server.mkdir(parents=True)
    payload.mkdir(parents=True)
    executable = payload / "ai"
    executable.write_text(f"#!/bin/sh\necho 'ai {version}'\n")
    executable.chmod(0o755)
    archive = server / f"ai-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="bin")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (server / f"ai-{version}.tar.gz.sha256").write_text(f"{digest}  ai-{version}.tar.gz\n")
    return server.parent.as_uri()


def _run_installer(repo: Path, home: Path, base: str, version="0.1.1"):
    env = os.environ.copy()
    env.update(HOME=str(home), AI_RELEASE_BASE=base, AI_VERSION=version)
    return subprocess.run([str(repo / "install")], env=env, text=True, capture_output=True)


def test_installer_exact_version_rerun_is_idempotent(tmp_path):
    repo = Path(__file__).parents[1]
    home = tmp_path / "home"
    home.mkdir()
    base = _release(tmp_path)
    first = _run_installer(repo, home, base)
    assert first.returncode == 0, first.stderr
    before = sorted((p.relative_to(home), p.lstat().st_mtime_ns)
                    for p in home.rglob("*") if not p.is_symlink())
    second = _run_installer(repo, home, base)
    assert second.returncode == 0
    assert "already installed" in second.stdout
    after = sorted((p.relative_to(home), p.lstat().st_mtime_ns)
                   for p in home.rglob("*") if not p.is_symlink())
    assert after == before
    assert (home / ".local/bin/ai").resolve().is_file()


def test_installer_checksum_failure_leaves_installation_untouched(tmp_path):
    repo = Path(__file__).parents[1]
    home = tmp_path / "home"
    home.mkdir()
    base = _release(tmp_path)
    checksum = tmp_path / "server/0.1.1/ai-0.1.1.tar.gz.sha256"
    checksum.write_text("0" * 64 + "  ai-0.1.1.tar.gz\n")
    result = _run_installer(repo, home, base)
    assert result.returncode != 0
    assert not (home / ".local/bin/ai").exists()
    assert not (home / ".local/share/ai/current").exists()


def test_installer_rejects_archive_links_before_extraction(tmp_path):
    repo = Path(__file__).parents[1]
    home = tmp_path / "home"
    home.mkdir()
    server = tmp_path / "server/0.1.1"
    server.mkdir(parents=True)
    archive = server / "ai-0.1.1.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo("bin")
        link.type = tarfile.SYMTYPE
        link.linkname = str(tmp_path / "outside")
        tar.addfile(link)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (server / "ai-0.1.1.tar.gz.sha256").write_text(f"{digest}  ai-0.1.1.tar.gz\n")
    result = _run_installer(repo, home, server.parent.as_uri())
    assert result.returncode != 0
    assert not (tmp_path / "outside").exists()


def test_installer_recovers_missing_launcher(tmp_path):
    repo = Path(__file__).parents[1]
    home = tmp_path / "home"
    home.mkdir()
    base = _release(tmp_path)
    assert _run_installer(repo, home, base).returncode == 0
    (home / ".local/bin/ai").unlink()
    recovered = _run_installer(repo, home, base)
    assert recovered.returncode == 0, recovered.stderr
    assert (home / ".local/bin/ai").resolve().is_file()


def test_installer_update_removes_valid_obsolete_release(tmp_path):
    repo = Path(__file__).parents[1]
    home = tmp_path / "home"
    home.mkdir()
    base = _release(tmp_path, "0.0.9")
    assert _run_installer(repo, home, base, "0.0.9").returncode == 0
    _release(tmp_path, "0.1.1")
    updated = _run_installer(repo, home, base, "0.1.1")
    assert updated.returncode == 0, updated.stderr
    assert not (home / ".local/share/ai/releases/0.0.9").exists()
    assert (home / ".local/bin/ai").readlink() == home / ".local/share/ai/current/bin/ai"
    assert subprocess.run([home / ".local/bin/ai", "--version"], text=True,
                          capture_output=True).stdout.strip() == "ai 0.1.1"


def test_installer_rejects_unrelated_launcher(tmp_path):
    repo = Path(__file__).parents[1]
    home = tmp_path / "home"
    launcher = home / ".local/bin/ai"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("unrelated")
    result = _run_installer(repo, home, _release(tmp_path))
    assert result.returncode != 0
    assert launcher.read_text() == "unrelated"


def test_installer_recovers_interruption_after_release_move(tmp_path):
    repo = Path(__file__).parents[1]
    home = tmp_path / "home"
    home.mkdir()
    base = _release(tmp_path)
    release = home / ".local/share/ai/releases/0.1.1/bin"
    release.mkdir(parents=True)
    source = tmp_path / "payload-0.1.1/bin/ai"
    (release / "ai").write_bytes(source.read_bytes())
    (release / "ai").chmod(0o755)
    result = _run_installer(repo, home, base)
    assert result.returncode == 0, result.stderr
    assert (home / ".local/bin/ai").resolve().is_file()
