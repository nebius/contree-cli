from __future__ import annotations

import pytest

from contree_cli.shell.trie import PrefixRouter


def _noop(remaining: tuple[str, ...], text: str) -> list[str]:
    return []


def _echo(remaining: tuple[str, ...], text: str) -> list[str]:
    return [text]


class TestMutableMapping:
    """PrefixRouter conforms to MutableMapping[tuple[str, ...], Handler]."""

    def test_setitem_getitem(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        assert r[("a",)] is _noop

    def test_setitem_deep(self):
        r = PrefixRouter()
        r[("a", "b", "c")] = _echo
        assert r[("a", "b", "c")] is _echo

    def test_getitem_missing_raises(self):
        r = PrefixRouter()
        with pytest.raises(KeyError):
            r[("x",)]

    def test_getitem_intermediate_raises(self):
        """Intermediate nodes without a handler raise KeyError."""
        r = PrefixRouter()
        r[("a", "b")] = _noop
        with pytest.raises(KeyError):
            r[("a",)]

    def test_delitem(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        del r[("a",)]
        with pytest.raises(KeyError):
            r[("a",)]

    def test_delitem_missing_raises(self):
        r = PrefixRouter()
        with pytest.raises(KeyError):
            del r[("x",)]

    def test_delitem_preserves_children(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        r[("a", "b")] = _echo
        del r[("a",)]
        assert r[("a", "b")] is _echo

    def test_contains(self):
        r = PrefixRouter()
        r[("a", "b")] = _noop
        assert ("a", "b") in r
        assert ("a",) not in r
        assert ("x",) not in r

    def test_contains_non_tuple(self):
        r = PrefixRouter()
        assert "not-a-tuple" not in r  # type: ignore[operator]

    def test_len(self):
        r = PrefixRouter()
        assert len(r) == 0
        r[("a",)] = _noop
        assert len(r) == 1
        r[("a", "b")] = _echo
        assert len(r) == 2
        r[("c",)] = _noop
        assert len(r) == 3

    def test_iter(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        r[("a", "b")] = _echo
        r[("c",)] = _noop
        keys = set(r)
        assert keys == {("a",), ("a", "b"), ("c",)}

    def test_overwrite(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        r[("a",)] = _echo
        assert r[("a",)] is _echo
        assert len(r) == 1


class TestValueAndChildren:
    """Properties: value and children."""

    def test_value_none_on_empty(self):
        r = PrefixRouter()
        assert r.value is None

    def test_value_set(self):
        r = PrefixRouter()
        r[()] = _noop  # root handler
        assert r.value is _noop

    def test_children_populated(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        r[("b",)] = _echo
        assert set(r.children) == {"a", "b"}

    def test_children_are_routers(self):
        r = PrefixRouter()
        r[("a", "b")] = _noop
        child = r.children["a"]
        assert isinstance(child, PrefixRouter)
        assert child.value is None
        assert child.children["b"].value is _noop


class TestResolve:
    """resolve(tokens) walks as far as possible."""

    def test_empty_tokens(self):
        r = PrefixRouter()
        node, depth = r.resolve(())
        assert node is r
        assert depth == 0

    def test_full_match(self):
        r = PrefixRouter()
        r[("a", "b")] = _noop
        node, depth = r.resolve(("a", "b"))
        assert depth == 2
        assert node.value is _noop

    def test_partial_match(self):
        r = PrefixRouter()
        r[("a", "b", "c")] = _noop
        node, depth = r.resolve(("a", "b"))
        assert depth == 2
        assert node.value is None
        assert "c" in node.children

    def test_no_match(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        node, depth = r.resolve(("x", "y"))
        assert depth == 0
        assert node is r

    def test_extra_tokens(self):
        """Tokens beyond the trie are not consumed."""
        r = PrefixRouter()
        r[("a",)] = _noop
        node, depth = r.resolve(("a", "extra", "stuff"))
        assert depth == 1
        assert node.value is _noop

    def test_resolve_with_handler_at_intermediate(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        r[("a", "b")] = _echo
        node, depth = r.resolve(("a", "b"))
        assert depth == 2
        assert node.value is _echo

    def test_resolve_stops_at_missing_child(self):
        r = PrefixRouter()
        r[("a",)] = _noop
        r[("a", "b")] = _echo
        node, depth = r.resolve(("a", "x"))
        assert depth == 1
        assert node.value is _noop
