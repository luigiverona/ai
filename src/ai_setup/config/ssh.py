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
from ai_setup.config.ssh_inventory import (
    LocalKey,
    RemoteKey,
    SSHInventory,
    fingerprint_text,
    inventory_local,
    open_revalidated_root,
    open_ssh_root,
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

    def inventory(self) -> SSHInventory:
        return inventory_local(
            self.ssh_dir,
            owner_uid=os.getuid(),
            dedicated_name=self.key.name,
        )

    def inventory_remote(self) -> tuple[RemoteKey, ...]:
        result = self.runner.run(
            Command(
                (
                    "gh",
                    "api",
                    "user/keys",
                    "--paginate",
                    "--jq",
                    '.[] | "\\(.id)\\t\\(.title)\\t\\(.key)"',
                ),
                mutate=False,
                sensitive_output=True,
            )
        )
        keys: list[RemoteKey] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].isdigit():
                keys.append(RemoteKey(int(parts[0]), parts[1], fingerprint_text(parts[2])))
        return tuple(sorted(keys, key=lambda key: key.key_id))

    def create(self, email: str) -> bool:
        ensure_managed_directory(
            trusted_root=self.home,
            directory=self.ssh_dir,
            mode=0o700,
            owner_uid=os.getuid(),
        )
        root_fd, _ = open_ssh_root(self.ssh_dir, owner_uid=os.getuid(), missing_ok=False)
        if root_fd is None:
            raise ValidationError("SSH", "inspect dedicated key", "SSH root is missing")
        try:
            try:
                private = os.stat(self.key.name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                private = None
            try:
                public = os.stat(self.key.name + ".pub", dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                public = None
        finally:
            os.close(root_fd)
        for name, item in ((self.key.name, private), (self.key.name + ".pub", public)):
            if item is not None and (
                not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid() or item.st_nlink != 1
            ):
                raise ValidationError(
                    "SSH",
                    "inspect dedicated key",
                    f"{name} is symbolic, non-regular, wrong-owner, or hard-linked",
                )
        if (private is None) != (public is None):
            raise ValidationError(
                "SSH",
                "inspect dedicated key",
                "dedicated SSH key pair is incomplete; refusing to overwrite it",
            )
        created = private is None
        if created:
            self.runner.run(
                Command(("ssh-keygen", "-t", "ed25519", "-f", str(self.key), "-C", email, "-N", ""))
            )
        if not self.runner.dry_run:
            inventory = self.inventory()
            dedicated = next(
                (key for key in inventory.keys if key.private_name == self.key.name),
                None,
            )
            if dedicated is None:
                raise ValidationError(
                    "SSH",
                    "verify dedicated key",
                    "dedicated key pair is unsafe or has an invalid public key",
                )
            root_fd, current_root = open_ssh_root(
                self.ssh_dir, owner_uid=os.getuid(), missing_ok=False
            )
            if root_fd is None or current_root != dedicated.root:
                if root_fd is not None:
                    os.close(root_fd)
                raise ValidationError(
                    "SSH", "verify dedicated key", "SSH root changed before mode update"
                )
            try:
                for name, mode, expected in (
                    (dedicated.private_name, 0o600, dedicated.private),
                    (dedicated.public_name, 0o644, dedicated.public),
                ):
                    fd = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=root_fd,
                    )
                    try:
                        current = os.fstat(fd)
                        if (
                            current.st_dev,
                            current.st_ino,
                            current.st_uid,
                            current.st_nlink,
                        ) != (
                            expected.device,
                            expected.inode,
                            expected.owner_uid,
                            expected.link_count,
                        ) or not stat.S_ISREG(current.st_mode):
                            raise ValidationError(
                                "SSH",
                                "verify dedicated key",
                                f"{name} changed before mode update",
                            )
                        os.fchmod(fd, mode)
                    finally:
                        os.close(fd)
            finally:
                os.close(root_fd)
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

    def delete(self, keys: tuple[LocalKey, ...], *, explicit_confirmation: bool) -> None:
        if not explicit_confirmation:
            raise PermissionError("SSH key deletion requires explicit confirmation")
        root_fd = open_revalidated_root(
            self.ssh_dir,
            keys,
            owner_uid=os.getuid(),
            dedicated=self.key,
        )
        removed: list[str] = []
        try:
            if self.runner.dry_run:
                return
            for key in keys:
                for name, expected in (
                    (key.public_name, key.public),
                    (key.private_name, key.private),
                ):
                    current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if (
                        current.st_dev,
                        current.st_ino,
                        current.st_mode,
                        current.st_uid,
                        current.st_nlink,
                        current.st_size,
                        current.st_mtime_ns,
                        current.st_ctime_ns,
                    ) != (
                        expected.device,
                        expected.inode,
                        expected.mode,
                        expected.owner_uid,
                        expected.link_count,
                        expected.size,
                        expected.modified_ns,
                        expected.changed_ns,
                    ):
                        raise ValidationError(
                            "SSH",
                            "delete keys",
                            "SSH inventory changed; review it again before deletion",
                        )
                    try:
                        os.unlink(name, dir_fd=root_fd)
                    except OSError as exc:
                        remaining = [
                            pending
                            for selected in keys
                            for pending in (selected.public_name, selected.private_name)
                            if pending not in removed
                        ]
                        raise ValidationError(
                            "SSH",
                            "delete keys",
                            f"removed {removed or 'nothing'}; remaining {remaining}: {exc}",
                        ) from exc
                    removed.append(name)
        finally:
            os.close(root_fd)

    def validate_deletion(self, keys: tuple[LocalKey, ...]) -> None:
        root_fd = open_revalidated_root(
            self.ssh_dir,
            keys,
            owner_uid=os.getuid(),
            dedicated=self.key,
        )
        os.close(root_fd)

    def delete_remote(
        self,
        keys: tuple[RemoteKey, ...],
        *,
        eligible_fingerprints: frozenset[str],
        explicit_confirmation: bool,
    ) -> None:
        if not explicit_confirmation:
            raise PermissionError("GitHub key deletion requires explicit confirmation")
        for key in keys:
            if not key.fingerprint or key.fingerprint not in eligible_fingerprints:
                raise PermissionError(f"ineligible GitHub key: {key.title}")
        for key in keys:
            self.runner.run(Command(("gh", "api", "--method", "DELETE", f"user/keys/{key.key_id}")))
