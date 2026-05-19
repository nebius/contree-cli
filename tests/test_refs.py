"""Tests for contree_cli.refs — operation-reference parsing and resolution.

This is the single source of truth for the UUID/history-reference
parsing logic. CLI handlers funnel their positional UUID arguments
through ``resolve_operation_uuids``, so testing here covers every command that
accepts operation references (op show, op cancel, op wait, session
wait, top-level show/kill).
"""

from __future__ import annotations

from contextvars import copy_context

import pytest

from contree_cli import SESSION_STORE
from contree_cli.refs import (
    history_spec_from_ref,
    looks_like_history_ref,
    resolve_operation_uuid,
    resolve_operation_uuids,
)

UUID_A = "019e3fb6-e2d8-7350-a8f9-8b2b5ebfda7f"
UUID_B = "019e3fb6-e447-760d-b7ab-62ef51f91b1f"
UUID_C = "019e3fb6-e5c3-7184-96f1-f7d56453a193"


# ----------------------------------------------------------------------
# history_spec_from_ref
# ----------------------------------------------------------------------


class TestHistorySpecFromRef:
    def test_head_alone_is_tip(self):
        assert history_spec_from_ref("HEAD") == ""

    def test_head_tilde_is_one_back(self):
        assert history_spec_from_ref("HEAD~") == "-1"

    def test_head_tilde_n_is_n_back(self):
        assert history_spec_from_ref("HEAD~3") == "-3"

    def test_head_tilde_zero_is_a_ref_but_invalid_when_resolved(self):
        # Lexically a ref (errors clearly via resolve_history_spec rather
        # than silently passing through as a bogus UUID).
        assert history_spec_from_ref("HEAD~0") == "-0"

    def test_head_tilde_garbage_is_invalid(self):
        assert history_spec_from_ref("HEAD~abc") is None

    def test_bare_at_is_tip(self):
        assert history_spec_from_ref("@") == ""

    def test_bare_colon_is_tip(self):
        assert history_spec_from_ref(":") == ""

    def test_at_n_is_absolute(self):
        assert history_spec_from_ref("@5") == "5"

    def test_colon_n_is_absolute(self):
        assert history_spec_from_ref(":12") == "12"

    def test_bare_n_is_absolute(self):
        assert history_spec_from_ref("7") == "7"

    def test_at_minus_n_is_relative_back(self):
        assert history_spec_from_ref("@-2") == "-2"

    def test_at_plus_n_is_relative_forward(self):
        assert history_spec_from_ref("@+1") == "+1"

    def test_uuid_is_not_a_ref(self):
        assert history_spec_from_ref(UUID_A) is None

    def test_garbage_is_not_a_ref(self):
        assert history_spec_from_ref("not-a-ref") is None


class TestLooksLikeHistoryRef:
    @pytest.mark.parametrize(
        "value",
        ["HEAD", "HEAD~", "HEAD~5", "@", ":", "@2", ":12", "7", "@-1", "@+3"],
    )
    def test_history_refs_are_recognised(self, value):
        assert looks_like_history_ref(value)

    @pytest.mark.parametrize("value", [UUID_A, "not-a-ref", "HEAD~abc"])
    def test_non_refs_are_rejected(self, value):
        assert not looks_like_history_ref(value)


# ----------------------------------------------------------------------
# resolve_operation_uuid -- single token; needs session_store
# ----------------------------------------------------------------------


