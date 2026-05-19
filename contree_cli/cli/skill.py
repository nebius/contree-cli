"""Install, remove, or upgrade ConTree agent skills.

contree skill install              # autodetect agent homes
contree skill install claude:~     # global ~/.claude
contree skill install codex:       # project-level .codex
contree skill install ./path       # raw path, class guessed
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from contree_cli import FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.skill import (
    SKILL_NAME,
    Skill,
    default_install_specs,
    forget_installed,
    list_installed,
    remember_installed,
    skill_from_spec,
    skill_version,
)
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)


def display_path(path: Path) -> str:
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def emit(action: str, skill: Skill) -> None:
    try:
        FORMATTER.get()(
            action=action,
            name=SKILL_NAME,
            kind=skill.kind,
            path=display_path(skill.path),
        )
    except LookupError:
        return


@dataclass(frozen=True)
class SkillInstallArgs(ArgumentsProtocol):
    specs: frozenset[Skill] = frozenset()
    force: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> SkillInstallArgs:
        return cls(specs=frozenset(ns.specs or []), force=ns.force)


@dataclass(frozen=True)
class SkillRemoveArgs(ArgumentsProtocol):
    specs: frozenset[Skill] = frozenset()
    force: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> SkillRemoveArgs:
        return cls(specs=frozenset(ns.specs or []), force=ns.force)


@dataclass(frozen=True)
class SkillUpgradeArgs(ArgumentsProtocol):
    specs: frozenset[Skill] = frozenset()

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> SkillUpgradeArgs:
        return cls(specs=frozenset(ns.specs or []))


@dataclass(frozen=True)
class SkillListArgs(ArgumentsProtocol):
    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> SkillListArgs:
        return cls()


# ── Parser ───────────────────────────────────────────────


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    sub = p.add_subparsers(dest="skill_action", required=True)

    def spec_arg(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "specs",
            nargs="*",
            metavar="SPEC",
            type=skill_from_spec,
            help="claude:~ codex:~ or raw path",
        )

    list_p = sub.add_parser(
        "list", aliases=["ls"], help="List remembered skill installs"
    )
    list_p.set_defaults(handler=cmd_skill_list, load_args=SkillListArgs)

    install_p = sub.add_parser(
        "install", aliases=["i"], help="Install ConTree skill files"
    )
    install_p.add_argument(
        *FLAGS["force"], action="store_true", help="Overwrite existing"
    )
    spec_arg(install_p)
    install_p.set_defaults(handler=cmd_skill_install, load_args=SkillInstallArgs)

    remove_p = sub.add_parser(
        "remove", aliases=["r", "rm", "del"], help="Remove installed skill files"
    )
    remove_p.add_argument(
        *FLAGS["force"], action="store_true", help="Do not ask for confirmation"
    )
    spec_arg(remove_p)
    remove_p.set_defaults(handler=cmd_skill_remove, load_args=SkillRemoveArgs)

    upgrade_p = sub.add_parser(
        "upgrade", aliases=["u", "update"], help="Upgrade installed skill files"
    )
    upgrade_p.set_defaults(handler=cmd_skill_upgrade, load_args=SkillUpgradeArgs)
    spec_arg(upgrade_p)

    return cmd_skill_install, SkillInstallArgs


def cmd_skill_list(_: SkillListArgs) -> int | None:
    formatter = FORMATTER.get()
    current = skill_version()
    for skill in sorted(list_installed(), key=lambda s: str(s.path)):
        formatter(
            name=SKILL_NAME,
            path=display_path(skill.path),
            kind=skill.kind,
            version=skill.installed_version or "?",
            latest=current,
            outdated=skill.needs_upgrade,
            exists=skill.exists,
        )
    return None


def cmd_skill_install(args: SkillInstallArgs) -> int | None:
    targets = args.specs or frozenset(default_install_specs())
    if not targets:
        logger.error("No agent tool homes detected; pass SPEC explicitly")
        return 1

    for skill in targets:
        try:
            skill.install(force=args.force)
        except FileExistsError:
            logger.warning(
                "Already installed at %s; use upgrade or --force",
                display_path(skill.path),
            )
            continue
        remember_installed(skill)
        logger.info(
            "Installed %s (%s) to %s", SKILL_NAME, skill.kind, display_path(skill.path)
        )
        emit("install", skill)
    return None


def cmd_skill_remove(args: SkillRemoveArgs) -> int | None:
    targets = args.specs or list_installed()
    if not targets:
        logger.error("No remembered installs to remove")
        return 1

    failed = False
    for skill in targets:
        if not skill.exists:
            logger.error("Not installed at %s", display_path(skill.path))
            failed = True
            continue

        if not args.force:
            answer = input(
                f"Remove {SKILL_NAME!r} from {display_path(skill.path)}? [y/N] "
            )
            if answer.lower() != "y":
                print("Aborted.")
                failed = True
                continue

        skill.remove()
        forget_installed(skill)
        logger.info(
            "Removed %s (%s) from %s", SKILL_NAME, skill.kind, display_path(skill.path)
        )
        emit("remove", skill)
    return 1 if failed else None


def cmd_skill_upgrade(args: SkillUpgradeArgs) -> int | None:
    targets = args.specs or list_installed()
    if not targets:
        logger.error("No remembered installs; run `contree skill install` first")
        return 1

    failed = False
    for skill in targets:
        if not skill.exists:
            logger.error(
                "Not installed at %s; run `contree skill install` first",
                display_path(skill.path),
            )
            failed = True
            continue

        skill.install(force=True)
        remember_installed(skill)
        logger.info(
            "Upgraded %s (%s) at %s", SKILL_NAME, skill.kind, display_path(skill.path)
        )
        emit("upgrade", skill)
    return 1 if failed else None
