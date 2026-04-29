from __future__ import annotations

import shutil
from pathlib import Path

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
        codex_home = tmp_path / ".codex"
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)

        monkeypatch.setattr("contree_cli.skill.default_codex_home", lambda: codex_home)
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )

        rc = cmd_skill_install(SkillInstallArgs(specs=()))
        assert rc is None
        # Non-claude types installed unconditionally
        assert (codex_home / "skills" / SKILL_NAME / "SKILL.md").is_file()
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
        home = Path("/tmp/test-codex")
        monkeypatch.setattr("contree_cli.skill.default_codex_home", lambda: home)
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
        assert s.references() == ""

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

    def test_resolve_path_explicit(self, tmp_path: Path) -> None:
        p = ClaudeSkill.resolve_path(str(tmp_path / "custom"))
        assert p == (tmp_path / "custom").resolve()

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
        assert "tools:" in rendered
        assert "- Bash" in rendered

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
        codex_home = tmp_path / ".codex-fresh"
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        monkeypatch.setattr("contree_cli.skill.default_codex_home", lambda: codex_home)

        rc = cmd_skill_install(SkillInstallArgs(specs=()))
        assert rc is None
        # Claude types: require ~/.claude
        assert (claude_home / "skills" / SKILL_NAME / "SKILL.md").is_file()
        assert (claude_home / "agents" / f"{SKILL_NAME}.md").is_file()
        # Non-claude types: installed even without pre-existing home
        assert (codex_home / "skills" / SKILL_NAME / "SKILL.md").is_file()

    def test_no_claude_types_without_home(
        self, tmp_path: Path, config_dir: Path, monkeypatch
    ) -> None:
        claude_home = tmp_path / ".claude-nonexist"
        codex_home = tmp_path / ".codex"
        monkeypatch.setattr(
            "contree_cli.skill.default_claude_home", lambda: claude_home
        )
        monkeypatch.setattr("contree_cli.skill.default_codex_home", lambda: codex_home)

        rc = cmd_skill_install(SkillInstallArgs(specs=()))
        assert rc is None
        assert not (claude_home / "skills" / SKILL_NAME).exists()
        assert not (claude_home / "agents").exists()
        assert (codex_home / "skills" / SKILL_NAME / "SKILL.md").is_file()

    def test_render_mentions_subagents(self, tmp_path: Path) -> None:
        s = ClaudeAgentSkill(path=tmp_path / "agent.md")
        rendered = s.render()
        assert "subagent" in rendered.lower()

    def test_kind(self) -> None:
        assert ClaudeAgentSkill.kind == "claude-agent"
