"""Sphinx extension that auto-generates CLI reference from argparse parsers.

Provides a ``contree-command`` directive that introspects the live
``contree_cli.arguments.parser`` object and renders structured docs for any
registered command (or ``__global__`` for top-level options).

Builds docutils nodes directly (no RST/MyST text generation) so it works
correctly regardless of the source parser (MyST or RST).
"""

from __future__ import annotations

import argparse
from typing import Any, ClassVar

from docutils import nodes
from sphinx.application import Sphinx
from sphinx.util.docutils import SphinxDirective


def _get_parser() -> argparse.ArgumentParser:
    from contree_cli.arguments import parser

    return parser


def _iter_subparser_actions(
    parser: argparse.ArgumentParser,
) -> list[argparse._SubParsersAction]:  # type: ignore[type-arg]
    if parser._subparsers is None:
        return []
    return [
        a
        for a in parser._subparsers._group_actions
        if isinstance(a, argparse._SubParsersAction)
    ]


def _get_subparsers(
    parser: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser]:
    result: dict[str, argparse.ArgumentParser] = {}
    for action in _iter_subparser_actions(parser):
        for name, sub in action.choices.items():
            result[name] = sub
    return result


def _find_command_parser(name: str) -> argparse.ArgumentParser | None:
    return _get_subparsers(_get_parser()).get(name)


def _get_aliases(parser: argparse.ArgumentParser, name: str) -> list[str]:
    for action in _iter_subparser_actions(parser):
        target = action.choices.get(name)
        if target is None:
            continue
        return [
            alias
            for alias, p in action.choices.items()
            if p is target and alias != name
        ]
    return []


def _get_sub_aliases(
    parent: argparse.ArgumentParser,
    name: str,
) -> list[str]:
    for action in _iter_subparser_actions(parent):
        target = action.choices.get(name)
        if target is None:
            continue
        return [
            alias
            for alias, p in action.choices.items()
            if p is target and alias != name
        ]
    return []


def _get_help_text(
    parser: argparse.ArgumentParser,
    sub_name: str,
) -> str:
    for action in _iter_subparser_actions(parser):
        for choice_action in action._choices_actions:
            if choice_action.dest == sub_name:
                return choice_action.help or ""
    return ""


def _format_metavar(action: argparse.Action) -> str:
    if action.metavar:
        if isinstance(action.metavar, tuple):
            return " ".join(action.metavar)
        return str(action.metavar)
    if action.type is not None:
        return action.type.__name__.upper()
    return action.dest.upper()


def _format_default(action: argparse.Action) -> str:
    if action.default in (None, False, [], argparse.SUPPRESS):
        return ""
    return str(action.default)


def _format_choices(action: argparse.Action) -> str:
    if action.choices is None:
        return ""
    return ", ".join(str(c) for c in action.choices)


def _gather_positionals(
    parser: argparse.ArgumentParser,
) -> list[argparse.Action]:
    return [
        a
        for a in parser._actions
        if not a.option_strings
        and not isinstance(a, argparse._SubParsersAction)
        and not isinstance(a, argparse._HelpAction)
    ]


def _gather_optionals(
    parser: argparse.ArgumentParser,
) -> list[argparse.Action]:
    return [
        a
        for a in parser._actions
        if a.option_strings and not isinstance(a, argparse._HelpAction)
    ]


# -- Node builders ----------------------------------------------------------


def _text(txt: str) -> nodes.Text:
    return nodes.Text(txt)


def _literal(txt: str) -> nodes.literal:
    return nodes.literal(txt, txt)


def _paragraph(*children: nodes.Node) -> nodes.paragraph:
    p = nodes.paragraph()
    p.extend(children)
    return p


def _rubric(title: str) -> nodes.rubric:
    return nodes.rubric(title, title)


def _nargs_suffix(action: argparse.Action) -> str:
    if action.nargs == argparse.REMAINDER:
        return " (all remaining args)"
    if action.nargs == "?":
        return " (optional)"
    if action.nargs == "*":
        return " (zero or more)"
    if action.nargs == "+":
        return " (one or more)"
    return ""


