from __future__ import annotations

import shutil
import shlex
import re
import configparser
import tempfile
from pathlib import Path

from ..errors import AiError
from ..runtime import Runtime

PACMAN = {"discord": "Discord", "mullvad-vpn": "Mullvad VPN", "spotify-launcher": "Spotify"}
AUR = {"librewolf-bin": "LibreWolf", "mullvad-browser-bin": "Mullvad Browser",
       "visual-studio-code-bin": "Visual Studio Code"}
FLATPAK = {"com.valvesoftware.Steam": "Steam", "org.vinegarhq.Sober": "Sober"}


def _installed(runtime: Runtime, package: str) -> bool:
    return runtime.run(["pacman", "-Q", package], check=False).returncode == 0


def _ensure_pacman(runtime: Runtime, missing: list[str]) -> None:
    if missing:
        runtime.sudo(["pacman", "-Syu", "--needed", "--noconfirm", *missing])
        for package in missing:
            if not runtime.dry_run and not _installed(runtime, package):
                raise AiError(f"Software: pacman did not install {package}")
            runtime.changed(f"installed {PACMAN.get(package, package)}")


def _ensure_build_tools(runtime: Runtime) -> None:
    missing = [p for p in ("base-devel", "git", "gnupg") if not _installed(runtime, p)]
    # Any AUR build may install repository dependencies. Synchronize and upgrade
    # first even when the build tools already exist, preserving Arch's supported
    # no-partial-upgrade model.
    runtime.sudo(["pacman", "-Syu", "--needed", "--noconfirm", *missing])


def _build_aur(runtime: Runtime, package: str) -> None:
    if runtime.dry_run:
        runtime.changed(f"installed {AUR[package]}")
        return
    _ensure_build_tools(runtime)
    temp = Path(tempfile.mkdtemp(prefix=f"ai-aur-{package}-"))
    try:
        source = temp / package
        runtime.run(["git", "clone", "--depth=1", f"https://aur.archlinux.org/{package}.git", str(source)])
        origin = runtime.run(["git", "-C", str(source), "remote", "get-url", "origin"]).stdout.strip()
        if origin != f"https://aur.archlinux.org/{package}.git":
            raise AiError(f"Software: invalid AUR origin for {package}")
        local_head = runtime.run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
        remote_head = runtime.run(["git", "-C", str(source), "ls-remote", "origin", "HEAD"]).stdout.split()
        if len(remote_head) < 1 or remote_head[0] != local_head:
            raise AiError(f"Software: AUR source HEAD verification failed for {package}")
        for required in (source / "PKGBUILD", source / ".SRCINFO"):
            if required.is_symlink() or not required.is_file():
                raise AiError(f"Software: invalid AUR source metadata for {package}")
        gpg_home = temp / "gnupg"
        gpg_home.mkdir(mode=0o700)
        fingerprints = []
        for line in (source / ".SRCINFO").read_text().splitlines():
            key, separator, value = line.strip().partition(" = ")
            if key == "validpgpkeys" and separator:
                if not re.fullmatch(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}", value):
                    raise AiError(f"Software: invalid AUR signing fingerprint for {package}")
                fingerprints.append(value.upper())
        env = {"GNUPGHOME": str(gpg_home)}
        for fingerprint in fingerprints:
            runtime.run(["gpg", "--batch", "--keyserver", "hkps://keyserver.ubuntu.com",
                         "--recv-keys", fingerprint], env=env, mutate=True)
            listing = runtime.run(["gpg", "--batch", "--with-colons", "--fingerprint", fingerprint],
                                  env=env).stdout
            imported = {line.split(":")[9].upper() for line in listing.splitlines()
                        if line.startswith("fpr:") and len(line.split(":")) > 9}
            if fingerprint not in imported:
                raise AiError(f"Software: failed to verify signing key for {package}")
        build = temp / "build"
        build.mkdir()
        config = temp / "makepkg.conf"
        config.write_text("source /etc/makepkg.conf\n" + "\n".join(
            f"{name}={shlex.quote(str(path))}" for name, path in (
                ("PKGDEST", source), ("SRCDEST", temp / "sources"),
                ("SRCPKGDEST", temp / "source-packages"), ("LOGDEST", temp / "logs"),
                ("BUILDDIR", build))) + "\n")
        runtime.run(["makepkg", "--config", str(config), "--syncdeps", "--needed", "--noconfirm"],
                    cwd=source, env=env, mutate=True)
        artifacts = [Path(p) for p in runtime.run(
            ["makepkg", "--config", str(config), "--packagelist"], cwd=source, env=env).stdout.splitlines()]
        matches: list[Path] = []
        for artifact in artifacts:
            try:
                artifact.resolve().relative_to(temp.resolve())
            except ValueError:
                raise AiError(f"Software: AUR artifact escaped build directory for {package}") from None
            if artifact.is_symlink() or not artifact.is_file():
                continue
            metadata = runtime.run(["pacman", "-Qp", str(artifact)]).stdout.split()
            name = metadata[0] if metadata else ""
            if name == package:
                matches.append(artifact)
        if len(matches) != 1:
            raise AiError(f"Software: AUR artifact identity mismatch for {package}")
        runtime.sudo(["pacman", "-U", "--noconfirm", str(matches[0])])
        if not _installed(runtime, package):
            raise AiError(f"Software: failed to verify AUR package {package}")
        runtime.changed(f"installed {AUR[package]}")
    finally:
        shutil.rmtree(temp)


def reconcile(runtime: Runtime) -> None:
    runtime.require_command("pacman", "Software")
    missing = [p for p in PACMAN if not _installed(runtime, p)]
    _ensure_pacman(runtime, missing)
    for package in AUR:
        if not _installed(runtime, package):
            _build_aur(runtime, package)
    if runtime.run(["flatpak", "--version"], check=False).returncode != 0:
        _ensure_pacman(runtime, ["flatpak"])
        if runtime.dry_run:
            for app, label in FLATPAK.items():
                runtime.changed(f"installed {label}")
            return
    if runtime.dry_run:
        root = runtime.home / ".local/share/flatpak"
        config = configparser.ConfigParser()
        config.read(root / "repo/config")
        remotes = [section[8:-1] for section in config.sections()
                   if section.startswith('remote "') and section.endswith('"')]
        present_apps = {app for app in FLATPAK if (root / "app" / app).exists()}
    else:
        remotes = runtime.run(["flatpak", "--user", "remotes", "--columns=name"], check=False).stdout.split()
        present_apps = set()
    if "flathub" not in remotes:
        runtime.run(["flatpak", "--user", "remote-add", "--if-not-exists", "flathub",
                     "https://dl.flathub.org/repo/flathub.flatpakrepo"], mutate=True)
        if not runtime.dry_run:
            actual = runtime.run(["flatpak", "--user", "remotes", "--columns=name"]).stdout.split()
            if "flathub" not in actual:
                raise AiError("Software: failed to verify Flathub remote")
    for app, label in FLATPAK.items():
        present = app in present_apps if runtime.dry_run else runtime.run(
            ["flatpak", "--user", "info", app], check=False).returncode == 0
        if not present:
            runtime.run(["flatpak", "--user", "install", "--noninteractive", "flathub", app], mutate=True)
            if not runtime.dry_run and runtime.run(["flatpak", "--user", "info", app], check=False).returncode:
                raise AiError(f"Software: failed to verify Flatpak {app}")
            runtime.changed(f"installed {label}")
