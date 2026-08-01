from __future__ import annotations

import gzip
import hashlib
import io
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.build_installer import (
    DIGEST_TOKEN,
    GITHUB_RELEASE_BASE,
    INSTALLER_SECURITY_CONTRACT,
    VERSION_TOKEN,
    build_installer,
    render_installer,
    validate_installer,
    validate_network_checksum_policy,
    validate_template,
)
from tools.build_release import (
    build,
    ensure_clean,
    project_version,
    validate_release_contract,
    validate_release_notes,
)
from tools.build_site import build_site
from tools.validate_release import validate_archive


def installer_fixture_template() -> str:
    security = "\n".join(INSTALLER_SECURITY_CONTRACT)
    return (
        f'#!/bin/sh\nreadonly AI_WORKSTATION_VERSION="{VERSION_TOKEN}"\n'
        f'{GITHUB_RELEASE_BASE}\nreadonly EXPECTED_SHA256="{DIGEST_TOKEN}"\n'
        f"{security}\nPYTHONDONTWRITEBYTECODE=1\nPYTHONDONTWRITEBYTECODE=1\n"
    )


class ReleaseToolTests(unittest.TestCase):
    def test_version_declarations_agree(self) -> None:
        self.assertEqual(project_version(Path.cwd()), "2.1.0")
        self.assertEqual(validate_release_contract(Path.cwd(), "v2.1.0"), "2.1.0")

    def test_installer_security_markers_are_release_contract(self) -> None:
        template = Path("bootstrap/install.in").read_text(encoding="utf-8")
        for declaration in INSTALLER_SECURITY_CONTRACT:
            with self.subTest(declaration=declaration):
                self.assertEqual(template.count(declaration), 1)

    def test_project_name_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "other"\nversion = "2.1.0"\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "project.name"):
                project_version(root)

    def test_build_is_reproducible_and_independently_validated(self) -> None:
        root = Path.cwd()
        with (
            tempfile.TemporaryDirectory() as first_raw,
            tempfile.TemporaryDirectory() as second_raw,
        ):
            first_dir = Path(first_raw)
            second_dir = Path(second_raw)
            first, first_digest = build(root, first_dir, "v2.1.0", allow_dirty=True)
            second, second_digest = build(root, second_dir, "v2.1.0", allow_dirty=True)
            self.assertEqual(first_digest, second_digest)
            for name in ("ai-2.1.0.tar.gz", "SHA256SUMS", "install"):
                self.assertEqual((first_dir / name).read_bytes(), (second_dir / name).read_bytes())
            validated = validate_archive(
                root,
                first,
                first.parent / "SHA256SUMS",
                first.parent / "install",
                run_runtime=False,
            )
            self.assertEqual(validated, first_digest)
            self.assertEqual((first_dir / "install").stat().st_mode & 0o777, 0o755)
            with tarfile.open(first, "r:gz") as bundle:
                names = {member.name for member in bundle.getmembers()}
            self.assertFalse(any(name.startswith("ai-2.1.0/tests/") for name in names))
            self.assertFalse(any(name.startswith("ai-2.1.0/.github/") for name in names))
            self.assertFalse(any(name.endswith("/py.typed") for name in names))

    def test_sha256sums_covers_archive_and_installer_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            archive, archive_digest = build(Path.cwd(), output, "v2.1.0", allow_dirty=True)
            installer_digest = hashlib.sha256((output / "install").read_bytes()).hexdigest()
            self.assertEqual(
                (output / "SHA256SUMS").read_text(encoding="ascii"),
                f"{archive_digest}  {archive.name}\n{installer_digest}  install\n",
            )
            self.assertEqual(
                {path.name for path in output.iterdir()}, {archive.name, "install", "SHA256SUMS"}
            )

    def test_installer_rendering_is_deterministic_and_recomputes_archive_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "ai-9.8.7.tar.gz"
            archive.write_bytes(b"actual archive bytes")
            template = root / "install.in"
            template.write_text(installer_fixture_template(), encoding="utf-8")
            first = root / "first"
            second = root / "second"
            first_digest = build_installer(template, "9.8.7", archive, first)
            second_digest = build_installer(template, "9.8.7", archive, second)
            expected_archive = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertIn(f'EXPECTED_SHA256="{expected_archive}"', first.read_text())
            self.assertEqual(first.stat().st_mode & 0o777, 0o755)

    def test_installer_template_rejects_missing_duplicate_and_unresolved_placeholders(self) -> None:
        digest = "a" * 64
        with self.assertRaisesRegex(ValueError, "version placeholder"):
            validate_template(DIGEST_TOKEN)
        with self.assertRaisesRegex(ValueError, "checksum placeholder"):
            validate_template(VERSION_TOKEN + DIGEST_TOKEN + DIGEST_TOKEN)
        with self.assertRaisesRegex(ValueError, "unresolved placeholders"):
            validate_template(VERSION_TOKEN + DIGEST_TOKEN + "@UNKNOWN@")
        with self.assertRaisesRegex(ValueError, "unresolved placeholder"):
            validate_installer(
                f'readonly AI_WORKSTATION_VERSION="9.8.7"\nreadonly EXPECTED_SHA256="{digest}"\n@UNKNOWN@',
                "9.8.7",
                digest,
            )

    def test_installer_rejects_invalid_digest_and_duplicate_declarations(self) -> None:
        template = installer_fixture_template()
        with self.assertRaisesRegex(ValueError, "64 lowercase"):
            render_installer(template, "9.8.7", "A" * 64)
        rendered = render_installer(template, "9.8.7", "a" * 64)
        with self.assertRaisesRegex(ValueError, "one checksum declaration"):
            validate_installer(
                rendered + 'readonly EXPECTED_SHA256="' + "a" * 64 + '"\n', "9.8.7", "a" * 64
            )

    def test_installer_rejects_wrong_archive_filename(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "wrong.tar.gz"
            archive.write_bytes(b"archive")
            template = root / "install.in"
            template.write_text(VERSION_TOKEN + DIGEST_TOKEN, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be named"):
                build_installer(template, "9.8.7", archive, root / "install")

    def test_published_installer_has_literal_digest_and_no_checksum_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            archive, digest = build(Path.cwd(), output, "v2.1.0", allow_dirty=True)
            installer = (output / "install").read_text(encoding="utf-8")
            self.assertEqual(installer.count('readonly AI_WORKSTATION_VERSION="2.1.0"'), 1)
            self.assertEqual(installer.count(f'readonly EXPECTED_SHA256="{digest}"'), 1)
            self.assertNotIn("AI_WORKSTATION_RELEASE_SHA256", installer)
            self.assertNotIn(f"{archive.name}.sha256", installer)
            self.assertNotRegex(installer, r"curl[^\n]*\.sha256")
            self.assertEqual(installer.count(GITHUB_RELEASE_BASE), 1)
            self.assertNotIn("https://ai.luigiverona.dev", installer)
            self.assertNotIn("latest/download", installer)
            self.assertNotIn("releases/latest", installer)
            self.assertNotIn("api.github.com", installer)
            self.assertIn(
                'release_url="${RELEASE_BASE}/v${AI_WORKSTATION_VERSION}/ai-${AI_WORKSTATION_VERSION}.tar.gz"',
                installer,
            )
            for forbidden in (
                "https://ai.luigiverona.dev/releases",
                "https://example.test/releases/v",
                "https://github.com/luigiverona/ai/releases/latest",
                "https://github.com/luigiverona/ai/latest/download",
                "https://api.github.com/repos/luigiverona/ai/releases",
            ):
                with (
                    self.subTest(forbidden=forbidden),
                    self.assertRaisesRegex(
                        ValueError, "GitHub Release base|forbidden release source"
                    ),
                ):
                    validate_installer(
                        installer.replace(GITHUB_RELEASE_BASE, forbidden), "2.1.0", digest
                    )

    def test_public_installer_has_no_production_test_controls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            build(Path.cwd(), output, "v2.1.0", allow_dirty=True)
            installer = (output / "install").read_text(encoding="utf-8")
            for forbidden in (
                "AI_WORKSTATION_INSTALLER_TESTING",
                "AI_WORKSTATION_TEST_SENTINEL",
                "AI_WORKSTATION_TEST_FAILURE_POINT",
                "AI_WORKSTATION_TEST_PAUSE_POINT",
                "AI_WORKSTATION_TEST_WRONG_OWNER_JOURNAL",
                "AI_WORKSTATION_RELEASE_BASE",
                "AI_WORKSTATION_CATALOG_ROOT",
                "failure_point",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, installer)

    def test_network_checksum_policy_allows_local_hashing_and_release_assets(self) -> None:
        for content in (
            "digest = hashlib.sha256(data).hexdigest()\n",
            "digest = local_hasher.sha256().hexdigest()\n",
            'printf \'%s  %s\\n\' "$EXPECTED_SHA256" "$archive" | sha256sum -c -\n',
            "assets=(SHA256SUMS)\n",
            'archive_url="https://example.test/ai-2.1.0.tar.gz"\ncurl "$archive_url"\n',
        ):
            with self.subTest(content=content):
                validate_network_checksum_policy(content)

    def test_network_checksum_policy_rejects_remote_sidecars(self) -> None:
        fixtures = (
            "curl https://example.test/SHA256SUMS\n",
            "curl https://github.com/luigiverona/ai/releases/download/v2.1.0/SHA256SUMS\n",
            'archive_url="https://example.test/ai-2.1.0.tar.gz"\n'
            'sidecar_url="${archive_url}.sha256"\n'
            'curl -o checksum "$sidecar_url"\n',
        )
        for content in fixtures:
            with (
                self.subTest(content=content),
                self.assertRaisesRegex(ValueError, "remote checksum metadata"),
            ):
                validate_network_checksum_policy(content)

    def test_site_uses_release_installer_and_contains_only_distribution_surface(self) -> None:
        root = Path.cwd()
        with tempfile.TemporaryDirectory() as assets_raw, tempfile.TemporaryDirectory() as site_raw:
            assets = Path(assets_raw)
            build(root, assets, "v2.1.0", allow_dirty=True)
            site = Path(site_raw) / "site"
            build_site(root, assets, site, "v2.1.0", skip_runtime_validation=True)
            entries = {path.relative_to(site) for path in site.rglob("*")}
            self.assertEqual(entries, {Path("install")})
            self.assertFalse((site / "install").is_symlink())
            self.assertFalse((site / "index.html").exists())
            self.assertFalse((site / "releases").exists())
            self.assertEqual((site / "install").read_bytes(), (assets / "install").read_bytes())
            self.assertNotEqual(
                (site / "install").read_bytes(), (root / "bootstrap/install.in").read_bytes()
            )

    def test_site_rejects_installer_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as assets_raw, tempfile.TemporaryDirectory() as site_raw:
            assets = Path(assets_raw)
            build(Path.cwd(), assets, "v2.1.0", allow_dirty=True)
            installer = assets / "install"
            installer.write_text(
                installer.read_text().replace(
                    'readonly EXPECTED_SHA256="', 'readonly EXPECTED_SHA256="0'
                ),
                encoding="utf-8",
            )
            installer.chmod(0o755)
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                build_site(
                    Path.cwd(),
                    assets,
                    Path(site_raw) / "site",
                    "v2.1.0",
                    skip_runtime_validation=True,
                )

    def test_site_rejects_missing_or_extra_assets(self) -> None:
        for mutation in ("missing", "extra"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as assets_raw,
                tempfile.TemporaryDirectory() as site_raw,
            ):
                assets = Path(assets_raw)
                build(Path.cwd(), assets, "v2.1.0", allow_dirty=True)
                if mutation == "missing":
                    (assets / "install").unlink()
                else:
                    (assets / "unexpected").write_text("extra", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "release assets differ"):
                    build_site(
                        Path.cwd(),
                        assets,
                        Path(site_raw) / "site",
                        "v2.1.0",
                        skip_runtime_validation=True,
                    )

    def test_site_rejects_symlink_installer_output(self) -> None:
        with tempfile.TemporaryDirectory() as assets_raw, tempfile.TemporaryDirectory() as site_raw:
            assets = Path(assets_raw)
            build(Path.cwd(), assets, "v2.1.0", allow_dirty=True)
            original = Path(site_raw) / "original-install"
            (assets / "install").rename(original)
            (assets / "install").symlink_to(original)
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                build_site(
                    Path.cwd(),
                    assets,
                    Path(site_raw) / "site",
                    "v2.1.0",
                    skip_runtime_validation=True,
                )

    def test_site_rejects_invalid_archive_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as assets_raw, tempfile.TemporaryDirectory() as site_raw:
            assets = Path(assets_raw)
            build(Path.cwd(), assets, "v2.1.0", allow_dirty=True)
            sums = assets / "SHA256SUMS"
            lines = sums.read_text(encoding="ascii").splitlines()
            sums.write_text("0" * 64 + "  ai-2.1.0.tar.gz\n" + lines[1] + "\n", encoding="ascii")
            with self.assertRaises(subprocess.CalledProcessError):
                build_site(
                    Path.cwd(),
                    assets,
                    Path(site_raw) / "site",
                    "v2.1.0",
                    skip_runtime_validation=True,
                )

    def test_release_notes_preflight_rejects_missing_empty_and_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "missing"):
                validate_release_notes(root, "9.8.7")
            notes = root / "docs/releases/v9.8.7.md"
            notes.parent.mkdir(parents=True)
            notes.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty"):
                validate_release_notes(root, "9.8.7")
            notes.write_text("# ai 8.7.6\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "heading"):
                validate_release_notes(root, "9.8.7")

    def test_workflow_contract_uses_three_assets_and_main_dispatch(self) -> None:
        release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
        pages = Path(".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn("dist/install", release)
        self.assertIn("-eq 3", release)
        self.assertIn("release-assets/install site/install", pages)
        self.assertIn("-eq 3", pages)
        self.assertNotIn("types: [published]", pages)
        self.assertIn("workflow_dispatch:", pages)

    def test_all_external_github_actions_are_pinned_to_full_commits(self) -> None:
        expected = {
            "actions/attest-build-provenance": {"0f67c3f4856b2e3261c31976d6725780e5e4c373"},
            "actions/checkout": {"3d3c42e5aac5ba805825da76410c181273ba90b1"},
            "actions/configure-pages": {"45bfe0192ca1faeb007ade9deae92b16b8254a0d"},
            "actions/deploy-pages": {"cd2ce8fcbc39b97be8ca5fce6e763baed58fa128"},
            "actions/download-artifact": {"3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"},
            "actions/setup-python": {"5fda3b95a4ea91299a34e894583c3862153e4b97"},
            "actions/upload-artifact": {"043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"},
            "actions/upload-pages-artifact": {"fc324d3547104276b827a68afc52ff2a11cc49c9"},
        }
        actual: dict[str, set[str]] = {}
        pattern = re.compile(r"^\s*(?:-\s*)?uses:\s*([^@\s]+)@([^\s#]+)", re.MULTILINE)
        for workflow in sorted(Path(".github/workflows").glob("*.yml")):
            for action, reference in pattern.findall(workflow.read_text(encoding="utf-8")):
                if action.startswith("./") or action.startswith("docker://"):
                    continue
                self.assertRegex(reference, r"\A[0-9a-f]{40}\Z", workflow.as_posix())
                actual.setdefault(action, set()).add(reference)
        self.assertEqual(actual, expected)

    def test_builder_rejects_dirty_actual_release(self) -> None:
        completed = type("Result", (), {"stdout": " M README.md\n"})()
        with patch("tools.build_release.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "dirty"):
                ensure_clean(Path.cwd())

    def test_validator_rejects_links(self) -> None:
        root = Path.cwd()
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            archive = directory / "ai-2.1.0.tar.gz"
            payload = io.BytesIO()
            epoch = int(
                subprocess.run(
                    ["git", "show", "-s", "--format=%ct", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            with tarfile.open(fileobj=payload, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                link = tarfile.TarInfo("ai-2.1.0/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                link.mtime = epoch
                link.mode = 0o644
                bundle.addfile(link)
            with archive.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as bundle:
                    bundle.write(payload.getvalue())
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            template = directory / "install.in"
            template.write_text(installer_fixture_template(), encoding="utf-8")
            build_installer(template, "2.1.0", archive, directory / "install")
            installer_digest = hashlib.sha256((directory / "install").read_bytes()).hexdigest()
            sums = directory / "SHA256SUMS"
            sums.write_text(
                f"{digest}  {archive.name}\n{installer_digest}  install\n", encoding="ascii"
            )
            with self.assertRaisesRegex(ValueError, "links and special files"):
                validate_archive(
                    root,
                    archive,
                    sums,
                    directory / "install",
                    run_runtime=False,
                )
