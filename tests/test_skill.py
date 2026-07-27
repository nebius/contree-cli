from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import contree_cli.config as config_mod
from contree_cli.cli.skill import (
    SkillInstallArgs,
    SkillListArgs,
    SkillRemoveArgs,
    SkillUpgradeArgs,
    cmd_skill_install,
    cmd_skill_list,
    cmd_skill_remove,
    cmd_skill_upgrade,
)
from contree_cli.skill import (
    ALL_SKILL_TYPES,
    SKILL_NAME,
    AmpSkill,
    ClaudeAgentSkill,
    ClaudeSkill,
    ClaudeSubagentSkill,
    ClineSkill,
    CodexSkill,
    OpenCodeSkill,
    Skill,
    guess_skill,
    list_installed,
    parse_version,
    skill_from_spec,
    skill_version,
    skills_from_spec,
)


def _spec(dest: Path) -> Skill:
    return skill_from_spec(str(dest))


def _specs(*paths: Path) -> frozenset[Skill]:
    return frozenset(_spec(p) for p in paths)


def _installed(tmp_path: Path) -> Path:
    return tmp_path / "skills" / SKILL_NAME


def _claude_installed(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "skills" / SKILL_NAME


class TestSkillInstall:
    def test_install_creates_skill_tree(
        self, tmp_path: Path, config_dir: Path, caplog
    ) -> None:
        dest = _installed(tmp_path)
        args = SkillInstallArgs(specs=_specs(dest))
        with caplog.at_level("INFO"):
            rc = cmd_skill_install(args)

        assert rc is None
        assert dest.is_dir()
        assert (dest / "SKILL.md").is_file()
        assert (dest / ".version").is_file()
        assert (dest / "agents" / "openai.yaml").is_file()
        assert {s.path for s in list_installed()} == {dest}
        assert "Installed" in caplog.text

    def test_install_refuses_existing_without_force(
        self, tmp_path: Path, config_dir: Path, caplog
    ) -> None:
        dest = _installed(tmp_path)
        args = SkillInstallArgs(specs=_specs(dest))
        assert cmd_skill_install(args) is None

        with caplog.at_level("WARNING"):
            rc = cmd_skill_install(args)
        assert rc is None
        assert "Already installed" in caplog.text

    def test_install_accepts_multiple_specs(
        self, tmp_path: Path, config_dir: Path
    ) -> None:
        dest1 = tmp_path / "codex" / SKILL_NAME
        dest2 = _claude_installed(tmp_path)
        rc = cmd_skill_install(SkillInstallArgs(specs=_specs(dest1, dest2)))
        assert rc is None
        assert (dest1 / "SKILL.md").is_file()
        assert (dest2 / "SKILL.md").is_file()
        assert {s.path for s in list_installed()} == {dest1, dest2}

    def test_install_claude_skill_dir(self, tmp_path: Path, config_dir: Path) -> None:
        dest = _claude_installed(tmp_path)
        rc = cmd_skill_install(SkillInstallArgs(specs=_specs(dest)))
        assert rc is None
        assert dest.is_dir()
        text = (dest / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith('---\nname: "contree"\n')

    def test_install_default_specs(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        agents_home = tmp_path / ".agents"
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)

        monkeypatch.setattr(
            "contree_cli.skill.default_agents_home", lambda: agents_home
        )
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )

        rc = cmd_skill_install(SkillInstallArgs(specs=()))
        assert rc is None
        # Non-claude types installed unconditionally; codex under ~/.agents
        assert (agents_home / "skills" / SKILL_NAME / "SKILL.md").is_file()
        # Claude types require ~/.claude to exist
        assert (claude_home / "skills" / SKILL_NAME / "SKILL.md").is_file()
        assert (claude_home / "agents" / f"{SKILL_NAME}.md").is_file()

    def test_install_scheme_spec(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )

        rc = cmd_skill_install(
            SkillInstallArgs(specs=frozenset({skill_from_spec("claude:~")}))
        )
        assert rc is None
        assert (claude_home / "skills" / SKILL_NAME / "SKILL.md").is_file()


class TestSkillUpgrade:
    def test_upgrade_rewrites_existing_install(
        self, tmp_path: Path, config_dir: Path
    ) -> None:
        dest = _installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest))) is None

        (dest / "SKILL.md").write_text("stale", encoding="utf-8")
        rc = cmd_skill_upgrade(SkillUpgradeArgs(specs=_specs(dest)))
        assert rc is None
        assert "stale" not in (dest / "SKILL.md").read_text(encoding="utf-8")

    def test_upgrade_requires_existing_install(
        self, tmp_path: Path, config_dir: Path, caplog
    ) -> None:
        with caplog.at_level("ERROR"):
            rc = cmd_skill_upgrade(SkillUpgradeArgs(specs=_specs(_installed(tmp_path))))
        assert rc == 1

    def test_upgrade_uses_remembered_when_omitted(
        self, tmp_path: Path, config_dir: Path
    ) -> None:
        dest1 = tmp_path / "codex" / SKILL_NAME
        dest2 = _claude_installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest1, dest2))) is None

        (dest1 / "SKILL.md").write_text("stale1", encoding="utf-8")
        (dest2 / "SKILL.md").write_text("stale2", encoding="utf-8")

        rc = cmd_skill_upgrade(SkillUpgradeArgs(specs=()))
        assert rc is None
        assert "stale1" not in (dest1 / "SKILL.md").read_text(encoding="utf-8")
        assert "stale2" not in (dest2 / "SKILL.md").read_text(encoding="utf-8")

    def test_upgrade_fails_when_registry_empty(self, config_dir: Path, caplog) -> None:
        with caplog.at_level("ERROR"):
            rc = cmd_skill_upgrade(SkillUpgradeArgs(specs=()))
        assert rc == 1


