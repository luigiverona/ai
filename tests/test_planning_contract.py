from __future__ import annotations

import contextlib
import io
import itertools
import unittest
from dataclasses import fields
from unittest.mock import patch

from ai_setup.catalog.loader import load_catalog
from ai_setup.cli import InvocationKind, invocation_from_args, main, parser
from ai_setup.models import (
    Capability,
    Catalog,
    RunOptions,
    Selection,
    Source,
    WorkflowStage,
)
from ai_setup.planning.planner import build_plan
from ai_setup.ui.terminal import Terminal
from ai_setup.workflow import Workflow

PackageIdentity = tuple[Source, str]

APPLICATIONS: dict[str, tuple[PackageIdentity, ...]] = {
    "browser": (
        (Source.AUR, "librewolf-bin"),
        (Source.AUR, "mullvad-browser-bin"),
    ),
    "development": ((Source.UPSTREAM, "codex"),),
    "editor": ((Source.AUR, "visual-studio-code-bin"),),
    "game": ((Source.FLATPAK, "org.vinegarhq.Sober"),),
    "media": ((Source.PACMAN, "spotify-launcher"),),
    "social": ((Source.PACMAN, "discord"),),
    "vpn": ((Source.PACMAN, "mullvad-vpn"),),
}
AUR_REQUIREMENTS = {
    (Source.AUR, "yay-bin"),
    (Source.PACMAN, "base-devel"),
    (Source.PACMAN, "git"),
}
FLATPAK_REQUIREMENTS = {(Source.PACMAN, "flatpak")}
CODEX_REQUIREMENTS = {(Source.PACMAN, "curl")}
CONFIGURATION_PACKAGES: dict[str, set[PackageIdentity]] = {
    "git": set(),
    "github": {
        (Source.PACMAN, "git"),
        (Source.PACMAN, "github-cli"),
        (Source.PACMAN, "openssh"),
    },
    "ssh": {
        (Source.PACMAN, "git"),
        (Source.PACMAN, "github-cli"),
        (Source.PACMAN, "openssh"),
    },
    "codex": {
        (Source.PACMAN, "curl"),
        (Source.UPSTREAM, "codex"),
    },
}
CONFIGURATION_CAPABILITIES = {
    "git": ((Capability.GIT,), ()),
    "github": ((Capability.GITHUB,), (Capability.DEPS, Capability.GIT)),
    "ssh": (
        (Capability.SSH,),
        (Capability.DEPS, Capability.GIT, Capability.GITHUB),
    ),
    "codex": ((Capability.CODEX,), (Capability.DEPS, Capability.SHELL)),
}


def identities(packages: object) -> tuple[PackageIdentity, ...]:
    return tuple((package.source, package.identifier) for package in packages)


def ordered(values: set[PackageIdentity]) -> tuple[PackageIdentity, ...]:
    return tuple(sorted(values, key=lambda value: (value[0].value, value[1])))


class PublicPlanningContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()
        cls.cli_parser = parser(cls.catalog)

    def invocation(self, *values: str):
        return invocation_from_args(self.cli_parser.parse_args(values), self.catalog)

    def plan(self, *values: str):
        invocation = self.invocation(*values)
        if invocation.selection is None:
            raise AssertionError("status has no setup plan")
        return invocation, build_plan(invocation.selection, self.catalog)

    @staticmethod
    def expected_app_plan(
        categories: tuple[str, ...],
    ) -> tuple[
        tuple[PackageIdentity, ...],
        tuple[Capability, ...],
        tuple[WorkflowStage, ...],
    ]:
        application_packages = {
            identity for category in categories for identity in APPLICATIONS[category]
        }
        dependencies: set[PackageIdentity] = set()
        sources = {source for source, _identifier in application_packages}
        prerequisites = {Capability.DEPS}
        stages = {
            WorkflowStage.ADMINISTRATOR,
            WorkflowStage.APPLICATIONS,
            WorkflowStage.VERIFICATION,
        }
        if Source.AUR in sources:
            dependencies.update(AUR_REQUIREMENTS)
        if Source.FLATPAK in sources:
            dependencies.update(FLATPAK_REQUIREMENTS)
            prerequisites.update({Capability.FLATPAK, Capability.FLATHUB})
            stages.add(WorkflowStage.FLATPAK)
        if (Source.UPSTREAM, "codex") in application_packages:
            dependencies.update(CODEX_REQUIREMENTS)
            prerequisites.update({Capability.CODEX, Capability.SHELL})
            stages.update({WorkflowStage.CODEX, WorkflowStage.SHELL})
        capability_order = tuple(
            capability for capability in Capability if capability in prerequisites
        )
        stage_order = tuple(stage for stage in WorkflowStage if stage in stages)
        return ordered(application_packages | dependencies), capability_order, stage_order

    def test_complete_public_invocations_preserve_exact_contract(self) -> None:
        expected_packages = ordered(
            {identity for values in APPLICATIONS.values() for identity in values}
            | {
                (Source.PACMAN, "base-devel"),
                (Source.AUR, "yay-bin"),
                (Source.PACMAN, "flatpak"),
                (Source.PACMAN, "github-cli"),
                (Source.PACMAN, "git"),
                (Source.PACMAN, "curl"),
                (Source.PACMAN, "openssh"),
            }
        )
        expected_stages = tuple(WorkflowStage)
        for values in ((), ("setup",)):
            with self.subTest(values=values):
                invocation, plan = self.plan(*values)
                self.assertEqual(invocation.kind, InvocationKind.SETUP)
                self.assertTrue(invocation.selection.complete)
                self.assertEqual(plan.selected, tuple(Capability))
                self.assertEqual(plan.prerequisites, ())
                self.assertEqual(identities(plan.packages), expected_packages)
                self.assertEqual(
                    len(identities(plan.packages)), len(set(identities(plan.packages)))
                )
                workflow = Workflow(plan, RunOptions(dry_run=True), Terminal())
                self.assertEqual(workflow._selected_stages(plan.packages), expected_stages)

    def test_apps_without_categories_preserves_all_app_contract(self) -> None:
        invocation, plan = self.plan("apps")
        expected_packages, prerequisites, stages = self.expected_app_plan(tuple(APPLICATIONS))
        self.assertFalse(invocation.selection.complete)
        self.assertEqual(invocation.selection.app_categories, frozenset())
        self.assertEqual(plan.selected, (Capability.APPS,))
        self.assertEqual(plan.prerequisites, prerequisites)
        self.assertEqual(identities(plan.packages), expected_packages)
        workflow = Workflow(plan, RunOptions(dry_run=True), Terminal())
        self.assertEqual(workflow._selected_stages(plan.packages), stages)

    def test_every_single_and_pairwise_app_selection_preserves_contract(self) -> None:
        category_sets = [
            *(tuple([category]) for category in APPLICATIONS),
            *itertools.combinations(APPLICATIONS, 2),
        ]
        for categories in category_sets:
            with self.subTest(categories=categories):
                invocation, plan = self.plan("apps", *categories)
                expected_packages, prerequisites, stages = self.expected_app_plan(categories)
                self.assertEqual(
                    invocation.selection.app_categories,
                    frozenset(categories),
                )
                self.assertEqual(plan.selected, (Capability.APPS,))
                self.assertEqual(plan.prerequisites, prerequisites)
                self.assertEqual(identities(plan.packages), expected_packages)
                workflow = Workflow(plan, RunOptions(dry_run=True), Terminal())
                self.assertEqual(workflow._selected_stages(plan.packages), stages)

    def test_category_pair_application_union_and_dependency_rules(self) -> None:
        for left, right in itertools.combinations(APPLICATIONS, 2):
            with self.subTest(left=left, right=right):
                _invocation, combined = self.plan("apps", left, right)
                _left_invocation, left_plan = self.plan("apps", left)
                _right_invocation, right_plan = self.plan("apps", right)
                expected_applications = set(APPLICATIONS[left]) | set(APPLICATIONS[right])
                actual_applications = {
                    identity
                    for identity in identities(combined.packages)
                    if identity in {value for values in APPLICATIONS.values() for value in values}
                }
                self.assertEqual(actual_applications, expected_applications)
                expected_packages, _prerequisites, _stages = self.expected_app_plan((left, right))
                self.assertEqual(identities(combined.packages), expected_packages)
                self.assertEqual(
                    identities(combined.packages),
                    ordered(
                        set(identities(left_plan.packages)) | set(identities(right_plan.packages))
                    ),
                )

    def test_source_specific_dependency_contracts(self) -> None:
        for category in ("browser", "editor"):
            with self.subTest(category=category):
                _invocation, plan = self.plan("apps", category)
                self.assertTrue(AUR_REQUIREMENTS <= set(identities(plan.packages)))
                self.assertFalse(FLATPAK_REQUIREMENTS & set(identities(plan.packages)))
        _invocation, game = self.plan("apps", "game")
        self.assertTrue(FLATPAK_REQUIREMENTS <= set(identities(game.packages)))
        self.assertFalse(AUR_REQUIREMENTS & set(identities(game.packages)))
        for category in ("media", "social", "vpn"):
            with self.subTest(category=category):
                _invocation, plan = self.plan("apps", category)
                self.assertFalse(AUR_REQUIREMENTS & set(identities(plan.packages)))
                self.assertFalse(FLATPAK_REQUIREMENTS & set(identities(plan.packages)))

    def test_focused_configuration_commands_preserve_exact_contracts(self) -> None:
        expected_stages = {
            "git": (WorkflowStage.GIT, WorkflowStage.VERIFICATION),
            "github": (
                WorkflowStage.ADMINISTRATOR,
                WorkflowStage.APPLICATIONS,
                WorkflowStage.GIT,
                WorkflowStage.GITHUB,
                WorkflowStage.VERIFICATION,
            ),
            "ssh": (
                WorkflowStage.ADMINISTRATOR,
                WorkflowStage.APPLICATIONS,
                WorkflowStage.GIT,
                WorkflowStage.GITHUB,
                WorkflowStage.SSH,
                WorkflowStage.VERIFICATION,
            ),
            "codex": (
                WorkflowStage.ADMINISTRATOR,
                WorkflowStage.APPLICATIONS,
                WorkflowStage.CODEX,
                WorkflowStage.SHELL,
                WorkflowStage.VERIFICATION,
            ),
        }
        for command, expected_packages in CONFIGURATION_PACKAGES.items():
            with self.subTest(command=command):
                invocation, plan = self.plan(command)
                selected, prerequisites = CONFIGURATION_CAPABILITIES[command]
                self.assertFalse(invocation.selection.complete)
                self.assertEqual(plan.selected, selected)
                self.assertEqual(plan.prerequisites, prerequisites)
                self.assertEqual(identities(plan.packages), ordered(expected_packages))
                workflow = Workflow(plan, RunOptions(dry_run=True), Terminal())
                self.assertEqual(
                    workflow._selected_stages(plan.packages),
                    expected_stages[command],
                )

    def test_status_remains_planner_independent(self) -> None:
        invocation = self.invocation("status")
        self.assertEqual(invocation.kind, InvocationKind.STATUS)
        self.assertIsNone(invocation.selection)
        with (
            patch("ai_setup.cli.build_plan") as planner,
            patch("ai_setup.cli.StatusWorkflow.run", return_value=0) as status,
        ):
            self.assertEqual(main(["status"]), 0)
        planner.assert_not_called()
        status.assert_called_once()

    def test_unknown_category_stops_before_any_workflow(self) -> None:
        error = io.StringIO()
        with (
            patch("ai_setup.cli.build_plan") as planner,
            patch("ai_setup.cli.Workflow.run") as workflow,
            patch("ai_setup.cli.StatusWorkflow.run") as status,
            contextlib.redirect_stderr(error),
            self.assertRaises(SystemExit) as caught,
        ):
            main(["apps", "unknown-category"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("unknown application category", error.getvalue())
        planner.assert_not_called()
        workflow.assert_not_called()
        status.assert_not_called()

    def test_dependency_categories_are_not_model_or_constructor_state(self) -> None:
        self.assertEqual(
            [field.name for field in fields(Catalog)],
            ["apps", "deps", "app_categories"],
        )
        self.assertEqual(
            [field.name for field in fields(Selection)],
            ["capabilities", "app_categories", "complete"],
        )

    def test_check_capability_remains_absent(self) -> None:
        self.assertNotIn("check", {capability.value for capability in Capability})
