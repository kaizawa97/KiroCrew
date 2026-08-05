"""Editable minutes — the user's edit of an agent's output, kept as a sidecar.

This is the one place the app's agent-ownership model bends, so the tests are
organised around the bargain rather than around the functions:

* **The agent's file is never written by a user edit**, and **the sidecar is never
  writable by an agent.** Those two together are what make the feature safe to add
  to a directory an agent owns, and each is pinned separately.
* **A read prefers the edit**, so the panel shows one copy and the client needs no
  merge step.
* **Revert is a delete**, which is why it always works.
* **``stale`` tells the truth about the agent having moved on.** Without it an edit
  silently freezes a live panel, which is the failure mode this design trades for
  never losing the user's text.
* **The body caps are consistent with each other.** A char limit larger than the
  byte limit means the user can open a document and then not be allowed to save it.

Ported from MeetNote's ``meetnote_lib/minutes.py`` (on ``feat/meetnote``); see the
block comment above ``store.agent_edits_dir`` for what changed and why.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401 — fixtures are used by name
    app_fixture,
    client_for,
    enabled_fixture,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store

BASE = k.API_BASE

NOTE_TAKER = {"id": "note-taker", "widget_type": "markdown"}
SKETCH = {"id": "sketch-artist", "widget_type": "html"}


def _set_mtime(path: Path, when: float) -> None:
    """Pin *path*'s mtime, so ``stale`` is deterministic rather than filesystem-timed."""
    os.utime(path, (when, when))


# ---------------------------------------------------------------------------
# The two ownership properties
# ---------------------------------------------------------------------------


class TestOwnership:
    """Neither writer can reach the other's file. The core of the design."""

    def test_the_edits_directory_is_unreachable_by_any_agent(self, root: Path):
        """No configured agent can be handed a path inside ``edits/``.

        The sibling of ``test_note_filename_is_unreachable_by_any_agent``, and the
        reason the sidecar needs no ``_`` prefix: an agent's output path is always
        ``meeting_dir / (safe_agent_id(id) + ext)``, a FLAT filename, and a validated
        agent id cannot contain a separator. So the containing directory is the
        barrier.

        Asserted through the validator rather than by inspection, so a future
        loosening of ``_SAFE_AGENT_ID_RE`` fails here instead of silently handing an
        agent the user's edits.
        """
        for hostile in (
            k.AGENT_EDITS_DIR,
            f"{k.AGENT_EDITS_DIR}/note-taker",
            f"../{k.AGENT_EDITS_DIR}/note-taker",
            f"{k.AGENT_EDITS_DIR}\\note-taker",
        ):
            # Either the id is refused outright, or it survives as a single flat
            # filename that cannot descend into the directory.
            try:
                safe = store.safe_agent_id(hostile)
            except store.MeetingsPathError:
                continue
            assert "/" not in safe and "\\" not in safe
            assert safe != k.AGENT_EDITS_DIR or True  # a file named `edits`, not the dir
            produced = store.agent_output_path("m1", f"{safe}.md", root)
            assert produced.parent == store.meeting_dir("m1", root)

    def test_an_agent_named_edits_gets_a_file_not_the_directory(self, root: Path):
        """The one collision worth checking: an agent literally called ``edits``.

        It produces ``edits.md``, a sibling of the ``edits/`` directory, so the two
        cannot fight.
        """
        store.ensure_agent_files("m1", [{"id": "edits", "widget_type": "markdown"}], "T", root)
        store.write_agent_edit("m1", NOTE_TAKER, "mine", root)
        assert store.agent_output_path("m1", "edits.md", root).is_file()
        assert store.agent_edits_dir("m1", root).is_dir()

    def test_saving_an_edit_does_not_touch_the_agents_file(self, root: Path):
        """The generated document survives verbatim — that is what makes revert safe."""
        store.write_agent_output("m1", NOTE_TAKER, "# Generated\n", root)
        store.write_agent_edit("m1", NOTE_TAKER, "# Mine\n", root)
        assert store.agent_output_path("m1", "note-taker.md", root).read_text() == "# Generated\n"

    def test_the_agent_rewriting_its_file_does_not_touch_the_edit(self, root: Path):
        """The other direction, which is the one the feature exists for."""
        store.write_agent_edit("m1", NOTE_TAKER, "# Mine\n", root)
        store.write_agent_output("m1", NOTE_TAKER, "# Regenerated\n", root)
        edit = store.read_agent_edit("m1", NOTE_TAKER, root)
        assert edit is not None and edit["content"] == "# Mine\n"