class TestResolveOperationUuid:
    def test_uuid_passes_through(self, session_store):
        # Bare UUID -- no history-ref pattern -- returns unchanged.
        assert resolve_operation_uuid(UUID_A, session_store) == UUID_A

    def test_garbage_passes_through(self, session_store):
        # Non-ref, non-UUID strings are still passed through; the
        # caller decides what to do with them (e.g. resolve_operation_uuids
        # validates as UUID afterwards and rejects).
        assert resolve_operation_uuid("not-a-ref", session_store) == "not-a-ref"

    def test_at_prefix_resolves(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-abc")
        assert resolve_operation_uuid("@2", session_store) == "op-abc"

    def test_colon_prefix_resolves(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-def")
        assert resolve_operation_uuid(":2", session_store) == "op-def"

    def test_bare_numeric_resolves(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-ghi")
        assert resolve_operation_uuid("2", session_store) == "op-ghi"

    def test_no_session_raises(self, session_store):
        with pytest.raises(ValueError, match="No active session"):
            resolve_operation_uuid("@1", session_store)

    def test_no_operation_uuid_raises(self, session_store):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="has no operation UUID"):
            resolve_operation_uuid("@1", session_store)

    def test_nonexistent_entry_raises(self, session_store):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="not found"):
            resolve_operation_uuid("@999", session_store)

    def test_at_minus_n_walks_back_from_tip(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-prev")
        session_store.set_image("img-3", kind="run", operation_uuid="op-tip")
        assert resolve_operation_uuid("@-1", session_store) == "op-prev"

    def test_at_minus_n_exceeds_history(self, session_store):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="Cannot go back"):
            resolve_operation_uuid("@-5", session_store)

    def test_at_plus_n_walks_forward(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-next")
        session_store.rollback(1)
        assert resolve_operation_uuid("@+1", session_store) == "op-next"

    def test_at_plus_n_exceeds_children(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-x")
        with pytest.raises(ValueError, match="Cannot go forward"):
            resolve_operation_uuid("@+5", session_store)

    def test_at_zero_errors_with_clear_message(self, session_store):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="non-zero"):
            resolve_operation_uuid("@0", session_store)

    def test_colon_minus_n_resolves(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-prev")
        session_store.set_image("img-3", kind="run", operation_uuid="op-tip")
        assert resolve_operation_uuid(":-1", session_store) == "op-prev"

    def test_bare_at_resolves_to_tip(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-tip")
        assert resolve_operation_uuid("@", session_store) == "op-tip"
        assert resolve_operation_uuid(":", session_store) == "op-tip"

    def test_bare_at_with_no_op_on_tip_raises(self, session_store):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="has no operation UUID"):
            resolve_operation_uuid("@", session_store)

    def test_head_resolves_to_tip(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-tip")
        assert resolve_operation_uuid("HEAD", session_store) == "op-tip"

    def test_head_tilde_n_walks_back(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-prev")
        session_store.set_image("img-3", kind="run", operation_uuid="op-tip")
        assert resolve_operation_uuid("HEAD~1", session_store) == "op-prev"

    def test_head_tilde_alone_means_one_back(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="op-prev")
        session_store.set_image("img-3", kind="run", operation_uuid="op-tip")
        assert resolve_operation_uuid("HEAD~", session_store) == "op-prev"

    def test_head_tilde_zero_errors_with_clear_message(self, session_store):
        # `HEAD~0` is a recognised reference but semantically invalid
        # ("0 steps back"). resolve_operation_uuid surfaces the same
        # "non-zero" error users see for @0 instead of pretending it's
        # a UUID-shaped token.
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="non-zero"):
            resolve_operation_uuid("HEAD~0", session_store)

    def test_head_tilde_garbage_passes_through(self, session_store):
        assert resolve_operation_uuid("HEAD~abc", session_store) == "HEAD~abc"


# ----------------------------------------------------------------------
# resolve_operation_uuids -- the list/whitespace-aware façade used by from_args
# ----------------------------------------------------------------------


def run_resolve(items, store):
    """Invoke resolve_operation_uuids with SESSION_STORE bound, like __main__ does."""
    SESSION_STORE.set(store)
    return copy_context().run(resolve_operation_uuids, items)


class TestResolveUuids:
    def test_already_split_passes_through(self, session_store):
        assert run_resolve([UUID_A, UUID_B], session_store) == [UUID_A, UUID_B]

    def test_space_joined_single_arg_is_split(self, session_store):
        joined = f"{UUID_A} {UUID_B} {UUID_C}"
        assert run_resolve([joined], session_store) == [UUID_A, UUID_B, UUID_C]

    def test_newline_joined_is_split(self, session_store):
        joined = f"{UUID_A}\n      {UUID_B}\n      {UUID_C}"
        assert run_resolve([joined], session_store) == [UUID_A, UUID_B, UUID_C]

    def test_mixed_args_and_joined(self, session_store):
        items = [f"{UUID_A} {UUID_B}", UUID_C, f"\t{UUID_A}"]
        assert run_resolve(items, session_store) == [UUID_A, UUID_B, UUID_C, UUID_A]

    def test_empty_list(self, session_store):
        assert run_resolve([], session_store) == []

    def test_invalid_token_raises(self, session_store):
        with pytest.raises(ValueError, match="Invalid operation reference"):
            run_resolve(["not-a-uuid"], session_store)

    def test_invalid_lists_every_bad_token(self, session_store):
        with pytest.raises(ValueError) as exc:
            run_resolve([f"{UUID_A} bogus garbage {UUID_B}"], session_store)
        msg = str(exc.value)
        assert "bogus" in msg
        assert "garbage" in msg

    def test_at_n_is_resolved_to_real_uuid(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid=UUID_A)
        # `@2` should expand to UUID_A, not be passed through.
        assert run_resolve(["@2"], session_store) == [UUID_A]

    def test_head_is_resolved_to_real_uuid(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid=UUID_A)
        assert run_resolve(["HEAD"], session_store) == [UUID_A]

    def test_mixed_refs_and_uuids_are_resolved(self, session_store):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid=UUID_A)
        session_store.set_image("img-3", kind="run", operation_uuid=UUID_B)
        # HEAD -> UUID_B, HEAD~1 -> UUID_A, raw UUID_C passes through.
        result = run_resolve(["HEAD HEAD~1", UUID_C], session_store)
        assert result == [UUID_B, UUID_A, UUID_C]

    def test_head_with_no_operation_on_tip_raises(self, session_store):
        # `use` entry has no operation_uuid -- the message is propagated
        # back so the caller can see what went wrong with which token.
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="has no operation UUID"):
            run_resolve(["HEAD"], session_store)

    def test_resolved_value_must_be_a_uuid(self, session_store):
        # Defensive: if history points at a non-UUID operation_uuid (e.g.
        # legacy data), resolve_operation_uuids should flag the token rather than
        # ship a malformed UUID to the API.
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid="not-a-uuid")
        with pytest.raises(ValueError, match="Invalid operation reference"):
            run_resolve(["HEAD"], session_store)
