"""Show the manual.

`contree agent [TOPIC]` prints the built-in manual.
`contree man` is an alias.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from contree_cli import FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.man import agent_manual
from contree_cli.output import DefaultFormatter

EPILOG = """\
examples:
  contree agent
  contree agent sessions
  contree man commands
"""


@dataclass(frozen=True)
class AgentArgs(ArgumentsProtocol):
    topic: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> AgentArgs:
        return cls(topic=ns.topic)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    topics = sorted(agent_manual().topics())
    p.add_argument(
        "topic",
        nargs="?",
        default="all",
        choices=topics,
        help="Manual topic",
    )
    return cmd_agent, AgentArgs


def cmd_agent(args: AgentArgs) -> None:
    manual = agent_manual()
    topics = manual.topics()
    sections = topics.get(args.topic, topics["all"])
    formatter = FORMATTER.get()
    if isinstance(formatter, DefaultFormatter):
        from contree_cli.man import Manual

        print(Manual(title=manual.title, sections=sections).render())
        return
    total = len(sections)
    for i, s in enumerate(sections, 1):
        formatter(
            command="agent",
            topic=args.topic,
            section=s.title,
            text=s.body,
            section_index=i,
            section_count=total,
        )
