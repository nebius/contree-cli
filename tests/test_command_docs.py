from __future__ import annotations

import types

from contree_cli.types import COMMAND_REGISTRY, get_command_docs


def _ensure_registered() -> None:
    if not COMMAND_REGISTRY:
        import contree_cli.arguments  # noqa: F401


def test_get_command_docs_extracts_description() -> None:
    mod = types.ModuleType("_fake_doc_mod")
    mod.__doc__ = "A helpful description."
    mod.EPILOG = "some examples"

    def fake_setup(p):  # type: ignore[no-untyped-def]
        pass

    fake_setup.__module__ = "_fake_doc_mod"

    import sys

    sys.modules["_fake_doc_mod"] = mod
    try:
        desc, epilog = get_command_docs(fake_setup)  # type: ignore[arg-type]
        assert desc == "A helpful description."
        assert epilog == "some examples"
    finally:
        del sys.modules["_fake_doc_mod"]


def test_get_command_docs_none_without_epilog() -> None:
    mod = types.ModuleType("_fake_no_epilog")
    mod.__doc__ = "Only a description."

    def fake_setup(p):  # type: ignore[no-untyped-def]
        pass

    fake_setup.__module__ = "_fake_no_epilog"

    import sys

    sys.modules["_fake_no_epilog"] = mod
    try:
        desc, epilog = get_command_docs(fake_setup)  # type: ignore[arg-type]
        assert desc == "Only a description."
        assert epilog is None
    finally:
        del sys.modules["_fake_no_epilog"]


def test_get_command_docs_missing_module() -> None:
    def fake_setup(p):  # type: ignore[no-untyped-def]
        pass

    fake_setup.__module__ = "_nonexistent_module_xyz"
    desc, epilog = get_command_docs(fake_setup)  # type: ignore[arg-type]
    assert desc is None
    assert epilog is None


def test_all_commands_have_docstrings() -> None:
    _ensure_registered()
    assert len(COMMAND_REGISTRY) > 0, "No commands registered"

    import sys

    missing: list[str] = []
    for name, _, setup_fn, _ in COMMAND_REGISTRY:
        mod = sys.modules.get(setup_fn.__module__)
        assert mod is not None, f"Module for {name!r} not loaded"
        if not mod.__doc__:
            missing.append(name)

    assert not missing, f"Commands without docstrings: {missing}"
