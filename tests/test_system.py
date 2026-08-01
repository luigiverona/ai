from __future__ import annotations

import unittest
from unittest.mock import patch

from ai_setup.system import ARCH_REQUIRED, is_arch_linux_x86_64


class SystemIdentityTests(unittest.TestCase):
    def test_exact_arch_linux_x86_64_identity_is_accepted(self) -> None:
        with (
            patch("ai_setup.system.platform.system", return_value="Linux"),
            patch("ai_setup.system.platform.machine", return_value="x86_64"),
        ):
            self.assertTrue(is_arch_linux_x86_64('NAME="Arch Linux"\nID=arch\n'))
            self.assertTrue(is_arch_linux_x86_64('ID="arch"\n'))

    def test_derivative_substring_non_linux_and_other_architecture_are_rejected(self) -> None:
        cases = (
            ("Linux", "x86_64", "ID=archcraft\n"),
            ("Linux", "x86_64", "ID=other\nID_LIKE=arch\n"),
            ("FreeBSD", "x86_64", "ID=arch\n"),
            ("Linux", "aarch64", "ID=arch\n"),
        )
        for system, machine, release in cases:
            with (
                self.subTest(system=system, machine=machine, release=release),
                patch("ai_setup.system.platform.system", return_value=system),
                patch("ai_setup.system.platform.machine", return_value=machine),
            ):
                self.assertFalse(is_arch_linux_x86_64(release))

    def test_arch_failure_diagnostic_is_exact(self) -> None:
        self.assertEqual(ARCH_REQUIRED, "Arch Linux x86-64 is required.")


if __name__ == "__main__":
    unittest.main()