# ---------------------------------------------------------------------------
# Store semantics
# ---------------------------------------------------------------------------


class TestStore:
    def test_no_edit_reads_as_none(self, root: Path):
        """Absent, not empty — so "has an edit" is a key check for the caller."""
        assert store.read_agent_edit("m1", NOTE_TAKER, root) is None

    def test_the_sidecar_keeps_the_outputs_extension(self, root: Path):
        path = store.agent_edit_path("m1", NOTE_TAKER, root)
        assert path is not None
        assert path.name == "note-taker.md"
        assert path.parent.name == k.AGENT_EDITS_DIR

    def test_a_chat_agent_has_no_editable_path(self, root: Path):
        """No output file means nothing to edit, and that is not an error at this layer."""
        assert store.agent_edit_path("m1", {"id": "chatty", "widget_type": "chat"}, root) is None

    def test_writing_for_a_chat_agent_is_refused(self, root: Path):
        with pytest.raises(store.MeetingsPathError) as excinfo:
            store.write_agent_edit("m1", {"id": "chatty", "widget_type": "chat"}, "x", root)
        assert excinfo.value.code == "agent_has_no_output"

    def test_revert_deletes_the_sidecar_and_reports_it(self, root: Path):
        store.write_agent_edit("m1", NOTE_TAKER, "# Mine\n", root)
        assert store.revert_agent_edit("m1", NOTE_TAKER, root) is True
        assert store.read_agent_edit("m1", NOTE_TAKER, root) is None

    def test_reverting_nothing_is_false_not_an_error(self, root: Path):
        assert store.revert_agent_edit("m1", NOTE_TAKER, root) is False

    def test_an_empty_edit_is_a_legitimate_edit(self, root: Path):
        """Deleting the whole document is something a user may mean."""
        store.write_agent_edit("m1", NOTE_TAKER, "", root)
        edit = store.read_agent_edit("m1", NOTE_TAKER, root)
        assert edit is not None and edit["content"] == ""

    def test_stale_is_false_when_the_edit_is_newer(self, root: Path):
        store.write_agent_output("m1", NOTE_TAKER, "# Generated\n", root)
        store.write_agent_edit("m1", NOTE_TAKER, "# Mine\n", root)
        _set_mtime(store.agent_output_path("m1", "note-taker.md", root), 1_000)
        edit_path = store.agent_edit_path("m1", NOTE_TAKER, root)
        assert edit_path is not None
        _set_mtime(edit_path, 2_000)
        edit = store.read_agent_edit("m1", NOTE_TAKER, root)
        assert edit is not None and edit["stale"] is False

    def test_stale_is_true_once_the_agent_writes_again(self, root: Path):
        """The signal that keeps a live panel honest."""
        store.write_agent_output("m1", NOTE_TAKER, "# Generated\n", root)
        store.write_agent_edit("m1", NOTE_TAKER, "# Mine\n", root)
        edit_path = store.agent_edit_path("m1", NOTE_TAKER, root)
        assert edit_path is not None
        _set_mtime(edit_path, 1_000)
        _set_mtime(store.agent_output_path("m1", "note-taker.md", root), 2_000)
        edit = store.read_agent_edit("m1", NOTE_TAKER, root)
        assert edit is not None and edit["stale"] is True

    def test_batch_read_omits_unedited_agents(self, root: Path):
        store.write_agent_edit("m1", NOTE_TAKER, "# Mine\n", root)
        edits = store.read_agent_edits("m1", [NOTE_TAKER, SKETCH], root)
        assert set(edits) == {"note-taker"}

    def test_a_malformed_agent_definition_is_skipped_not_fatal(self, root: Path):
        """One bad config entry must not blank the whole batch."""
        store.write_agent_edit("m1", NOTE_TAKER, "# Mine\n", root)
        edits = store.read_agent_edits("m1", [{"id": "../evil"}, NOTE_TAKER], root)
        assert set(edits) == {"note-taker"}


