from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping

Handler = Callable[..., list[str]]


class PrefixRouter(MutableMapping[tuple[str, ...], Handler]):
    """Dict-of-dicts trie mapping token paths to handler callables.

    Keys are tuples of tokens, e.g. ``("contree", "session", "use")``.
    Values are handler callables receiving ``(remaining, text) -> list[str]``.

    The :meth:`resolve` method walks the trie as far as possible,
    returning the deepest matching node and how many tokens were consumed.
    """

    __slots__ = ("_children", "_handler")

    def __init__(self) -> None:
        self._handler: Handler | None = None
        self._children: dict[str, PrefixRouter] = {}

    # -- Properties -----------------------------------------------------------

    @property
    def value(self) -> Handler | None:
        """The handler stored at this node, or ``None``."""
        return self._handler

    @property
    def children(self) -> dict[str, PrefixRouter]:
        """Direct children mapping token → sub-router."""
        return self._children

    # -- MutableMapping interface ---------------------------------------------

    def __getitem__(self, key: tuple[str, ...]) -> Handler:
        node = self
        for token in key:
            try:
                node = node._children[token]
            except KeyError:
                raise KeyError(key) from None
        if node._handler is None:
            raise KeyError(key)
        return node._handler

    def __setitem__(self, key: tuple[str, ...], value: Handler) -> None:
        node = self
        for token in key:
            if token not in node._children:
                node._children[token] = PrefixRouter()
            node = node._children[token]
        node._handler = value

    def __delitem__(self, key: tuple[str, ...]) -> None:
        node = self
        for token in key:
            try:
                node = node._children[token]
            except KeyError:
                raise KeyError(key) from None
        if node._handler is None:
            raise KeyError(key)
        node._handler = None

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, tuple):
            return False
        node = self
        for token in key:
            if not isinstance(token, str):
                return False
            child = node._children.get(token)
            if child is None:
                return False
            node = child
        return node._handler is not None

    def __iter__(self) -> Iterator[tuple[str, ...]]:
        yield from self._iter_keys(())

    def _iter_keys(
        self,
        prefix: tuple[str, ...],
    ) -> Iterator[tuple[str, ...]]:
        if self._handler is not None:
            yield prefix
        for token, child in self._children.items():
            yield from child._iter_keys((*prefix, token))

    def __len__(self) -> int:
        count = 1 if self._handler is not None else 0
        for child in self._children.values():
            count += len(child)
        return count

    def resolve(self, tokens: tuple[str, ...]) -> tuple[PrefixRouter, int]:
        node = self
        depth = 0
        for token in tokens:
            child = node._children.get(token)
            if child is None:
                break
            node = child
            depth += 1
        return node, depth

    def __repr__(self) -> str:
        return (
            f"PrefixRouter(handler={self._handler is not None}, "
            f"children={list(self._children)})"
        )
