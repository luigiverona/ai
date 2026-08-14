import pytest
import subprocess

from ai.components import software
from conftest import FakeRuntime


def test_missing_pacman_software_uses_full_upgrade(tmp_path):
    runtime = FakeRuntime(tmp_path, dry_run=True)
    software._ensure_pacman(runtime, ["discord"])
    assert (("sudo", "--", "pacman", "-Syu", "--needed", "--noconfirm", "discord"), True, None) in runtime.calls


def test_healthy_software_rerun_has_no_mutations(tmp_path):
    responses = {("pacman", "-Q", package): (0, "installed\n", "")
                 for package in (*software.PACMAN, *software.AUR)}
    responses.update({
        ("flatpak", "--version"): (0, "Flatpak 1\n", ""),
        ("flatpak", "--user", "remotes", "--columns=name"): (0, "flathub\n", ""),
        **{("flatpak", "--user", "info", app): (0, "installed\n", "") for app in software.FLATPAK},
    })
    runtime = FakeRuntime(tmp_path, responses)
    software.reconcile(runtime)
    assert runtime.changes == []
    assert not any(mutate for _, mutate, _ in runtime.calls)


def test_aur_dry_run_creates_no_temporary_state(tmp_path, monkeypatch):
    called = False
    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError
    monkeypatch.setattr(software.tempfile, "mkdtemp", forbidden)
    runtime = FakeRuntime(tmp_path, dry_run=True)
    software._build_aur(runtime, "librewolf-bin")
    assert not called
    assert runtime.changes == ["installed LibreWolf"]


def test_failed_aur_build_removes_component_temp(tmp_path, monkeypatch):
    temp = tmp_path / "aur-temp"
    temp.mkdir()
    monkeypatch.setattr(software.tempfile, "mkdtemp", lambda **kw: str(temp))
    runtime = FakeRuntime(tmp_path, {
        ("git", "clone", "--depth=1", "https://aur.archlinux.org/librewolf-bin.git", str(temp / "librewolf-bin")):
            (1, "", "clone failed")
    })
    monkeypatch.setattr(software, "_ensure_build_tools", lambda runtime: None)
    with pytest.raises(RuntimeError):
        software._build_aur(runtime, "librewolf-bin")
    assert not temp.exists()


def test_successful_aur_build_validates_identity_and_removes_temp(tmp_path, monkeypatch):
    temp = tmp_path / "aur-temp"
    temp.mkdir()
    source = temp / "librewolf-bin"
    artifact = source / "librewolf-bin-1-1-x86_64.pkg.tar.zst"
    monkeypatch.setattr(software.tempfile, "mkdtemp", lambda **kw: str(temp))
    monkeypatch.setattr(software, "_ensure_build_tools", lambda runtime: None)

    def response(argv, cwd):
        if argv[:2] == ["git", "clone"]:
            source.mkdir()
            (source / "PKGBUILD").write_text("pkgname=librewolf-bin")
            (source / ".SRCINFO").write_text("pkgbase = librewolf-bin\n")
        elif argv[0] == "makepkg" and "--syncdeps" in argv:
            artifact.write_text("package")
        return (0, "", "")

    responses = {
        ("git", "clone", "--depth=1", "https://aur.archlinux.org/librewolf-bin.git", str(source)): response,
        ("git", "-C", str(source), "remote", "get-url", "origin"):
            (0, "https://aur.archlinux.org/librewolf-bin.git\n", ""),
        ("git", "-C", str(source), "rev-parse", "HEAD"): (0, "abc\n", ""),
        ("git", "-C", str(source), "ls-remote", "origin", "HEAD"): (0, "abc\tHEAD\n", ""),
        ("pacman", "-Qp", str(artifact)): (0, "librewolf-bin 1-1\n", ""),
        ("pacman", "-Q", "librewolf-bin"): (0, "librewolf-bin 1-1\n", ""),
    }
    runtime = FakeRuntime(tmp_path, responses)
    original = runtime.run
    def dynamic(argv, **kwargs):
        if argv[0] == "makepkg" and "--syncdeps" in argv:
            runtime.calls.append((tuple(argv), kwargs.get("mutate", False), kwargs.get("cwd")))
            artifact.write_text("package")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "makepkg" and "--packagelist" in argv:
            runtime.calls.append((tuple(argv), kwargs.get("mutate", False), kwargs.get("cwd")))
            return subprocess.CompletedProcess(argv, 0, str(artifact) + "\n", "")
        return original(argv, **kwargs)
    runtime.run = dynamic
    software._build_aur(runtime, "librewolf-bin")
    assert runtime.changes == ["installed LibreWolf"]
    assert not temp.exists()
