"""Argparse introspection helpers for the dynamic shell completer.

Walks a live ``argparse.ArgumentParser`` tree to decide what the user is
typing (subcommand, flag name, flag value, positional) and records the
canonical subcommand names visited so :mod:`contree_cli.shell.argmap`
can resolve a completion source by ``(command_path, dest)``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Walking the parser tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkResult:
    parser: argparse.ArgumentParser
    consumed: int
    seen_double_dash: bool
    in_remainder: bool
    remainder_action: argparse.Action | None
    flags_seen: tuple[str, ...] = field(default_factory=tuple)
    command_path: tuple[str, ...] = field(default_factory=tuple)


def canonical_name(parser: argparse.ArgumentParser) -> str:
    """Return the canonical subcommand name a parser was registered under.

    ``argparse.add_parser("foo", aliases=["f"])`` builds one parser whose
    ``prog`` ends with the canonical ``foo``; aliases share the same parser
    object but never appear in ``prog``. Extracting the last whitespace
    separated token therefore normalises alias dispatch to a single name.
    """
    return parser.prog.rsplit(None, 1)[-1]


def walk(
    root: argparse.ArgumentParser,
    tokens: list[str],
) -> WalkResult:
    """Descend *root* through subparsers based on *tokens*.

    Stops at the first token that does not match a subparser choice.
    ``--`` and ``argparse.REMAINDER`` short-circuit the walk so later
    helpers can offer the right completion (sandbox path or nothing).
    ``command_path`` collects canonical subcommand names visited; the
    completer uses it as the lookup key in :mod:`argmap`.
    """
    parser = root
    consumed = 0
    seen_double_dash = False
    in_remainder = False
    remainder_action: argparse.Action | None = None
    flags_seen: list[str] = []
    command_path: list[str] = []

    while consumed < len(tokens):
        tok = tokens[consumed]

        if tok == "--":
            seen_double_dash = True
            consumed += 1
            continue

        if tok.startswith("-"):
            flags_seen.append(tok.split("=", 1)[0])
            consumed += 1
            continue

        sub_action = find_subparsers(parser)
        if sub_action is not None and tok in sub_action.choices:
            chosen = sub_action.choices[tok]
            assert isinstance(chosen, argparse.ArgumentParser)
            parser = chosen
            command_path.append(canonical_name(chosen))
            consumed += 1
            continue

        rem = find_remainder(parser)
        if rem is not None:
            in_remainder = True
            remainder_action = rem
            break

        consumed += 1

    return WalkResult(
        parser=parser,
        consumed=consumed,
        seen_double_dash=seen_double_dash,
        in_remainder=in_remainder,
        remainder_action=remainder_action,
        flags_seen=tuple(flags_seen),
        command_path=tuple(command_path),
    )


def find_subparsers(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def find_remainder(parser: argparse.ArgumentParser) -> argparse.Action | None:
    for action in parser._actions:
        if action.nargs == argparse.REMAINDER:
            return action
    return None


# ---------------------------------------------------------------------------
# Deciding what to complete next
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subcommand:
    action: argparse._SubParsersAction[argparse.ArgumentParser]


@dataclass(frozen=True)
class FlagName:
    parser: argparse.ArgumentParser


@dataclass(frozen=True)
class FlagValue:
    action: argparse.Action
    value_text: str


@dataclass(frozen=True)
class Positional:
    action: argparse.Action
    sticky: bool


@dataclass(frozen=True)
class End:
    pass


Target = Subcommand | FlagName | FlagValue | Positional | End


# Action classes whose presence means "no value to complete after the flag".
NO_VALUE_ACTION_NAMES: frozenset[str] = frozenset(
    {
        "_StoreTrueAction",
        "_StoreFalseAction",
        "_CountAction",
        "_StoreConstAction",
        "_AppendConstAction",
        "_HelpAction",
        "_VersionAction",
    }
)

# Flag tokens that terminate parsing.
TERMINATING_FLAGS: frozenset[str] = frozenset({"-h", "--help"})


def action_takes_value(action: argparse.Action) -> bool:
    """Return True when *action* expects a value after its flag."""
    if action.nargs == 0:
        return False
    return type(action).__name__ not in NO_VALUE_ACTION_NAMES


def find_option_action(
    parser: argparse.ArgumentParser,
    flag: str,
) -> argparse.Action | None:
    """Find the action registered for an option string, or ``None``."""
    for action in parser._actions:
        if flag in action.option_strings:
            return action
    return None


def positional_actions(
    parser: argparse.ArgumentParser,
) -> list[argparse.Action]:
    """Return non-flag, non-help, non-subparsers actions in declaration order."""
    out: list[argparse.Action] = []
    for action in parser._actions:
        if action.option_strings:
            continue
        if isinstance(action, argparse._SubParsersAction):
            continue
        out.append(action)
    return out


def positional_min_consumes(action: argparse.Action) -> int:
    """Minimum number of tokens an unfilled positional consumes."""
    nargs = action.nargs
    if nargs is None:
        return 1
    if isinstance(nargs, int):
        return nargs
    if nargs == "?":
        return 0
    if nargs == "*":
        return 0
    if nargs == "+":
        return 1
    if nargs == argparse.REMAINDER:
        return 0
    return 1


def positional_is_sticky(action: argparse.Action) -> bool:
    return action.nargs in {"*", "+", argparse.REMAINDER}


def count_positional_tokens(
    tokens: list[str],
    consumed: int,
) -> int:
    """Count non-flag tokens after *consumed* in *tokens* (excludes trailing edit)."""
    count = 0
    seen_dd = False
    i = consumed
    while i < len(tokens) - 1:
        tok = tokens[i]
        if tok == "--":
            seen_dd = True
            i += 1
            continue
        if not seen_dd and tok.startswith("-"):
            i += 1
            continue
        count += 1
        i += 1
    return count


def split_equals_flag(text: str) -> tuple[str, str] | None:
    """Split ``--flag=value`` into ``("--flag", "value")``, else ``None``."""
    if not text.startswith("-"):
        return None
    if "=" not in text:
        return None
    name, value = text.split("=", 1)
    return name, value


def next_target(
    walk_result: WalkResult,
    tokens: list[str],
    text: str,
) -> Target:
    """Decide what the user is currently completing.

    *tokens* is the full prefix list excluding the in-progress *text*. The
    walker already consumed the subparser portion; the prefix from
    ``walk_result.consumed`` onwards belongs to the active parser.
    """
    parser = walk_result.parser

    # Inside REMAINDER everything is part of the sticky positional.
    if walk_result.in_remainder and walk_result.remainder_action is not None:
        return Positional(walk_result.remainder_action, sticky=True)

    # Inline =-form flag, e.g. user typed "--use=tag:ub".
    eq = split_equals_flag(text)
    if eq is not None:
        name, value_text = eq
        action = find_option_action(parser, name)
        if action is None:
            return End()
        if not action_takes_value(action):
            return End()
        return FlagValue(action, value_text)

    # User is typing a flag name.
    if text.startswith("-") and not walk_result.seen_double_dash:
        return FlagName(parser)

    # Previous token was a flag terminator -> nothing to complete.
    prev = tokens[-1] if tokens else ""
    if prev in TERMINATING_FLAGS:
        return End()

    # Previous token was a flag expecting a value -> complete that value.
    if prev.startswith("-") and not walk_result.seen_double_dash:
        action = find_option_action(parser, prev)
        if action is not None and action_takes_value(action):
            return FlagValue(action, text)

    # Subcommand step: at the active parser, the next non-flag positional
    # might be a subparser choice.
    sub_action = find_subparsers(parser)
    if sub_action is not None:
        # If we are still at the subparser slot (none of its choices was
        # consumed), offer subcommand names.
        slot_filled = any(
            tok in sub_action.choices
            for tok in tokens[walk_result.consumed : -1]
            if not tok.startswith("-") and tok != "--"
        )
        if not slot_filled:
            return Subcommand(sub_action)

    # Positional slot: pick the first unfilled positional action.
    positional_target = pick_positional(parser, tokens, walk_result.consumed)
    if positional_target is not None:
        return positional_target

    return End()


def pick_positional(
    parser: argparse.ArgumentParser,
    tokens: list[str],
    consumed: int,
) -> Positional | None:
    """Return the next unfilled positional action, if any."""
    actions = positional_actions(parser)
    if not actions:
        return None
    filled = count_positional_tokens(tokens, consumed)
    cursor = 0
    for action in actions:
        if positional_is_sticky(action):
            return Positional(action, sticky=True)
        slot = positional_min_consumes(action)
        # nargs=None means exactly one
        slot = max(slot, 1) if action.nargs in (None, "?") and slot == 0 else slot
        if action.nargs == "?":
            slot = 1  # treat as one-slot for filling order
        if filled - cursor < slot:
            return Positional(action, sticky=False)
        cursor += slot
    return None
