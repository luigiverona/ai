from __future__ import annotations

import ast
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_setup.config.files import (
    ExistingFilePolicy,
    ensure_managed_directory,
    inspect_managed_file,
    replace_managed_file,
)
from ai_setup.errors import ValidationError


class ManagedFileTests(unittest.TestCase):
    def home(self, raw: str) -> Path:
        home = Path(raw) / "home"
        home.mkdir(mode=0o700)
        return home

    def replace(
        self,
        home: Path,
        target: Path,
        content: str = "managed\n",
        *,
        mode: int = 0o600,
        policy: ExistingFilePolicy = ExistingFilePolicy.USER_OWNED,
    ) -> bool:
        snapshot = inspect_managed_file(trusted_root=home, target=target, owner_uid=os.getuid())
        return replace_managed_file(
            trusted_root=home,
            target=target,
            content=content,
            mode=mode,
            owner_uid=os.getuid(),
            expected=snapshot,
            existing_policy=policy,
        )

    def test_containment_root_and_parent_traversal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            outside = Path(raw) / "outside"
            for target in (home, outside / "file", home / ".." / "outside"):
                with self.subTest(target=target), self.assertRaises(ValidationError):
                    self.replace(home, target)

    def test_target_and_every_existing_ancestor_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            outside = Path(raw) / "outside"
            outside.mkdir()
            target = home / ".config/fish/conf.d/ai.fish"
            for relative in (".config", ".config/fish", ".config/fish/conf.d"):
                with self.subTest(relative=relative):
                    root = home / relative
                    root.parent.mkdir(parents=True, exist_ok=True)
                    root.symlink_to(outside, target_is_directory=True)
                    with self.assertRaises(ValidationError):
                        self.replace(home, target)
                    root.unlink()
            target.parent.mkdir(parents=True)
            unrelated = outside / "file"
            unrelated.write_text("keep\n")
            target.symlink_to(unrelated)
            with self.assertRaises(ValidationError):
                self.replace(home, target)
            self.assertEqual(unrelated.read_text(), "keep\n")

    def test_non_directory_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            (home / ".config").write_text("not a directory\n")
            with self.assertRaisesRegex(ValidationError, "not a directory"):
                self.replace(home, home / ".config/fish/conf.d/ai.fish")

    def test_wrong_owner_root_ancestor_and_target_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed/file"
            target.parent.mkdir()
            target.write_text("old\n")
            real_lstat = Path.lstat
            real_stat = os.stat

            def wrong_root(path: Path) -> os.stat_result:
                item = real_lstat(path)
                values = list(item)
                values[4] = item.st_uid + 1
                return os.stat_result(values)

            with (
                patch.object(Path, "lstat", wrong_root),
                self.assertRaisesRegex(ValidationError, "not owned"),
            ):
                self.replace(home, target)

            def wrong_target(
                path: str,
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                item = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
                if path == "file":
                    values = list(item)
                    values[4] = item.st_uid + 1
                    return os.stat_result(values)
                return item

            with (
                patch("ai_setup.config.files.os.stat", wrong_target),
                self.assertRaisesRegex(ValidationError, "not owned"),
            ):
                self.replace(home, target)

    def test_directories_are_created_one_component_at_a_time_and_fsynced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            calls: list[int] = []
            real_fsync = os.fsync

            def recording_fsync(fd: int) -> None:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    calls.append(fd)
                real_fsync(fd)

            with patch("ai_setup.config.files.os.fsync", recording_fsync):
                ensure_managed_directory(
                    trusted_root=home,
                    directory=home / "one/two/three",
                    mode=0o700,
                    owner_uid=os.getuid(),
                )
            self.assertTrue((home / "one/two/three").is_dir())
            self.assertGreaterEqual(len(calls), 3)
            self.assertEqual((home / "one/two/three").stat().st_mode & 0o777, 0o700)

    def test_temporary_file_is_exclusive_same_directory_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed/file"
            self.replace(home, target)
            self.assertEqual(target.read_text(), "managed\n")
            self.assertEqual(list(target.parent.glob(".file.ai-*")), [])

    def test_temporary_creation_uses_exclusive_nofollow_open_in_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed"
            observed: list[tuple[str | os.PathLike[str], int, int | None]] = []
            real_open = os.open

            def recording_open(
                path: str | os.PathLike[str],
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if isinstance(path, str) and path.startswith(".managed.ai-"):
                    observed.append((path, flags, dir_fd))
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("ai_setup.config.files.os.open", recording_open):
                self.replace(home, target)
            self.assertEqual(len(observed), 1)
            _, flags, dir_fd = observed[0]
            self.assertTrue(flags & os.O_EXCL)
            self.assertTrue(flags & getattr(os, "O_NOFOLLOW", 0))
            self.assertIsNotNone(dir_fd)

    def test_file_is_fsynced_before_replace_and_parent_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed"
            events: list[str] = []
            real_fsync = os.fsync
            real_replace = os.replace

            def recording_fsync(fd: int) -> None:
                events.append("dir-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")
                real_fsync(fd)

            def recording_replace(*args: object, **kwargs: object) -> None:
                events.append("replace")
                real_replace(*args, **kwargs)

            with (
                patch("ai_setup.config.files.os.fsync", recording_fsync),
                patch("ai_setup.config.files.os.replace", recording_replace),
            ):
                self.replace(home, target)
            self.assertLess(events.index("file-fsync"), events.index("replace"))
            self.assertLess(events.index("replace"), events.index("dir-fsync"))

    def test_correct_file_is_unchanged_and_wrong_mode_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed"
            self.assertTrue(self.replace(home, target))
            inode = target.stat().st_ino
            self.assertFalse(self.replace(home, target))
            self.assertEqual(target.stat().st_ino, inode)
            target.chmod(0o644)
            self.assertTrue(self.replace(home, target))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_unrecognized_dedicated_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed"
            target.write_text("unrelated\n")
            before = (target.read_bytes(), target.stat().st_mode, target.stat().st_ino)
            with self.assertRaisesRegex(
                ValidationError,
                "not recognized as managed by ai; it was left unchanged; inspect the path",
            ):
                self.replace(
                    home,
                    target,
                    policy=ExistingFilePolicy.EXACT_CONTENT,
                )
            self.assertEqual(
                (target.read_bytes(), target.stat().st_mode, target.stat().st_ino),
                before,
            )

    def test_write_file_fsync_and_replace_failures_preserve_original(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed"
            target.write_text("original\n")
            target.chmod(0o600)
            cases = (
                ("os.write", OSError("write failed")),
                ("os.fsync", OSError("file fsync failed")),
                ("os.replace", OSError("replace failed")),
            )
            for name, error in cases:
                with self.subTest(name=name):
                    patch_name = f"ai_setup.config.files.{name}"
                    with patch(patch_name, side_effect=error), self.assertRaises(ValidationError):
                        self.replace(home, target, "replacement\n")
                    self.assertEqual(target.read_text(), "original\n")
                    self.assertEqual(list(home.glob(".managed.ai-*")), [])

    def test_directory_fsync_failure_reports_visible_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed"
            real_fsync = os.fsync

            def fail_directory(fd: int) -> None:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("directory fsync failed")
                real_fsync(fd)

            with (
                patch("ai_setup.config.files.os.fsync", fail_directory),
                self.assertRaisesRegex(ValidationError, "replacement is visible and verified"),
            ):
                self.replace(home, target, "replacement\n")
            self.assertEqual(target.read_text(), "replacement\n")

    def test_final_verification_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            target = home / "managed"
            real_snapshot = __import__("ai_setup.config.files", fromlist=["_snapshot"])._snapshot
            calls = 0

            def corrupt_final(*args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                result = real_snapshot(*args, **kwargs)
                if calls == 4 and result is not None:
                    return type(result)(
                        b"wrong", result.mode, result.device, result.inode, result.modified_ns
                    )
                return result

            with (
                patch("ai_setup.config.files._snapshot", corrupt_final),
                self.assertRaisesRegex(ValidationError, "final type"),
            ):
                self.replace(home, target)

    def test_wrong_owner_existing_ancestor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = self.home(raw)
            parent = home / "managed"
            parent.mkdir()
            target = parent / "file"
            real_stat = os.stat

            def wrong_ancestor(
                path: str,
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                item = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
                if path == "managed":
                    values = list(item)
                    values[4] = item.st_uid + 1
                    return os.stat_result(values)
                return item

            with (
                patch("ai_setup.config.files.os.stat", wrong_ancestor),
                self.assertRaisesRegex(ValidationError, "not owned"),
            ):
                self.replace(home, target)

    def test_production_managed_writes_do_not_bypass_the_primitive(self) -> None:
        root = Path(__file__).parents[1] / "src/ai_setup"
        allowed = {
            root / "config/files.py",
            root / "execution/runner.py",
            root / "packages/managers.py",
        }
        violations: list[str] = []
        for path in root.rglob("*.py"):
            if path in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in {"write_text", "write_bytes", "replace", "rename"}:
                        violations.append(f"{path.relative_to(root)}:{node.lineno}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