def _build_table(
    headers: list[str],
    rows: list[list[list[nodes.Node]]],
) -> nodes.table:
    table = nodes.table()
    ncols = len(headers)
    tgroup = nodes.tgroup(cols=ncols)
    table += tgroup

    for _ in range(ncols):
        tgroup += nodes.colspec()

    # Header
    thead = nodes.thead()
    tgroup += thead
    hrow = nodes.row()
    thead += hrow
    for h in headers:
        entry = nodes.entry()
        entry += _paragraph(_text(h))
        hrow += entry

    # Body
    tbody = nodes.tbody()
    tgroup += tbody
    for row_cells in rows:
        row = nodes.row()
        tbody += row
        for cell_nodes in row_cells:
            entry = nodes.entry()
            if cell_nodes:
                p = nodes.paragraph()
                p.extend(cell_nodes)
                entry += p
            row += entry

    return table


def _build_positionals_table(
    actions: list[argparse.Action],
) -> nodes.table:
    has_defaults = any(_format_default(a) for a in actions)

    headers = ["Argument", "Description"]
    if has_defaults:
        headers.append("Default")

    rows: list[list[list[nodes.Node]]] = []
    for action in actions:
        name = action.dest.upper()
        suffix = _nargs_suffix(action)
        label = name + suffix

        row: list[list[nodes.Node]] = [
            [_literal(label)],
            [_text(action.help or "")],
        ]
        if has_defaults:
            default = _format_default(action)
            row.append([_text(default)] if default else [])
        rows.append(row)

    return _build_table(headers=headers, rows=rows)


def _takes_value(action: argparse.Action) -> bool:
    return not isinstance(
        action,
        (
            argparse._StoreTrueAction,
            argparse._StoreFalseAction,
            argparse._StoreConstAction,
            argparse._CountAction,
        ),
    )


def _option_flags_nodes(action: argparse.Action) -> list[nodes.Node]:
    result: list[nodes.Node] = []
    metavar = _format_metavar(action) if _takes_value(action) else ""
    for i, opt in enumerate(action.option_strings):
        if i > 0:
            result.append(_text(", "))
        label = f"{opt} {metavar}" if metavar else opt
        result.append(_literal(label))
    return result


def _build_optionals_table(
    actions: list[argparse.Action],
) -> nodes.table:
    has_defaults = any(_format_default(a) for a in actions)
    has_choices = any(_format_choices(a) for a in actions)

    headers = ["Option", "Description"]
    if has_defaults:
        headers.append("Default")
    if has_choices:
        headers.append("Choices")

    rows: list[list[list[nodes.Node]]] = []
    for action in actions:
        row: list[list[nodes.Node]] = [
            _option_flags_nodes(action),
            [_text(action.help or "")],
        ]
        if has_defaults:
            default = _format_default(action)
            row.append([_text(default)] if default else [])
        if has_choices:
            choices = _format_choices(action)
            row.append([_text(choices)] if choices else [])
        rows.append(row)

    return _build_table(headers=headers, rows=rows)


def _build_parser_nodes(
    parser: argparse.ArgumentParser,
    command_name: str,
    env: Any,
) -> list[nodes.Node]:
    result: list[nodes.Node] = []

    positionals = _gather_positionals(parser)
    if positionals:
        result.append(_rubric("Positional arguments"))
        result.append(_build_positionals_table(positionals))

    optionals = _gather_optionals(parser)
    if optionals:
        result.append(_rubric("Options"))
        result.append(_build_optionals_table(optionals))

    # Subcommands
    sub_parsers = _get_subparsers(parser)
    if sub_parsers:
        seen: set[int] = set()
        unique_subs: list[tuple[str, argparse.ArgumentParser]] = []
        for name, sub in sub_parsers.items():
            pid = id(sub)
            if pid not in seen:
                seen.add(pid)
                unique_subs.append((name, sub))

        result.append(_rubric("Subcommands"))

        for sub_name, sub_parser in unique_subs:
            aliases = _get_sub_aliases(parser, sub_name)

            # Bold title line
            title_nodes: list[nodes.Node] = [
                _literal(f"{command_name} {sub_name}"),
            ]
            if aliases:
                alias_parts: list[nodes.Node] = [_text(" (alias: ")]
                for i, a in enumerate(aliases):
                    if i > 0:
                        alias_parts.append(_text(", "))
                    alias_parts.append(_literal(a))
                alias_parts.append(_text(")"))
                title_nodes.extend(alias_parts)

            title_p = nodes.paragraph()
            strong = nodes.strong()
            strong.extend(title_nodes)
            title_p += strong
            result.append(title_p)

            sub_desc = sub_parser.description or ""
            if not sub_desc:
                sub_desc = _get_help_text(parser, sub_name)
            if sub_desc:
                result.append(_paragraph(_text(sub_desc.strip())))

            sub_positionals = _gather_positionals(sub_parser)
            if sub_positionals:
                result.append(_build_positionals_table(sub_positionals))

            sub_optionals = _gather_optionals(sub_parser)
            if sub_optionals:
                result.append(_build_optionals_table(sub_optionals))

    return result