class TestSkillRemove:
    def test_remove_deletes_with_force(self, tmp_path: Path, config_dir: Path) -> None:
        dest = _installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest))) is None

        rc = cmd_skill_remove(SkillRemoveArgs(specs=_specs(dest), force=True))
        assert rc is None
        assert not dest.exists()
        assert list_installed() == frozenset()

    def test_remove_requires_existing(
        self, tmp_path: Path, config_dir: Path, caplog
    ) -> None:
        with caplog.at_level("ERROR"):
            rc = cmd_skill_remove(
                SkillRemoveArgs(specs=_specs(_installed(tmp_path)), force=True)
            )
        assert rc == 1

    def test_remove_multiple(self, tmp_path: Path, config_dir: Path) -> None:
        dest1 = tmp_path / "codex" / SKILL_NAME
        dest2 = _claude_installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest1, dest2))) is None

        rc = cmd_skill_remove(SkillRemoveArgs(specs=_specs(dest1, dest2), force=True))
        assert rc is None
        assert not dest1.exists()
        assert not dest2.exists()
        assert list_installed() == frozenset()

    def test_remove_all_remembered(self, tmp_path: Path, config_dir: Path) -> None:
        dest1 = tmp_path / "codex" / SKILL_NAME
        dest2 = _claude_installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest1, dest2))) is None

        rc = cmd_skill_remove(SkillRemoveArgs(force=True))
        assert rc is None
        assert not dest1.exists()
        assert not dest2.exists()
        assert list_installed() == frozenset()

    def test_remove_empty_registry(self, config_dir: Path, caplog) -> None:
        with caplog.at_level("ERROR"):
            rc = cmd_skill_remove(SkillRemoveArgs(force=True))
        assert rc == 1


