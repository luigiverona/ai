from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ai_setup.config.codex_installer import (
    TRUSTED_CODEX_INSTALLER,
    CodexInstallerProvenance,
    verify_codex_installer,
)
from ai_setup.config.files import (
    ExistingFilePolicy,
    ensure_managed_directory,
    inspect_managed_file,
    replace_managed_file,
)
from ai_setup.errors import CommandError, ValidationError
from ai_setup.execution.runner import (
    Command,
    CommandRunner,
    interactive_terminal_available,
    manual_authentication_env,
)
from ai_setup.identity import IDENTITY


@dataclass(frozen=True, slots=True)
class CodexProfile:
    identifier: str
    launcher_name: str
    directory_name: str
    display_label: str


CODEX_PROFILES = (
    CodexProfile("01", "codex-01", "01", "codex-01"),
    CodexProfile("02", "codex-02", "02", "codex-02"),
)


def codex_profile(identifier: str) -> CodexProfile:
    return next(profile for profile in CODEX_PROFILES if profile.identifier == identifier)


class CodexManager:
    def __init__(
        self,
        runner: CommandRunner,
        home: Path,
        workspace: Path | None = None,
        *,
        installer_provenance: CodexInstallerProvenance = TRUSTED_CODEX_INSTALLER,
    ) -> None:
        self.runner = runner
        self.home = home
        self.state_root = IDENTITY.codex_state_root(home)
        self.bin_dir = home / ".local/bin"
        self.shared_bin = IDENTITY.codex_shared_binary(home)
        self.workspace = workspace
        self.installer_provenance = installer_provenance

    def install(self) -> None:
        owner_uid = os.getuid()
        ensure_managed_directory(
            trusted_root=self.home,
            directory=self.shared_bin.parent,
            mode=0o755,
            owner_uid=owner_uid,
            intermediate_mode=0o755,
        )
        ensure_managed_directory(
            trusted_root=self.home,
            directory=self.state_root,
            mode=0o700,
            owner_uid=owner_uid,
            intermediate_mode=0o755,
        )
        installer_state = self.state_root / "installer"
        isolated_home = installer_state / "environment-home"
        for directory in (installer_state, isolated_home):
            ensure_managed_directory(
                trusted_root=self.home,
                directory=directory,
                mode=0o700,
                owner_uid=owner_uid,
            )
        env = {
            "CODEX_INSTALL_DIR": str(self.shared_bin.parent),
            "CODEX_HOME": str(installer_state),
            "CODEX_RELEASE": "latest",
            "CODEX_NON_INTERACTIVE": "1",
            "HOME": str(isolated_home),
            "SHELL": "/bin/sh",
            "PATH": os.defpath,
            "LC_ALL": "C",
        }
        for name in (
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "https_proxy",
            "http_proxy",
            "all_proxy",
            "no_proxy",
        ):
            if value := os.environ.get(name):
                env[name] = value
        download_root = self.workspace / "downloads" if self.workspace else installer_state
        installer = download_root / "codex-install.sh"
        download = self.runner.run(
            Command(
                (
                    "curl",
                    "-fsSL",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--tlsv1.2",
                    "--max-redirs",
                    "1",
                    "--max-filesize",
                    str(self.installer_provenance.maximum_bytes),
                    "--write-out",
                    "%{url_effective}\n%{content_type}\n%{size_download}\n",
                    "-o",
                    str(installer),
                    self.installer_provenance.canonical_url,
                )
            )
        )
        if not self.runner.dry_run:
            metadata = download.stdout.splitlines()
            if len(metadata) != 3 or not metadata[2].isdigit():
                raise ValidationError(
                    "codex", "verify official installer", "invalid download metadata"
                )
            verify_codex_installer(
                installer.read_bytes(),
                effective_url=metadata[0],
                content_type=metadata[1],
                reported_size=int(metadata[2]),
                provenance=self.installer_provenance,
            )
        self.runner.run(Command(("sh", str(installer)), env=env, replace_env=True))
        if not self.runner.dry_run and not self.shared_bin.is_file():
            raise ValidationError(
                "codex",
                "install official release",
                "installer did not create the shared executable",
            )
        version = self.runner.run(
            Command((str(self.shared_bin), "--version"), mutate=False), check=False
        )
        if version.returncode and not self.runner.dry_run:
            raise ValidationError("codex", "verify executable", "shared executable is not runnable")

    def executable_valid(self) -> bool:
        if not self.shared_bin.is_file():
            return False
        result = self.runner.run(
            Command((str(self.shared_bin), "--version"), mutate=False), check=False
        )
        return result.returncode == 0

    def unrelated_codex(self) -> Path | None:
        found = shutil.which("codex")
        if not found:
            return None
        path = Path(found)
        try:
            if path.resolve(strict=False) == self.shared_bin.resolve(strict=False):
                return None
        except OSError:
            pass
        return path

    def create_profiles(self) -> None:
        owner_uid = os.getuid()
        if not self.runner.dry_run:
            ensure_managed_directory(
                trusted_root=self.home,
                directory=self.bin_dir,
                mode=0o755,
                owner_uid=owner_uid,
                intermediate_mode=0o755,
            )
            ensure_managed_directory(
                trusted_root=self.home,
                directory=self.state_root,
                mode=0o700,
                owner_uid=owner_uid,
                intermediate_mode=0o755,
            )
        for spec in CODEX_PROFILES:
            profile = self.state_root / spec.directory_name
            if not self.runner.dry_run:
                ensure_managed_directory(
                    trusted_root=self.home,
                    directory=profile,
                    mode=0o700,
                    owner_uid=owner_uid,
                )
                config = profile / "config.toml"
                config_snapshot = inspect_managed_file(
                    trusted_root=self.home,
                    target=config,
                    owner_uid=owner_uid,
                )
                existing = (
                    config_snapshot.content.decode("utf-8") if config_snapshot is not None else ""
                )
                setting = 'cli_auth_credentials_store = "file"'
                pattern = re.compile(r"(?m)^\s*cli_auth_credentials_store\s*=.*$")
                if pattern.search(existing):
                    content = pattern.sub(setting, existing, count=1)
                else:
                    content = existing
                    if content and not content.endswith("\n"):
                        content += "\n"
                    content += setting + "\n"
                replace_managed_file(
                    trusted_root=self.home,
                    target=config,
                    content=content,
                    mode=0o600,
                    owner_uid=owner_uid,
                    expected=config_snapshot,
                    existing_policy=ExistingFilePolicy.USER_OWNED,
                )
                launcher = (
                    f'#!/bin/sh\nexport CODEX_HOME="{profile}"\nexec "{self.shared_bin}" "$@"\n'
                )
                launcher_path = self.bin_dir / spec.launcher_name
                launcher_snapshot = inspect_managed_file(
                    trusted_root=self.home,
                    target=launcher_path,
                    owner_uid=owner_uid,
                )
                replace_managed_file(
                    trusted_root=self.home,
                    target=launcher_path,
                    content=launcher,
                    mode=0o700,
                    owner_uid=owner_uid,
                    expected=launcher_snapshot,
                    existing_policy=ExistingFilePolicy.EXACT_CONTENT,
                    directory_mode=0o755,
                )

    def device_auth_supported(self, number: str) -> bool:
        spec = codex_profile(number)
        launcher = str(self.bin_dir / spec.launcher_name)
        result = self.runner.run(
            Command((launcher, "login", "--help"), mutate=False),
            check=False,
        )
        return result.returncode == 0 and "--device-auth" in result.stdout

    def authenticate(self, number: str, *, device_auth: bool = True) -> None:
        spec = codex_profile(number)
        launcher = str(self.bin_dir / spec.launcher_name)
        if not self.runner.dry_run and not interactive_terminal_available():
            raise ValidationError(
                "Codex",
                f"authenticate {spec.display_label}",
                "authentication requires an interactive terminal; "
                f"run {IDENTITY.command_name} codex from a terminal",
            )
        try:
            self.runner.run(
                Command(
                    (launcher, "login", "--device-auth") if device_auth else (launcher, "login"),
                    env=manual_authentication_env(),
                    interactive=True,
                    failure_component="codex",
                    failure_operation=f"authenticate {spec.display_label}",
                )
            )
        except CommandError as exc:
            mode = "device authentication" if device_auth else "manual authentication"
            raise ValidationError(
                "codex",
                f"authenticate {spec.display_label}",
                f"{mode} exited with status {exc.exit_code}; "
                "sign-in was cancelled or did not complete",
                exc.exit_code,
            ) from exc
        if not self.runner.dry_run and not self.verified(number):
            raise ValidationError(
                "codex",
                f"authenticate {spec.display_label}",
                "sign-in was cancelled or did not complete",
            )

    def verified(self, number: str) -> bool:
        spec = codex_profile(number)
        profile = self.state_root / spec.directory_name
        launcher = self.bin_dir / spec.launcher_name
        if not (profile.is_dir() and launcher.is_file() and self.shared_bin.is_file()):
            return False
        if profile.stat().st_mode & 0o077 or launcher.stat().st_mode & 0o077:
            return False
        expected = f'export CODEX_HOME="{profile}"'
        if expected not in launcher.read_text(encoding="utf-8").splitlines():
            return False
        auth_file = profile / "auth.json"
        if auth_file.is_symlink() or (auth_file.exists() and auth_file.stat().st_mode & 0o077):
            return False
        result = self.runner.run(
            Command((str(launcher), "login", "status"), mutate=False), check=False
        )
        return result.returncode == 0

    def profiles_distinct(self) -> bool:
        launchers = [self.bin_dir / spec.launcher_name for spec in CODEX_PROFILES]
        if not all(path.is_file() for path in launchers):
            return False
        contents = [path.read_text(encoding="utf-8") for path in launchers]
        return all(
            str(self.state_root / spec.directory_name) in content
            for spec, content in zip(CODEX_PROFILES, contents, strict=True)
        ) and len(set(contents)) == len(CODEX_PROFILES)