class ContreeCommandDirective(SphinxDirective):
    """Directive to render CLI docs for a contree command.

    Usage in MyST::

        ```{contree-command} run
        ```

    Or for global options::

        ```{contree-command} __global__
        ```
    """

    required_arguments = 1
    optional_arguments = 0
    has_content = False
    option_spec: ClassVar[dict[str, Any]] = {}

    def run(self) -> list[nodes.Node]:
        command_name = self.arguments[0].strip()

        if command_name == "__global__":
            return self._render_global()
        return self._render_command(command_name)

    def _render_global(self) -> list[nodes.Node]:
        parser = _get_parser()
        result: list[nodes.Node] = []

        optionals = _gather_optionals(parser)
        if optionals:
            result.append(_rubric("Global options"))
            result.append(
                _paragraph(
                    _text(
                        "These options apply to all commands and must appear "
                        "before the subcommand name."
                    ),
                )
            )
            result.append(_build_optionals_table(optionals))

        # Commands table
        sub_parsers = _get_subparsers(parser)
        seen: set[int] = set()

        cmd_rows: list[list[list[nodes.Node]]] = []
        for name, sub in sub_parsers.items():
            pid = id(sub)
            if pid in seen:
                continue
            seen.add(pid)

            aliases = _get_aliases(parser, name)
            alias_nodes: list[nodes.Node] = []
            for i, a in enumerate(aliases):
                if i > 0:
                    alias_nodes.append(_text(", "))
                alias_nodes.append(_literal(a))

            help_text = _get_help_text(parser, name)

            # Create a doc reference node
            ref = nodes.reference("", "", internal=True)
            ref["refuri"] = f"{name}.html"
            ref += _text(name)

            cmd_rows.append(
                [
                    [ref],
                    alias_nodes,
                    [_text(help_text)] if help_text else [],
                ]
            )

        result.append(_rubric("Commands"))
        result.append(
            _build_table(
                headers=["Command", "Aliases", "Description"],
                rows=cmd_rows,
            )
        )

        return result

    def _render_command(self, name: str) -> list[nodes.Node]:
        parser = _find_command_parser(name)
        if parser is None:
            self.state.document.reporter.warning(
                f"contree command {name!r} not found",
                line=self.lineno,
            )
            warning = nodes.warning()
            warning += _paragraph(
                _text("Command "),
                _literal(name),
                _text(" not found in parser."),
            )
            return [warning]

        result: list[nodes.Node] = []

        aliases = _get_aliases(_get_parser(), name)
        if aliases:
            alias_nodes: list[nodes.Node] = [
                nodes.strong("", "Aliases: "),
            ]
            for i, a in enumerate(aliases):
                if i > 0:
                    alias_nodes.append(_text(", "))
                alias_nodes.append(_literal(a))
            result.append(_paragraph(*alias_nodes))

        result.extend(
            _build_parser_nodes(parser, name, self.env),
        )
        return result


def setup(app: Sphinx) -> dict[str, Any]:
    app.add_directive("contree-command", ContreeCommandDirective)
    return {
        "version": "0.3",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