class TestNonDestructiveInstall:
    """Installing into a populated directory must never delete foreign files."""

    def populated_dir(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.py").write_text("print('keep me')", encoding="utf-8")
        (repo / "README.md").write_text("keep me too", encoding="utf-8")
        return repo

    def test_install_keeps_foreign_files(self, tmp_path: Path) -> None:
        repo = self.populated_dir(tmp_path)
        ClaudeSkill(path=repo).install()
        assert (repo / "SKILL.md").is_file()
        assert (repo / "src" / "main.py").read_text(encoding="utf-8") == (
            "print('keep me')"
        )
        assert (repo / "README.md").read_text(encoding="utf-8") == "keep me too"

    def test_install_without_force_works_when_no_skill_md(self, tmp_path: Path) -> None:
        repo = self.populated_dir(tmp_path)
        skill = ClaudeSkill(path=repo)
        assert not skill.exists
        skill.install()
        assert skill.exists

    def test_install_refuses_when_skill_md_present(self, tmp_path: Path) -> None:
        repo = self.populated_dir(tmp_path)
        skill = ClaudeSkill(path=repo)
        skill.install()
        with pytest.raises(FileExistsError):
            skill.install()

    def test_install_refuses_when_only_version_file_present(
        self, tmp_path: Path
    ) -> None:
        """A partially-removed prior install (SKILL.md gone, .version
        left behind) must not have .version silently overwritten."""
        repo = self.populated_dir(tmp_path)
        skill = ClaudeSkill(path=repo)
        skill.install()
        (repo / "SKILL.md").unlink()
        assert not skill.exists
        with pytest.raises(FileExistsError):
            skill.install()
        # --force still works and rewrites cleanly.
        skill.install(force=True)
        assert skill.exists

    def test_force_reinstall_replaces_only_skill_files(self, tmp_path: Path) -> None:
        repo = self.populated_dir(tmp_path)
        skill = ClaudeSkill(path=repo)
        skill.install()
        (repo / "SKILL.md").write_text("stale", encoding="utf-8")
        skill.install(force=True)
        assert "stale" not in (repo / "SKILL.md").read_text(encoding="utf-8")
        assert (repo / "src" / "main.py").read_text(encoding="utf-8") == (
            "print('keep me')"
        )
        assert (repo / "README.md").read_text(encoding="utf-8") == "keep me too"

    def test_remove_keeps_foreign_files_and_dir(self, tmp_path: Path) -> None:
        repo = self.populated_dir(tmp_path)
        skill = ClaudeSkill(path=repo)
        skill.install()
        skill.remove()
        assert not (repo / "SKILL.md").exists()
        assert not (repo / ".version").exists()
        assert not (repo / "agents").exists()
        assert (repo / "src" / "main.py").is_file()
        assert (repo / "README.md").is_file()

    def test_remove_clean_install_removes_dir(self, tmp_path: Path) -> None:
        dest = tmp_path / "skills" / SKILL_NAME
        skill = ClaudeSkill(path=dest)
        skill.install()
        skill.remove()
        assert not dest.exists()

    def test_remove_keeps_sibling_skills_and_agents(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        claude_home = tmp_path / ".claude"
        other_skill = claude_home / "skills" / "other-skill" / "SKILL.md"
        other_skill.parent.mkdir(parents=True)
        other_skill.write_text("other skill", encoding="utf-8")
        other_agent = claude_home / "agents" / "reviewer.md"
        other_agent.parent.mkdir(parents=True)
        other_agent.write_text("other agent", encoding="utf-8")
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        monkeypatch.setattr(
            "contree_cli.skill.default_codex_home", lambda: tmp_path / ".codex"
        )

        assert cmd_skill_install(SkillInstallArgs(specs=())) is None
        assert (claude_home / "skills" / SKILL_NAME / "SKILL.md").is_file()
        assert (claude_home / "agents" / f"{SKILL_NAME}.md").is_file()

        assert cmd_skill_remove(SkillRemoveArgs(force=True)) is None
        assert not (claude_home / "skills" / SKILL_NAME).exists()
        assert not (claude_home / "agents" / f"{SKILL_NAME}.md").exists()
        assert other_skill.read_text(encoding="utf-8") == "other skill"
        assert other_agent.read_text(encoding="utf-8") == "other agent"

    def test_remove_shared_dir_keeps_foreign_agents(self, tmp_path: Path) -> None:
        claude_home = tmp_path / ".claude"
        other_agent = claude_home / "agents" / "reviewer.md"
        other_agent.parent.mkdir(parents=True)
        other_agent.write_text("other agent", encoding="utf-8")

        skill = ClaudeSkill(path=claude_home)
        skill.install()
        skill.remove()
        assert other_agent.read_text(encoding="utf-8") == "other agent"
        assert other_agent.parent.is_dir()


class TestProjectRootSpecs:
    """A directory spec is a project root, not the skill directory itself."""

    def test_raw_dir_expands_to_all_kinds(self, tmp_path: Path, monkeypatch) -> None:
        claude_home = tmp_path / ".claude-home"
        claude_home.mkdir()
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        root = tmp_path / "proj"
        root.mkdir()

        skills = skills_from_spec(str(root))
        paths = {s.path for s in skills}
        assert len(skills) == len(ALL_SKILL_TYPES)
        assert root / ".claude" / "skills" / SKILL_NAME in paths
        assert root / ".agents" / "skills" / SKILL_NAME in paths
        assert root / ".claude" / "agents" / f"{SKILL_NAME}.md" in paths
        assert root / ".claude" / "agents" / f"{SKILL_NAME}-subagent.md" in paths
        assert all(str(s.path).startswith(str(root)) for s in skills)

    def test_raw_dir_without_claude_home_skips_claude_kinds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home",
            lambda: tmp_path / "missing",
        )
        root = tmp_path / "proj"
        root.mkdir()

        kinds = {s.kind for s in skills_from_spec(str(root))}
        assert "claude" not in kinds
        assert "codex" in kinds

    def test_kind_path_resolves_project_root(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        skill = skill_from_spec(f"claude:{root}")
        assert skill.path == root / ".claude" / "skills" / SKILL_NAME

    def test_codex_kind_path_uses_agents_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        skill = skill_from_spec(f"codex:{root}")
        assert skill.path == root / ".agents" / "skills" / SKILL_NAME

    def test_codex_global_under_agents_home(self, tmp_path: Path, monkeypatch) -> None:
        agents_home = tmp_path / ".agents"
        monkeypatch.setattr(
            "contree_cli.skill.default_agents_home", lambda: agents_home
        )
        skill = skill_from_spec("codex:~")
        assert skill.path == agents_home / "skills" / SKILL_NAME

    def test_dir_with_skill_md_stays_literal(self, tmp_path: Path) -> None:
        dest = tmp_path / "somewhere"
        dest.mkdir()
        (dest / "SKILL.md").write_text("installed", encoding="utf-8")
        assert skills_from_spec(str(dest)) == (ClaudeSkill(path=dest),)

    def test_contree_basename_under_skills_dir_stays_literal(
        self, tmp_path: Path
    ) -> None:
        """A path shaped like a real install target (.../skills/contree)
        stays literal even without SKILL.md, e.g. a partially-removed
        install being pointed at directly for cleanup."""
        dest = tmp_path / "anywhere" / "skills" / SKILL_NAME
        assert skills_from_spec(str(dest)) == (ClaudeSkill(path=dest),)

    def test_contree_basename_elsewhere_expands_as_project_root(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A project directory that merely happens to be named `contree`
        (parent isn't `skills/`) must NOT be mistaken for a skill
        artifact -- it expands like any other project root."""
        claude_home = tmp_path / ".claude-home"
        claude_home.mkdir()
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        root = tmp_path / "anywhere" / SKILL_NAME
        skills = skills_from_spec(str(root))
        assert len(skills) == len(ALL_SKILL_TYPES)
        assert all(str(s.path).startswith(str(root)) for s in skills)

    def test_md_path_stays_literal(self, tmp_path: Path) -> None:
        dest = tmp_path / "somewhere" / "custom.md"
        assert skills_from_spec(str(dest)) == (ClaudeSubagentSkill(path=dest),)

    def test_installed_path_round_trips_for_removal(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        installed = root / ".claude" / "skills" / SKILL_NAME
        skills = skills_from_spec(str(installed))
        assert skills == (ClaudeSkill(path=installed),)

    def test_project_root_inside_agent_home_expands(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A project living inside .claude/.codex/.agents is still a root."""
        claude_home = tmp_path / ".claude-home"
        claude_home.mkdir()
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        root = tmp_path / ".claude" / "projects" / "proj"
        root.mkdir(parents=True)
        skills = skills_from_spec(str(root))
        assert len(skills) == len(ALL_SKILL_TYPES)
        assert all(str(s.path).startswith(str(root)) for s in skills)

    def test_install_remove_cycle_leaves_project_clean(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        claude_home = tmp_path / ".claude-home"
        claude_home.mkdir()
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex-home"))
        root = tmp_path / "proj"
        root.mkdir()
        (root / "app.py").write_text("app", encoding="utf-8")

        specs = frozenset(skills_from_spec(str(root)))
        assert cmd_skill_install(SkillInstallArgs(specs=specs)) is None
        assert cmd_skill_remove(SkillRemoveArgs(specs=specs, force=True)) is None
        assert [p.name for p in root.iterdir()] == ["app.py"]


class TestContreeHomeInSkill:
    """Rendered skills and the install registry must respect CONTREE_HOME."""

    def test_codex_sandbox_lists_contree_home_writable_root(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        data_home = tmp_path / "contree-data"
        monkeypatch.setattr(config_mod, "CONTREE_HOME", data_home)
        body = CodexSkill(path=tmp_path / "codex").body()
        assert re.search(r'writable_roots = \["[^"]*contree-data"\]', body)
        # TOML basic strings treat backslash as an escape; the path must
        # be rendered with forward slashes on every platform
        assert "\\" not in body

    def test_codex_sandbox_contracts_home_prefix(self, monkeypatch) -> None:
        monkeypatch.setattr(config_mod, "CONTREE_HOME", Path.home() / "custom-contree")
        body = CodexSkill(path=Path("unused")).body()
        assert 'writable_roots = ["~/custom-contree"]' in body

    def test_installed_skill_md_reflects_contree_home(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        data_home = tmp_path / "contree-data"
        monkeypatch.setattr(config_mod, "CONTREE_HOME", data_home)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex-home"))
        dest = tmp_path / ".codex" / "skills" / SKILL_NAME
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest))) is None
        text = (dest / "SKILL.md").read_text(encoding="utf-8")
        assert re.search(r'writable_roots = \["[^"]*contree-data"\]', text)

    def test_registry_db_lives_under_contree_home(
        self, tmp_path: Path, config_dir: Path
    ) -> None:
        dest = _installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest))) is None
        assert (config_mod.CONTREE_HOME / "cli" / "skills.db").is_file()


class TestSkillList:
    def test_list_shows_remembered(self, tmp_path: Path, config_dir: Path) -> None:
        dest1 = tmp_path / "codex" / SKILL_NAME
        dest2 = _claude_installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest1, dest2))) is None

        rows: list[dict[str, object]] = []

        class CaptureFormatter:
            def __call__(self, **kwargs: object) -> None:
                rows.append(kwargs)

            def flush(self) -> None:
                return

        from contree_cli import FORMATTER

        FORMATTER.set(CaptureFormatter())
        assert cmd_skill_list(SkillListArgs()) is None

        v = skill_version()
        assert len(rows) == 2
        for row in rows:
            assert row["name"] == "contree"
            assert row["kind"] == "claude"
            assert row["version"] == v
            assert row["latest"] == v
            assert row["outdated"] is False
            assert row["exists"] is True

    def test_stale_entries_cleaned_on_list(
        self, tmp_path: Path, config_dir: Path
    ) -> None:
        dest = _installed(tmp_path)
        assert cmd_skill_install(SkillInstallArgs(specs=_specs(dest))) is None
        assert len(list_installed()) == 1

        # Remove directory behind the registry's back
        shutil.rmtree(dest)

        # list_installed should clean up and return empty
        assert list_installed() == frozenset()


