from __future__ import annotations

import pytest

from contree_cli.docker.dockerignore import (
    is_ignored,
    may_include_descendants,
    parse_dockerignore,
    pattern_to_regex,
)
from contree_cli.docker.local_context import LocalContext


class TestPatternToRegex:
    @pytest.mark.parametrize(
        "pattern,subject,expected",
        [
            ("foo", "foo", True),
            ("foo", "bar", False),
            ("foo", "foo/bar", False),  # bare pattern, no subpath match
            ("foo/", "foo/bar", True),
            ("foo/", "foo", True),  # trailing slash also matches the bare dir name
            ("*.log", "x.log", True),
            ("*.log", "sub/x.log", False),  # * does not cross /
            ("**/*.log", "x.log", True),
            ("**/*.log", "a/x.log", True),
            ("**/*.log", "a/b/x.log", True),
            ("src/**", "src/a/b.c", True),
            ("src/**", "src/a.txt", True),
            ("a/**/b", "a/b", True),
            ("a/**/b", "a/x/b", True),
            ("a/**/b", "a/x/y/b", True),
            ("a?b", "axb", True),
            ("a?b", "ab", False),
            ("file[12]", "file1", True),
            ("file[12]", "file3", False),
        ],
    )
    def test_matches(self, pattern, subject, expected):
        import re

        regex = pattern_to_regex(pattern)
        assert bool(re.fullmatch(regex, subject)) is expected


class TestParseDockerignore:
    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_dockerignore(tmp_path) == ()

    def test_comments_and_blank_lines_skipped(self, tmp_path):
        (tmp_path / ".dockerignore").write_text(
            "# header\n\nfoo\n  # indented comment kept as literal? no\n!bar\n"
        )
        rules = parse_dockerignore(tmp_path)
        # "  # indented" is not a comment in Docker; we follow simple lstrip
        # check then startswith #. After strip, it becomes "# indented comment..."
        # which IS treated as a comment. Verify behaviour.
        assert len(rules) == 2
        assert rules[0].raw.strip() == "foo"
        assert rules[1].negate is True

    def test_negation_then_match(self, tmp_path):
        (tmp_path / ".dockerignore").write_text("*.log\n!keep.log\n")
        rules = parse_dockerignore(tmp_path)
        assert is_ignored("x.log", rules) is True
        assert is_ignored("keep.log", rules) is False

    def test_match_order_last_wins(self, tmp_path):
        (tmp_path / ".dockerignore").write_text("!keep.log\n*.log\n")
        rules = parse_dockerignore(tmp_path)
        # *.log comes after !keep.log so keep.log gets re-ignored
        assert is_ignored("keep.log", rules) is True

    @pytest.mark.parametrize("parent_pattern", ["assets", "assets/"])
    def test_later_exception_overrides_ignored_parent(self, tmp_path, parent_pattern):
        (tmp_path / ".dockerignore").write_text(f"{parent_pattern}\n!assets/keep.txt\n")
        rules = parse_dockerignore(tmp_path)

        assert is_ignored("assets/keep.txt", rules) is False
        assert is_ignored("assets/drop.txt", rules) is True
        assert is_ignored("assets", rules) is True

    def test_later_parent_rule_overrides_file_exception(self, tmp_path):
        (tmp_path / ".dockerignore").write_text("!assets/keep.txt\nassets\n")
        rules = parse_dockerignore(tmp_path)

        assert is_ignored("assets/keep.txt", rules) is True

    def test_reincluded_parent_applies_to_descendants(self, tmp_path):
        (tmp_path / ".dockerignore").write_text("assets\n!assets/public\n")
        rules = parse_dockerignore(tmp_path)

        assert is_ignored("assets/public/keep.txt", rules) is False
        assert is_ignored("assets/private/drop.txt", rules) is True


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        ("tree/keep.log", False),
        ("tree/cache*/keep.txt", False),
        ("tree/volume-data/keep.txt", True),
        ("tree/*-data/keep.txt", True),
        ("tree/vol?me-data/keep.txt", True),
        ("tree/[cv]olume-data/keep.txt", True),
        ("tree/[cx]olume-data/keep.txt", False),
        ("tree/**/keep.txt", True),
        ("**/keep.txt", True),
    ],
)
def test_descendant_exception_must_reach_the_ignored_directory(
    tmp_path, exception, expected
):
    (tmp_path / ".dockerignore").write_text(f"tree/volume-data/\n!{exception}\n")
    rules = parse_dockerignore(tmp_path)

    assert may_include_descendants("tree/volume-data", rules) is expected


def test_parent_exclusion_overrides_an_earlier_descendant_exception(tmp_path):
    (tmp_path / ".dockerignore").write_text(
        "!tree/volume-data/keep.txt\ntree/volume-data/\n"
    )
    rules = parse_dockerignore(tmp_path)

    assert not may_include_descendants("tree/volume-data", rules)


def test_character_class_crossing_separator_is_not_pruned(tmp_path):
    (tmp_path / ".dockerignore").write_text("tree/a/\n!tree/[a][b/c]keep.txt\n")
    rules = parse_dockerignore(tmp_path)

    assert not is_ignored("tree/a/keep.txt", rules)
    assert may_include_descendants("tree/a", rules)


class TestLocalContextIgnore:
    def test_dockerignore_filters_dir_walk(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("ok")
        (tmp_path / "src" / "ignore.log").write_text("nope")
        (tmp_path / ".dockerignore").write_text("**/*.log\n")

        local = LocalContext.from_dir(tmp_path)
        mapped = local.collect(("src",), "/app", uid=0, gid=0, mode_override=None)
        paths = sorted(m.instance_path for m in mapped)
        assert paths == ["/app/app.py"]

    def test_dockerignore_blocks_file_source(self, tmp_path):
        (tmp_path / "secret.env").write_text("token=hi")
        (tmp_path / ".dockerignore").write_text("*.env\n")

        local = LocalContext.from_dir(tmp_path)
        mapped = local.collect(
            ("secret.env",), "/app.env", uid=0, gid=0, mode_override=None
        )
        assert mapped == []

    def test_default_excludes_still_apply(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x")
        (tmp_path / "app.py").write_text("ok")

        local = LocalContext.from_dir(tmp_path)
        mapped = local.collect((".",), "/app/", uid=0, gid=0, mode_override=None)
        paths = sorted(m.instance_path for m in mapped)
        # .git filtered out by DEFAULT_FILE_EXCLUDES
        assert all(".git" not in p for p in paths)
        assert "/app/app.py" in paths

    def test_negation_reincludes(self, tmp_path):
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "keep.log").write_text("k")
        (tmp_path / "logs" / "junk.log").write_text("j")
        (tmp_path / ".dockerignore").write_text("logs/*.log\n!logs/keep.log\n")

        local = LocalContext.from_dir(tmp_path)
        mapped = local.collect(("logs",), "/dest", uid=0, gid=0, mode_override=None)
        paths = sorted(m.instance_path for m in mapped)
        assert paths == ["/dest/keep.log"]
