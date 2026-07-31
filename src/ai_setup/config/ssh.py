from __future__ import annotations

import os
import stat
from pathlib import Path

from ai_setup.config.files import (
    ExistingFilePolicy,
    ensure_managed_directory,
    inspect_managed_file,
    replace_managed_file,
)
from ai_setup.errors import ValidationError
from ai_setup.execution.runner import Command, CommandRunner
from ai_setup.identity import IDENTITY


class SSHManager:
    def __init__(self, runner: CommandRunner, home: Path) -> None:
        self.runner = runner
        self.home = home
        self.ssh_dir = home / ".ssh"
        self.key = IDENTITY.ssh_key(home)

    def _inspect_dedicated_pair(self) -> bool:
        owner_uid = os.getuid()
        try:
            root = self.ssh_dir.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode) or root.st_uid != owner_uid:
            raise ValidationError("SSH", "inspect dedicated key", "SSH root is unsafe")

        observed: list[os.stat_result | None] = []
        for path in (self.key, self.key.with_suffix(".pub")):
            try:
                item = path.lstat()
            except FileNotFoundError:
                item = None
            if item is not None and (
                not stat.S_ISREG(item.st_mode) or item.st_uid != owner_uid or item.st_nlink != 1
            ):
                raise ValidationError(
                    "SSH",
                    "inspect dedicated key",
                    f"{path.name} is symbolic, non-regular, wrong-owner, or hard-linked",
                )
            observed.append(item)
        private, public = observed
        if (private is None) != (public is None):
            raise ValidationError(
                "SSH",
                "inspect dedicated key",
                "dedicated SSH key pair is incomplete; refusing to overwrite it",
            )
        if private is None:
            return False
        if public is None:
            raise RuntimeError("dedicated key pair validation lost its public key")

        public_path = self.key.with_suffix(".pub")
        try:
            public_fd = os.open(
                public_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                opened = os.fstat(public_fd)
                if (opened.st_dev, opened.st_ino) != (public.st_dev, public.st_ino):
                    raise OSError("dedicated public key changed while opening")
                public_bytes = b""
                while chunk := os.read(public_fd, 4096):
                    public_bytes += chunk
                    if len(public_bytes) > 1024 * 1024:
                        raise OSError("dedicated public key is too large")
            finally:
                os.close(public_fd)
            public_parts = public_bytes.decode("ascii").split()
        except (OSError, UnicodeError) as exc:
            raise ValidationError(
                "SSH", "inspect dedicated key", "dedicated public key is invalid"
            ) from exc
        if len(public_parts) < 2 or public_parts[0] != "ssh-ed25519":
            raise ValidationError("SSH", "inspect dedicated key", "dedicated public key is invalid")
        self.runner.run(
            Command(
                ("ssh-keygen", "-l", "-f", str(public_path)),
                mutate=False,
                sensitive_output=True,
                failure_component="SSH",
                failure_operation="validate dedicated public key",
            )
        )
        derived = self.runner.run(
            Command(
                ("ssh-keygen", "-y", "-f", str(self.key)),
                mutate=False,
                sensitive_output=True,
                failure_component="SSH",
                failure_operation="validate dedicated key",
            )
        ).stdout.split()
        if len(derived) < 2 or derived[:2] != public_parts[:2]:
            raise ValidationError(
                "SSH",
                "inspect dedicated key",
                "dedicated private and public keys do not match",
            )
        for path, expected in ((self.key, private), (public_path, public)):
            current = path.lstat()
            if (
                current.st_dev,
                current.st_ino,
                current.st_uid,
                current.st_nlink,
                current.st_size,
                current.st_mtime_ns,
            ) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_uid,
                expected.st_nlink,
                expected.st_size,
                expected.st_mtime_ns,
            ):
                raise ValidationError(
                    "SSH", "inspect dedicated key", f"{path.name} changed during validation"
                )
        return True

    def create(self, email: str) -> bool:
        ensure_managed_directory(
            trusted_root=self.home,
            directory=self.ssh_dir,
            mode=0o700,
            owner_uid=os.getuid(),
        )
        created = not self._inspect_dedicated_pair()
        if created:
            self.runner.run(
                Command(("ssh-keygen", "-t", "ed25519", "-f", str(self.key), "-C", email, "-N", ""))
            )
        if not self.runner.dry_run:
            if not self._inspect_dedicated_pair():
                raise ValidationError(
                    "SSH", "verify dedicated key", "dedicated key pair is missing"
                )
            for path, mode in ((self.key, 0o600), (self.key.with_suffix(".pub"), 0o644)):
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    current = os.fstat(fd)
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or current.st_uid != os.getuid()
                        or current.st_nlink != 1
                    ):
                        raise ValidationError(
                            "SSH", "verify dedicated key", f"{path.name} changed before mode update"
                        )
                    os.fchmod(fd, mode)
                finally:
                    os.close(fd)
        self._configure_host()
        return created

    def _configure_host(self) -> None:
        config = self.ssh_dir / "config"
        include = f"Include ~/.ssh/config.d/{IDENTITY.ssh_fragment_filename}"
        owner_uid = os.getuid()
        config_snapshot = inspect_managed_file(
            trusted_root=self.home,
            target=config,
            owner_uid=owner_uid,
        )
        existing = config_snapshot.content.decode("utf-8") if config_snapshot is not None else ""
        content = existing if include in existing.splitlines() else include + "\n" + existing
        owned = IDENTITY.ssh_fragment(self.home)
        block = (
            "Host github.com\n"
            "    HostName github.com\n"
            "    User git\n"
            f"    IdentityFile {self.key}\n"
            "    IdentitiesOnly yes\n"
        )
        if not self.runner.dry_run:
            owned_snapshot = inspect_managed_file(
                trusted_root=self.home,
                target=owned,
                owner_uid=owner_uid,
            )
            if owned_snapshot is not None and owned_snapshot.content != block.encode():
                raise ValidationError(
                    "SSH",
                    "configure host",
                    f"existing {owned} is not recognized as managed",
                )
            replace_managed_file(
                trusted_root=self.home,
                target=config,
                content=content,
                mode=0o600,
                owner_uid=owner_uid,
                expected=config_snapshot,
                existing_policy=ExistingFilePolicy.USER_OWNED,
            )
            replace_managed_file(
                trusted_root=self.home,
                target=owned,
                content=block,
                mode=0o600,
                owner_uid=owner_uid,
                expected=owned_snapshot,
                existing_policy=ExistingFilePolicy.EXACT_CONTENT,
            )

    def upload(self, title: str) -> None:
        self.runner.run(
            Command(("gh", "ssh-key", "add", str(self.key.with_suffix(".pub")), "--title", title))
        )

    def verify(self, *, read_only: bool = False) -> bool:
        argv: tuple[str, ...] = ("ssh", "-T", "-o", "BatchMode=yes", "git@github.com")
        if read_only:
            argv = (
                "ssh",
                "-T",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPersist=no",
                "-o",
                "ControlPath=none",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                f"UserKnownHostsFile={self.ssh_dir / 'known_hosts'}",
                "-i",
                str(self.key),
                "git@github.com",
            )
        result = self.runner.run(
            Command(argv, mutate=False),
            check=False,
        )
        return result.returncode == 1 and "successfully authenticated" in (
            result.stdout + result.stderr
        )