class TestParseVersion:
    def test_simple(self) -> None:
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_with_suffix(self) -> None:
        assert parse_version("1.2.3rc1") == (1, 2)

    def test_unknown(self) -> None:
        assert parse_version("unknown") == ()

    def test_comparison(self) -> None:
        assert parse_version("0.4.3") < parse_version("0.4.4")
        assert parse_version("0.4.4") == parse_version("0.4.4")


class TestSkillFromSpec:
    def test_claude_global(self, monkeypatch) -> None:
        home = Path("/tmp/test-claude")
        monkeypatch.setattr("contree_cli.skill.default_claude_home", lambda: home)
        skill = skill_from_spec("claude:~")
        assert isinstance(skill, ClaudeSkill)
        assert skill.path == home / "skills" / SKILL_NAME

    def test_codex_global(self, monkeypatch) -> None:
        home = Path("/tmp/test-agents")
        monkeypatch.setattr("contree_cli.skill.default_agents_home", lambda: home)
        skill = skill_from_spec("codex:~")
        assert isinstance(skill, CodexSkill)
        assert skill.path == home / "skills" / SKILL_NAME

    def test_opencode_global(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENCODE_HOME", raising=False)
        skill = skill_from_spec("opencode:~")
        assert isinstance(skill, OpenCodeSkill)

    def test_amp_global(self) -> None:
        skill = skill_from_spec("amp:~")
        assert isinstance(skill, AmpSkill)

    def test_cline_global(self, monkeypatch) -> None:
        monkeypatch.delenv("CLINE_DIR", raising=False)
        skill = skill_from_spec("cline:~")
        assert isinstance(skill, ClineSkill)

    def test_claude_subagent(self, tmp_path: Path) -> None:
        md = tmp_path / "skill.md"
        skill = skill_from_spec(str(md))
        assert isinstance(skill, ClaudeSubagentSkill)

    def test_raw_path_guess(self, tmp_path: Path) -> None:
        p = tmp_path / ".codex" / "skills" / "contree"
        skill = skill_from_spec(str(p))
        assert isinstance(skill, CodexSkill)

    def test_project_level(self) -> None:
        skill = skill_from_spec("claude:")
        assert isinstance(skill, ClaudeSkill)
        assert skill.path.name == SKILL_NAME


class TestGuessSkill:
    def test_md_suffix(self, tmp_path: Path) -> None:
        p = tmp_path / "foo.md"
        assert isinstance(guess_skill(p), ClaudeSubagentSkill)

    def test_claude_marker(self) -> None:
        p = Path("/home/user/.claude/skills/contree")
        assert isinstance(guess_skill(p), ClaudeSkill)

    def test_codex_marker(self) -> None:
        p = Path("/home/user/.codex/skills/contree")
        assert isinstance(guess_skill(p), CodexSkill)

    def test_opencode_marker(self) -> None:
        p = Path("/home/user/.config/opencode/skills/contree")
        assert isinstance(guess_skill(p), OpenCodeSkill)

    def test_cline_marker(self) -> None:
        p = Path("/home/user/.cline/skills/contree")
        assert isinstance(guess_skill(p), ClineSkill)

    def test_agents_marker(self) -> None:
        p = Path("/home/user/.config/agents/skills/contree")
        assert isinstance(guess_skill(p), AmpSkill)

    def test_dot_agents_marker_is_codex(self) -> None:
        p = Path("/home/user/.agents/skills/contree")
        assert isinstance(guess_skill(p), CodexSkill)

    def test_unknown_defaults_anthropic(self) -> None:
        p = Path("/some/random/path")
        assert isinstance(guess_skill(p), ClaudeSkill)


class TestSkillClasses:
    def test_codex_render(self, tmp_path: Path) -> None:
        s = CodexSkill(path=tmp_path / "codex")
        assert "---" in s.render()
        assert "interface:" in s.openai_yaml()
        assert "display_name" in s.openai_yaml()

    def test_codex_has_frontmatter(self, tmp_path: Path) -> None:
        s = CodexSkill(path=tmp_path / "codex")
        assert s.frontmatter().startswith("---")

    def test_codex_install_creates_rules(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        codex_home = tmp_path / ".codex"
        monkeypatch.setattr("contree_cli.skill.default_codex_home", lambda: codex_home)
        s = CodexSkill(path=codex_home / "skills" / "contree")
        s.install()
        rules = codex_home / "rules" / "contree.rules"
        assert rules.is_file()
        assert "contree" in rules.read_text(encoding="utf-8")
        assert "allow" in rules.read_text(encoding="utf-8")

    def test_codex_remove_deletes_rules(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        codex_home = tmp_path / ".codex"
        monkeypatch.setattr("contree_cli.skill.default_codex_home", lambda: codex_home)
        s = CodexSkill(path=codex_home / "skills" / "contree")
        s.install()
        rules = codex_home / "rules" / "contree.rules"
        assert rules.is_file()
        s.remove()
        assert not rules.exists()
        assert not s.path.exists()

    def test_anthropic_frontmatter_has_allowed_tools(self, tmp_path: Path) -> None:
        s = ClaudeSkill(path=tmp_path / "claude")
        fm = s.frontmatter()
        assert "allowed-tools:" in fm
        assert "Bash(contree:*)" in fm

    def test_subagent_install_remove(self, tmp_path: Path, config_dir: Path) -> None:
        md = tmp_path / "test.md"
        s = ClaudeSubagentSkill(path=md)
        s.install()
        assert md.exists()
        assert "---" in md.read_text(encoding="utf-8")
        s.remove()
        assert not md.exists()

    def test_subagent_no_fallback(self, tmp_path: Path) -> None:
        s = ClaudeSubagentSkill(path=tmp_path / "sub.md")
        assert s.fallback() == ""

    def test_subagent_frontmatter_uses_tools_field(self, tmp_path: Path) -> None:
        fm = ClaudeSubagentSkill(path=tmp_path / "sub.md").frontmatter()
        assert "tools: Bash, Read, Grep" in fm
        assert "allowed-tools" not in fm

    def test_codex_body_has_sandbox_section(self, tmp_path: Path) -> None:
        assert "## Codex Sandbox" in CodexSkill(path=tmp_path / "codex").body()

    def test_claude_body_has_no_sandbox_section(self, tmp_path: Path) -> None:
        assert "## Codex Sandbox" not in ClaudeSkill(path=tmp_path / "claude").body()

    def test_workflow_numbering_is_sequential(self, tmp_path: Path) -> None:
        for skill in (
            ClaudeSkill(path=tmp_path / "claude"),
            ClaudeSubagentSkill(path=tmp_path / "sub.md"),
        ):
            body = skill.body()
            workflow = body.split("## Required Workflow", 1)[1].split("\n## ", 1)[0]
            numbers = [
                int(line.split(".", 1)[0])
                for line in workflow.splitlines()
                if line[:1].isdigit()
            ]
            assert numbers == list(range(1, len(numbers) + 1))

    def test_skill_hash_eq(self, tmp_path: Path) -> None:
        a = ClaudeSkill(path=tmp_path / "a")
        b = ClaudeSkill(path=tmp_path / "a")
        c = ClaudeSkill(path=tmp_path / "c")
        assert a == b
        assert a != c
        assert hash(a) == hash(b)
        assert a != "not a skill"

    def test_resolve_path_empty(self) -> None:
        p = ClaudeSkill.resolve_path("")
        assert p.name == SKILL_NAME
        assert p.is_absolute()

    def test_resolve_path_treats_dir_as_project_root(self, tmp_path: Path) -> None:
        p = ClaudeSkill.resolve_path(str(tmp_path / "custom"))
        root = (tmp_path / "custom").resolve()
        assert p == root / ".claude" / "skills" / SKILL_NAME

    def test_installed_version_missing(self, tmp_path: Path) -> None:
        s = ClaudeSkill(path=tmp_path / "noexist")
        assert s.installed_version == ""
        assert s.needs_upgrade is True

    def test_installed_version_present(self, tmp_path: Path) -> None:
        dest = tmp_path / "skill"
        dest.mkdir()
        (dest / ".version").write_text("0.4.4", encoding="utf-8")
        s = ClaudeSkill(path=dest)
        assert s.installed_version == "0.4.4"

    def test_opencode_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "oc"))
        assert OpenCodeSkill.home_dir() == tmp_path / "oc"

    def test_cline_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CLINE_DIR", str(tmp_path / "cl"))
        assert ClineSkill.home_dir() == tmp_path / "cl"

    def test_codex_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
        from contree_cli.skill import default_codex_home

        assert default_codex_home() == tmp_path / "cx"


class TestClaudeAgentSkill:
    def test_resolve_path_global(self, monkeypatch) -> None:
        home = Path("/tmp/test-claude")
        monkeypatch.setattr("contree_cli.skill.default_claude_home", lambda: home)
        p = ClaudeAgentSkill.resolve_path("~")
        assert p == home / "agents" / f"{SKILL_NAME}.md"

    def test_resolve_path_project(self) -> None:
        p = ClaudeAgentSkill.resolve_path("")
        assert p.name == f"{SKILL_NAME}.md"
        assert "agents" in p.parts

    def test_resolve_path_explicit(self, tmp_path: Path) -> None:
        p = ClaudeAgentSkill.resolve_path(str(tmp_path / "custom.md"))
        assert p == (tmp_path / "custom.md").resolve()

    def test_spec_global(self, monkeypatch) -> None:
        home = Path("/tmp/test-claude")
        monkeypatch.setattr("contree_cli.skill.default_claude_home", lambda: home)
        skill = skill_from_spec("claude-agent:~")
        assert isinstance(skill, ClaudeAgentSkill)
        assert skill.path == home / "agents" / f"{SKILL_NAME}.md"

    def test_render_has_skills_frontmatter(self, tmp_path: Path) -> None:
        s = ClaudeAgentSkill(path=tmp_path / "agent.md")
        rendered = s.render()
        assert "skills:" in rendered
        assert "- contree" in rendered
        assert "tools: Bash, Read, Grep" in rendered

    def test_install_remove(self, tmp_path: Path, config_dir: Path) -> None:
        md = tmp_path / "agents" / "contree.md"
        s = ClaudeAgentSkill(path=md)
        s.install()
        assert md.exists()
        content = md.read_text(encoding="utf-8")
        assert "---" in content
        assert "skills:" in content
        s.remove()
        assert not md.exists()

    def test_install_refuses_existing(self, tmp_path: Path) -> None:
        md = tmp_path / "agents" / "contree.md"
        s = ClaudeAgentSkill(path=md)
        s.install()
        import pytest

        with pytest.raises(FileExistsError):
            s.install()

    def test_install_force_overwrites(self, tmp_path: Path) -> None:
        md = tmp_path / "agents" / "contree.md"
        s = ClaudeAgentSkill(path=md)
        s.install()
        md.write_text("stale", encoding="utf-8")
        s.install(force=True)
        assert "stale" not in md.read_text(encoding="utf-8")

    def test_cmd_install_includes_agent(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        agents_home = tmp_path / ".agents-fresh"
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        monkeypatch.setattr(
            "contree_cli.skill.default_agents_home", lambda: agents_home
        )

        rc = cmd_skill_install(SkillInstallArgs(specs=()))
        assert rc is None
        # Claude types: require ~/.claude
        assert (claude_home / "skills" / SKILL_NAME / "SKILL.md").is_file()
        assert (claude_home / "agents" / f"{SKILL_NAME}.md").is_file()
        # Non-claude types: installed even without pre-existing home
        assert (agents_home / "skills" / SKILL_NAME / "SKILL.md").is_file()

    def test_no_claude_types_without_home(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        claude_home = tmp_path / ".claude-nonexist"
        agents_home = tmp_path / ".agents"
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        monkeypatch.setattr(
            "contree_cli.skill.default_agents_home", lambda: agents_home
        )

        rc = cmd_skill_install(SkillInstallArgs(specs=()))
        assert rc is None
        assert not (claude_home / "skills" / SKILL_NAME).exists()
        assert not (claude_home / "agents").exists()
        assert (agents_home / "skills" / SKILL_NAME / "SKILL.md").is_file()

    def test_render_mentions_subagents(self, tmp_path: Path) -> None:
        s = ClaudeAgentSkill(path=tmp_path / "agent.md")
        rendered = s.render()
        assert "subagent" in rendered.lower()

    def test_kind(self) -> None:
        assert ClaudeAgentSkill.kind == "claude-agent"
