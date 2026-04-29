from __future__ import annotations

import json

import pytest

from contree_cli import FORMATTER
from contree_cli.cli.agent import AgentArgs, cmd_agent
from contree_cli.man import agent_manual, parse_manual
from contree_cli.output import DefaultFormatter, JSONFormatter

Capsys = pytest.CaptureFixture[str]


class TestParseManual:
    def test_parses_title(self) -> None:
        m = parse_manual("My Title\n========\n\nBody here.")
        assert m.title == "My Title"

    def test_parses_sections(self) -> None:
        text = (
            "Doc\n===\n\n"
            "Section One\n===========\n\nbody1\n\n"
            "Section Two\n===========\n\nbody2"
        )
        m = parse_manual(text)
        assert len(m.sections) == 2
        assert m.sections[0].title == "Section One"
        assert "body1" in m.sections[0].body
        assert m.sections[1].title == "Section Two"

    def test_render(self) -> None:
        text = "Doc\n===\n\nSec\n===\n\nbody"
        m = parse_manual(text)
        rendered = m.render()
        assert "Doc" in rendered
        assert "Sec" in rendered
        assert "body" in rendered

    def test_topics(self) -> None:
        text = "Doc\n===\n\nMy Topic\n========\n\nbody"
        m = parse_manual(text)
        topics = m.topics()
        assert "all" in topics
        assert "my_topic" in topics
        assert "my" in topics


class TestAgentManual:
    def test_loads(self) -> None:
        m = agent_manual()
        assert m.title
        assert len(m.sections) > 0

    def test_has_core_sections(self) -> None:
        topics = agent_manual().topics()
        assert "sessions" in topics
        assert "all" in topics


class TestCmdAgent:
    def test_default_output(self, capsys: Capsys) -> None:
        FORMATTER.set(DefaultFormatter())
        cmd_agent(AgentArgs(topic="all"))
        out = capsys.readouterr().out
        assert "Manual" in out

    def test_sessions_topic(self, capsys: Capsys) -> None:
        FORMATTER.set(DefaultFormatter())
        cmd_agent(AgentArgs(topic="sessions"))
        out = capsys.readouterr().out
        assert "Sessions" in out or "sessions" in out.lower()

    def test_json_output(self, capsys: Capsys) -> None:
        FORMATTER.set(JSONFormatter())
        cmd_agent(AgentArgs(topic="all"))
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert len(lines) == len(agent_manual().sections)
        parsed = json.loads(lines[0])
        assert parsed["command"] == "agent"
