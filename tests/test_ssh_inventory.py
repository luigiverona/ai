from __future__ import annotations

import base64
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_setup.config.ssh import SSHManager
from ai_setup.config.ssh_inventory import (
    RemoteKey,
    fingerprint_text,
    github_correlated_local_keys,
    inventory_local,
)
from ai_setup.errors import ValidationError
from ai_setup.execution.runner import Command, CommandResult
from tests.helpers import FakeRunner

PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "fixture@example\n"
)
FINGERPRINT = "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA"


class SSHInventoryTests(unittest.TestCase):
    def home(self, raw: str) -> Path:
        home = Path(raw) / "home"
        home.mkdir(mode=0o700)
        return home

    def pair(
        self,
        ssh: Path,
        name: str,
        *,
        public: str = PUBLIC_KEY,
    ) -> tuple[Path, Path]:
        private = ssh / name
        public_path = ssh / f"{name}.pub"
        private.write_text("private fixture bytes are never read\n")
        public_path.write_text(public)
        return private, public_path

    def test_missing_root_returns_empty_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            result = inventory_local(home / ".ssh")
            self.assertIsNone(result.root)
            self.assertEqual((result.keys, result.unsafe), ((), ()))
            self.assertFalse((home / ".ssh").exists())

    def test_safe_root_and_pair_are_accepted_nonrecursively_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            self.pair(ssh, "z-key")
            self.pair(ssh, "a-key")
            nested = ssh / "nested"
            nested.mkdir()
            self.pair(nested, "nested-key")
            result = inventory_local(ssh)
            self.assertEqual([key.private_name for key in result.keys], ["a-key", "z-key"])
            self.assertEqual([key.fingerprint for key in result.keys], [FINGERPRINT] * 2)
            self.assertNotIn("nested-key", {key.private_name for key in result.keys})

    def test_root_symlink_non_directory_and_wrong_owner_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            outside = Path(raw) / "outside"
            outside.mkdir()
            ssh = home / ".ssh"
            ssh.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "SSH root is a symbolic link"):
                inventory_local(ssh)
            ssh.unlink()
            ssh.write_text("not a directory")
            with self.assertRaisesRegex(ValidationError, "SSH root is not a directory"):
                inventory_local(ssh)
            ssh.unlink()
            ssh.mkdir()
            real_lstat = Path.lstat

            def wrong_owner(path: Path) -> os.stat_result:
                item = real_lstat(path)
                if path == ssh:
                    values = list(item)
                    values[4] = item.st_uid + 1
                    return os.stat_result(values)
                return item

            with (
                patch.object(Path, "lstat", wrong_owner),
                self.assertRaisesRegex(ValidationError, "SSH root is not owned"),
            ):
                inventory_local(ssh)

    def test_private_symlink_is_preserved_and_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            outside = Path(raw) / "private-target"
            outside.write_text("secret target")
            (ssh / "id_link").symlink_to(outside)
            (ssh / "id_link.pub").write_text(PUBLIC_KEY)
            result = inventory_local(ssh)
            self.assertEqual(result.keys, ())
            self.assertIn("symbolic private path", {entry.reason for entry in result.unsafe})
            self.assertEqual(outside.read_text(), "secret target")

    def test_public_symlink_variants_are_never_opened_or_fingerprinted(self) -> None:
        variants = ("external", "local", "dangling", "loop")
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                home = self.home(raw)
                ssh = home / ".ssh"
                ssh.mkdir()
                (ssh / "id_test").write_text("private")
                outside = Path(raw) / "outside.pub"
                outside.write_text(PUBLIC_KEY)
                public = ssh / "id_test.pub"
                if variant == "external":
                    public.symlink_to(outside)
                elif variant == "local":
                    other = ssh / "other.pub"
                    other.write_text(PUBLIC_KEY)
                    public.symlink_to(other.name)
                elif variant == "dangling":
                    public.symlink_to("missing.pub")
                else:
                    public.symlink_to(public.name)
                real_open = os.open

                def guarded_open(
                    path: str | os.PathLike[str],
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                    public_name: str = public.name,
                    opener: object = real_open,
                ) -> int:
                    if path == public_name:
                        raise AssertionError("symbolic public key was opened")
                    return opener(path, flags, mode, dir_fd=dir_fd)  # type: ignore[operator]

                with patch("ai_setup.config.ssh_inventory.os.open", guarded_open):
                    result = inventory_local(ssh)
                self.assertEqual(result.keys, ())
                self.assertIn("symbolic public path", {entry.reason for entry in result.unsafe})
                self.assertEqual(outside.read_text(), PUBLIC_KEY)

    def test_directories_fifo_socket_and_public_special_files_are_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            (ssh / "id_directory").mkdir()
            (ssh / "id_directory.pub").write_text(PUBLIC_KEY)
            os.mkfifo(ssh / "id_fifo")
            (ssh / "id_fifo.pub").write_text(PUBLIC_KEY)
            server = socket.socket(socket.AF_UNIX)
            try:
                server.bind(str(ssh / "id_socket"))
                (ssh / "id_socket.pub").write_text(PUBLIC_KEY)
                (ssh / "id_public_fifo").write_text("private")
                os.mkfifo(ssh / "id_public_fifo.pub")
                result = inventory_local(ssh)
            finally:
                server.close()
            self.assertEqual(result.keys, ())
            reasons = {entry.reason for entry in result.unsafe}
            self.assertIn("non-regular private path", reasons)
            self.assertIn("non-regular public path", reasons)

    def test_wrong_owner_private_and_public_are_ineligible(self) -> None:
        for wrong_name in ("id_owner", "id_owner.pub"):
            with self.subTest(wrong_name=wrong_name), tempfile.TemporaryDirectory() as raw:
                home = self.home(raw)
                ssh = home / ".ssh"
                ssh.mkdir()
                self.pair(ssh, "id_owner")
                real_stat = os.stat

                def wrong_owner(
                    path: str | os.PathLike[str],
                    *,
                    dir_fd: int | None = None,
                    follow_symlinks: bool = True,
                    selected: str = wrong_name,
                    stat_fn: object = real_stat,
                ) -> os.stat_result:
                    item = stat_fn(  # type: ignore[operator]
                        path, dir_fd=dir_fd, follow_symlinks=follow_symlinks
                    )
                    if path == selected:
                        values = list(item)
                        values[4] = item.st_uid + 1
                        return os.stat_result(values)
                    return item

                with patch("ai_setup.config.ssh_inventory.os.stat", wrong_owner):
                    result = inventory_local(ssh)
                self.assertEqual(result.keys, ())
                self.assertTrue(any("wrong-owner" in item.reason for item in result.unsafe))

    def test_hard_linked_and_protected_aliases_are_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            private, public = self.pair(ssh, "id_ed25519_ai_github")
            os.link(private, ssh / "id_alias")
            os.link(public, ssh / "id_alias.pub")
            result = inventory_local(ssh)
            self.assertEqual(result.keys, ())
            self.assertTrue(any("hard-linked" in item.reason for item in result.unsafe))
            self.assertTrue(private.exists())
            self.assertTrue(public.exists())

    def test_dedicated_pair_is_inventoried_but_always_protected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            self.pair(ssh, "id_ed25519_ai_github")
            result = inventory_local(ssh)
            self.assertEqual(len(result.keys), 1)
            self.assertTrue(result.keys[0].protected)
            remote = (RemoteKey(1, "same", FINGERPRINT),)
            self.assertEqual(github_correlated_local_keys(result.keys, remote), ())

    def test_dedicated_generation_and_reuse_keep_parameters_and_safe_modes(self) -> None:
        class KeygenRunner(FakeRunner):
            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                result = super().run(command, check=check)
                if command.argv[0] == "ssh-keygen":
                    private = Path(command.argv[command.argv.index("-f") + 1])
                    private.write_text("generated private fixture")
                    private.with_suffix(".pub").write_text(PUBLIC_KEY)
                return result

        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            runner = KeygenRunner()
            manager = SSHManager(runner, home)  # type: ignore[arg-type]
            self.assertTrue(manager.create("person@example.com"))
            keygen = next(command for command in runner.commands if command.argv[0] == "ssh-keygen")
            self.assertEqual(
                keygen.argv,
                (
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-f",
                    str(manager.key),
                    "-C",
                    "person@example.com",
                    "-N",
                    "",
                ),
            )
            self.assertEqual(manager.key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(manager.key.with_suffix(".pub").stat().st_mode & 0o777, 0o644)
            before = len(
                [command for command in runner.commands if command.argv[0] == "ssh-keygen"]
            )
            self.assertFalse(manager.create("person@example.com"))
            after = len([command for command in runner.commands if command.argv[0] == "ssh-keygen"])
            self.assertEqual(before, after)

    def test_dedicated_public_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            (ssh / "id_ed25519_ai_github").write_text("private")
            outside = Path(raw) / "outside.pub"
            outside.write_text(PUBLIC_KEY)
            (ssh / "id_ed25519_ai_github.pub").symlink_to(outside)
            with self.assertRaisesRegex(ValidationError, "symbolic"):
                SSHManager(FakeRunner(), home).create("person@example.com")  # type: ignore[arg-type]
            self.assertEqual(outside.read_text(), PUBLIC_KEY)

    def test_fingerprint_matches_known_ssh_keygen_fixture(self) -> None:
        self.assertEqual(fingerprint_text(PUBLIC_KEY), FINGERPRINT)

    def test_malformed_type_mismatch_and_multiple_records_are_rejected(self) -> None:
        malformed = "ssh-ed25519 not-base64 fixture\n"
        mismatch = PUBLIC_KEY.replace("ssh-ed25519 ", "ssh-rsa ", 1)
        multiple = PUBLIC_KEY + PUBLIC_KEY
        key_type = b"ssh-ed25519"
        truncated = (
            "ssh-ed25519 " + base64.b64encode(len(key_type).to_bytes(4, "big") + key_type).decode()
        )
        for value in (malformed, mismatch, multiple, truncated):
            with self.subTest(value=value[:20]):
                self.assertIsNone(fingerprint_text(value))

    def test_private_bytes_are_never_opened_during_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            self.pair(ssh, "id_private")
            real_open = os.open

            def guarded_open(
                path: str | os.PathLike[str],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if path == "id_private":
                    raise AssertionError("private key content was opened")
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("ai_setup.config.ssh_inventory.os.open", guarded_open):
                result = inventory_local(ssh)
            self.assertEqual(len(result.keys), 1)

    def test_exact_fingerprint_not_comment_title_or_filename_controls_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            self.pair(ssh, "github-email@example")
            key = inventory_local(ssh).keys[0]
            mismatches = (
                RemoteKey(1, key.private_name, "SHA256:different"),
                RemoteKey(2, "fixture@example", None),
            )
            self.assertEqual(github_correlated_local_keys((key,), mismatches), ())
            self.assertEqual(
                github_correlated_local_keys(
                    (key,), (RemoteKey(3, "unrelated title", key.fingerprint),)
                ),
                (key,),
            )

    def test_snapshot_contains_root_and_complete_entry_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            self.pair(ssh, "id_snapshot")
            key = inventory_local(ssh).keys[0]
            self.assertGreater(key.root.device, 0)
            self.assertGreater(key.root.inode, 0)
            for item in (key.private, key.public):
                self.assertTrue(stat.S_ISREG(item.mode))
                self.assertEqual(item.owner_uid, os.getuid())
                self.assertEqual(item.link_count, 1)
                self.assertGreater(item.inode, 0)

    def test_batch_revalidation_aborts_all_deletion_on_entry_changes(self) -> None:
        mutations = ("private_symlink", "public_symlink", "private_inode", "public_content")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                home = self.home(raw)
                ssh = home / ".ssh"
                ssh.mkdir()
                first = self.pair(ssh, "id_first")
                second = self.pair(ssh, "id_second")
                manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
                keys = manager.inventory().keys
                outside = Path(raw) / "outside"
                outside.write_text("preserve")
                target = first[0] if mutation.startswith("private") else first[1]
                if mutation.endswith("symlink"):
                    target.unlink()
                    target.symlink_to(outside)
                elif mutation == "private_inode":
                    target.unlink()
                    target.write_text("replacement")
                else:
                    target.write_text(PUBLIC_KEY.replace("fixture", "changed"))
                with self.assertRaisesRegex(ValidationError, "changed after inventory"):
                    manager.delete(keys, explicit_confirmation=True)
                self.assertTrue(second[0].exists())
                self.assertTrue(second[1].exists())
                self.assertEqual(outside.read_text(), "preserve")

    def test_type_mode_link_count_and_mocked_owner_changes_abort_batch(self) -> None:
        mutations = ("type", "mode", "link_count", "owner")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                home = self.home(raw)
                ssh = home / ".ssh"
                ssh.mkdir()
                private, public = self.pair(ssh, "id_change")
                manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
                keys = manager.inventory().keys
                if mutation == "type":
                    private.unlink()
                    private.mkdir()
                elif mutation == "mode":
                    private.chmod(0o640)
                elif mutation == "link_count":
                    os.link(private, ssh / "id_alias")
                else:
                    real_stat = os.stat

                    def wrong_owner(
                        path: str | os.PathLike[str],
                        *,
                        dir_fd: int | None = None,
                        follow_symlinks: bool = True,
                        selected: str = private.name,
                        stat_fn: object = real_stat,
                    ) -> os.stat_result:
                        item = stat_fn(  # type: ignore[operator]
                            path, dir_fd=dir_fd, follow_symlinks=follow_symlinks
                        )
                        if path == selected:
                            values = list(item)
                            values[4] = item.st_uid + 1
                            return os.stat_result(values)
                        return item

                context = (
                    patch("ai_setup.config.ssh_inventory.os.stat", wrong_owner)
                    if mutation == "owner"
                    else patch("ai_setup.config.ssh_inventory.os.stat", wraps=os.stat)
                )
                with context, self.assertRaisesRegex(ValidationError, "changed after inventory"):
                    manager.delete(keys, explicit_confirmation=True)
                self.assertTrue(public.exists())

    def test_root_replacement_aborts_complete_batch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            self.pair(ssh, "id_old")
            manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
            keys = manager.inventory().keys
            ssh.rename(home / ".ssh-old")
            ssh.mkdir()
            self.pair(ssh, "id_old")
            with self.assertRaisesRegex(ValidationError, "SSH root changed"):
                manager.delete(keys, explicit_confirmation=True)
            self.assertTrue((ssh / "id_old").exists())

    def test_exact_names_are_unlinked_relative_to_validated_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            private, public = self.pair(ssh, "id_delete")
            manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
            keys = manager.inventory().keys
            calls: list[tuple[str, int | None]] = []
            real_unlink = os.unlink

            def recording_unlink(
                path: str | os.PathLike[str], *, dir_fd: int | None = None
            ) -> None:
                calls.append((os.fspath(path), dir_fd))
                real_unlink(path, dir_fd=dir_fd)

            with patch("ai_setup.config.ssh.os.unlink", recording_unlink):
                manager.delete(keys, explicit_confirmation=True)
            self.assertEqual([name for name, _ in calls], ["id_delete.pub", "id_delete"])
            self.assertTrue(all(fd is not None for _, fd in calls))
            self.assertFalse(private.exists())
            self.assertFalse(public.exists())

    def test_partial_unlink_failure_reports_removed_and_remaining_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            private, public = self.pair(ssh, "id_partial")
            manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
            keys = manager.inventory().keys
            real_unlink = os.unlink

            def fail_private(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
                if path == private.name:
                    raise OSError("injected unlink failure")
                real_unlink(path, dir_fd=dir_fd)

            with (
                patch("ai_setup.config.ssh.os.unlink", fail_private),
                self.assertRaisesRegex(
                    ValidationError, "removed \\['id_partial.pub'\\].*remaining"
                ),
            ):
                manager.delete(keys, explicit_confirmation=True)
            self.assertFalse(public.exists())
            self.assertTrue(private.exists())

    def test_dry_run_and_missing_confirmation_never_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            private, public = self.pair(ssh, "id_keep")
            manager = SSHManager(FakeRunner(dry_run=True), home)  # type: ignore[arg-type]
            keys = manager.inventory().keys
            manager.delete(keys, explicit_confirmation=True)
            self.assertTrue(private.exists() and public.exists())
            with self.assertRaises(PermissionError):
                manager.delete(keys, explicit_confirmation=False)
            self.assertTrue(private.exists() and public.exists())


if __name__ == "__main__":
    unittest.main()