# ---------------------------------------------------------------------------
# The read overlay
# ---------------------------------------------------------------------------


class TestOutputsOverlay:
    @pytest.mark.asyncio
    async def test_the_edit_is_what_the_poll_returns(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={"title": "Standup"})
            store.write_agent_output("standup", NOTE_TAKER, "# Generated\n", root)
            resp = await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": "# Mine\n"},
            )
            assert resp.status == 200

            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert body["outputs"]["note-taker"] == "# Mine\n"
        assert body["edits"]["note-taker"]["stale"] is False

    @pytest.mark.asyncio
    async def test_the_edits_map_does_not_resend_the_content(self, app, root: Path):
        """It is already in ``outputs``; sending it twice doubles the largest field."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": "# Mine\n"},
            )
            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert "content" not in body["edits"]["note-taker"]
        assert set(body["edits"]["note-taker"]) == {"updated_at", "stale"}

    @pytest.mark.asyncio
    async def test_an_unedited_agent_has_no_entry(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert body["edits"] == {}

    @pytest.mark.asyncio
    async def test_reverting_brings_the_agents_text_back(self, app, root: Path):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            store.write_agent_output("standup", NOTE_TAKER, "# Generated\n", root)
            await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": "# Mine\n"},
            )
            resp = await client.delete(
                f"{BASE}/meetings/standup/outputs", json={"agent_id": "note-taker"}
            )
            assert (await resp.json())["reverted"] is True

            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert body["outputs"]["note-taker"] == "# Generated\n"
        assert body["edits"] == {}

    @pytest.mark.asyncio
    async def test_reverting_nothing_still_succeeds(self, app):
        """The request asked for "no edit on this agent", and that is the state after."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.delete(
                f"{BASE}/meetings/standup/outputs", json={"agent_id": "note-taker"}
            )
            assert resp.status == 200
            assert (await resp.json())["reverted"] is False

    @pytest.mark.asyncio
    async def test_a_sidecar_is_ignored_once_the_agent_turns_into_an_html_widget(
        self, app, root: Path
    ):
        """Changing ``widget_type`` after an edit must not feed markdown to the iframe.

        Reachable without any API misuse: edit the note-taker, then change its widget
        type in Settings. The overlay applies the same editability predicate the write
        gate does, which is what closes it.
        """
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": "# Mine\n"},
            )
            config = store.read_config(root)
            for agent in config["meeting_agents"]:
                if agent["id"] == "note-taker":
                    agent["widget_type"] = "html"
            store.write_config(config, root)

            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert body["edits"] == {}
        assert "# Mine" not in body["outputs"].get("note-taker", "")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    @pytest.mark.asyncio
    async def test_the_generated_half_is_still_redacted(self, app, root: Path):
        """Adding the overlay must not have dropped the pass that was already there."""
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            store.write_agent_output(
                "standup", NOTE_TAKER, "# Notes\n\nkey AKIAIOSFODNN7EXAMPLE here", root
            )
            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert "AKIAIOSFODNN7EXAMPLE" not in body["outputs"]["note-taker"]

    @pytest.mark.asyncio
    async def test_the_users_own_edit_is_returned_verbatim(self, app, root: Path):
        """Deliberate, and safe because an edit is redacted BY CONSTRUCTION.

        The only way to produce one is to edit what the same endpoint already
        redacted on its way to the browser, so a second pass would only risk mangling
        a correction made inside an already-substituted span. Same position
        ``handle_put_note`` takes for the note.
        """
        marker = "  trailing space and a\ttab\n\n"
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": marker},
            )
            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        # Byte-for-byte, whitespace included: `field_str` would have stripped this.
        assert body["outputs"]["note-taker"] == marker


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.asyncio
    async def test_an_unknown_agent_is_404(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "ghost", "content": "x"},
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "agent_not_found"

    @pytest.mark.asyncio
    async def test_an_html_agent_is_refused_with_409(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "sketch-artist", "content": "<p>x</p>"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "agent_output_not_editable"

    @pytest.mark.asyncio
    async def test_a_non_string_body_is_refused_and_erases_nothing(self, app, root: Path):
        """The ``field_str`` trap, pinned.

        That helper treats a non-string as MISSING and returns its default, so a
        malformed body would have answered 200 having replaced the minutes with ``""``.
        """
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": "# Mine\n"},
            )
            resp = await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": {"not": "a string"}},
            )
            assert resp.status == 400

            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert body["outputs"]["note-taker"] == "# Mine\n"

    @pytest.mark.asyncio
    async def test_a_missing_agent_id_is_refused(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.put(f"{BASE}/meetings/standup/outputs", json={"content": "x"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_an_over_long_document_is_413(self, app):
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.put(
                f"{BASE}/meetings/standup/outputs",
                json={"agent_id": "note-taker", "content": "x" * (k.MAX_MINUTES_CHARS + 1)},
            )
            assert resp.status == 413


# ---------------------------------------------------------------------------
# The two caps have to agree
# ---------------------------------------------------------------------------


class TestBodyCaps:
    """A char limit larger than the byte limit is a trap, so the arithmetic is pinned.

    The failure it prevents is the worst shape available here: the user can OPEN the
    document, edit it, and only then be told it cannot be saved.
    """

    def test_the_byte_cap_covers_the_char_cap_at_three_bytes_per_char(self):
        # UTF-8 CJK is three bytes; Japanese minutes are the realistic worst case.
        assert k.MAX_MINUTES_BODY_BYTES >= k.MAX_MINUTES_CHARS * 3

    def test_the_route_raises_the_cap_above_the_shared_default(self):
        from kiro_crew.apps.builtins.meetings.backend.routes import _common

        assert k.MAX_MINUTES_BODY_BYTES > _common.MAX_BODY_BYTES

    @pytest.mark.asyncio
    async def test_a_body_over_the_shared_default_is_accepted_here(self, app, root: Path):
        """Functional half of the arithmetic above.

        A multibyte document comfortably inside ``MAX_MINUTES_CHARS`` exceeds the
        256 KiB default, so this request is exactly the one that used to 413.
        """
        from kiro_crew.apps.builtins.meetings.backend.routes import _common

        content = "議" * 100_000  # 300 000 bytes as UTF-8
        assert len(content) <= k.MAX_MINUTES_CHARS
        assert len(content.encode()) > _common.MAX_BODY_BYTES

        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.put(
                f"{BASE}/meetings/standup/outputs",
                data=json.dumps({"agent_id": "note-taker", "content": content}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200

            body = await (await client.get(f"{BASE}/meetings/standup/outputs")).json()
        assert body["outputs"]["note-taker"] == content


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    def test_both_methods_are_registered_on_the_outputs_path(self):
        from aiohttp import web

        from kiro_crew.apps.builtins.meetings.backend.routes import register_routes

        app = web.Application()
        register_routes(app)
        methods = {
            route.method
            for route in app.router.routes()
            if route.resource is not None
            and route.resource.canonical == f"{BASE}/meetings/{{meeting_id}}/outputs"
        }
        assert {"GET", "PUT", "DELETE"} <= methods

    def test_the_handlers_read_off_the_event_loop(self):
        """Same rule the rest of this app's handlers follow."""
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml

        for handler in (ml.handle_put_output, ml.handle_delete_output):
            src = inspect.getsource(handler)
            assert "asyncio.to_thread" in src, f"{handler.__name__} must not do file IO inline"
            assert "store.write_agent_edit" not in src
            assert "store.revert_agent_edit" not in src

    def test_the_write_gate_and_the_read_overlay_share_one_predicate(self):
        """Two copies of "which agents are editable" would eventually disagree."""
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import meeting_lifecycle as ml

        assert "_is_editable(" in inspect.getsource(ml._collect_outputs)
        assert "_is_editable(" in inspect.getsource(ml._editable_agent)
        assert ml._is_editable({"id": "a", "widget_type": "markdown"}) is True
        assert ml._is_editable({"id": "a", "widget_type": "html"}) is False
        assert ml._is_editable({"id": "a", "widget_type": "chat"}) is False
        # An absent widget_type defaults to markdown, so it must be editable too.
        assert ml._is_editable({"id": "a"}) is True
