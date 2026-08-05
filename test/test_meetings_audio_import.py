"""Importing an existing recording into a live meeting.

Two halves, tested separately because they fail differently:

* :mod:`...domain.audio` — the pure split from one transcript blob into the lines
  ``broadcast`` expects. Every downstream consumer (dictionary, noise gate, agent
  batcher, translation queue) is per-line, so the boundary rules are the feature.
* the route — a file path arriving from a client, which means the interesting tests
  are the refusals and their ORDER, not the happy path.

No model and no audio decoder is ever reached: ``transcribe_audio`` and the
availability probe are patched in every route test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from meetings_helpers import (  # noqa: F401 — fixtures are used by name
    app_fixture,
    client_for,
    enabled_fixture,
    fake_sessions_fixture,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.domain import audio
from kiro_crew.apps.builtins.meetings.backend.routes import _common
from kiro_crew.apps.builtins.meetings.backend.routes import audio_import as ai

BASE = k.API_BASE


async def _start(client, meeting_id: str = "standup") -> None:
    await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
    resp = await client.post(f"{BASE}/meetings/{meeting_id}/start", json={})
    assert resp.status == 200, await resp.text()


def _patch(monkeypatch: pytest.MonkeyPatch, **over: Any) -> dict[str, list]:
    """Patch the route's three external dependencies. Returns a call log.

    ``transcribe_audio`` is patched on :mod:`kiro_crew.transcribe`, not on this
    module, because the route imports it INSIDE the handler (so a heavy optional
    dependency is not imported at gateway startup).
    """
    import kiro_crew.transcribe as transcribe_mod

    log: dict[str, list] = {"vetted": [], "transcribed": []}

    def _vet(raw: str) -> tuple[str, str]:
        log["vetted"].append(raw)
        return over.get("vet", (raw, ""))

    async def _transcribe(path: str, *_a: Any, **_kw: Any) -> str | None:
        log["transcribed"].append(path)
        return over.get("transcript", "we decided to ship on Friday")

    monkeypatch.setattr(ai, "_vet_audio_file", _vet)
    monkeypatch.setattr(ai, "_transcription_ready", lambda: over.get("ready", True))
    monkeypatch.setattr(transcribe_mod, "transcribe_audio", _transcribe)
    return log


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


class TestSplitTranscript:
    def _split(self, text: str, *, max_chars: int = 4000, max_lines: int = 2000) -> list[str]:
        return audio.split_transcript(text, max_chars=max_chars, max_lines=max_lines)

    def test_nothing_in_nothing_out(self):
        assert self._split("") == []
        assert self._split("   \n\n\t ") == []

    def test_prefers_the_transcribers_own_segments(self):
        """Tier 1. A whisper segment is the closest thing to "one utterance"."""
        assert self._split("first line\nsecond line\n\nthird line") == [
            "first line",
            "second line",
            "third line",
        ]

    def test_does_not_resplit_segments_on_punctuation(self):
        """A segment containing two sentences stays ONE line.

        Tier 1 wins outright: the transcriber's own boundary is better information
        than anything punctuation can reconstruct, so sentence splitting must not
        also run over it.
        """
        assert self._split("Yes. No.\nMaybe.") == ["Yes. No.", "Maybe."]

    def test_falls_back_to_sentences_for_a_single_paragraph(self):
        """Tier 2. AWS Transcribe returns one line for the whole recording."""
        assert self._split("We shipped it. Bob owns the rollback! Does that work?") == [
            "We shipped it.",
            "Bob owns the rollback!",
            "Does that work?",
        ]

    def test_a_decimal_point_is_not_a_sentence_boundary(self):
        # The lookahead requires whitespace after the mark, which is what keeps
        # "3.5" and "v1.2" intact.
        assert self._split("We picked version 1.2 and 3.5 GB of RAM.") == [
            "We picked version 1.2 and 3.5 GB of RAM.",
        ]

    def test_splits_cjk_sentence_marks(self):
        assert self._split("出荷を決めた。ロールバックは田中さんが担当。") == [
            "出荷を決めた。",
            "ロールバックは田中さんが担当。",
        ]

    def test_an_over_long_line_is_wrapped_not_truncated(self):
        """Tier 3. Truncating would silently DROP the tail of a long sentence."""
        long_line = " ".join(["word"] * 100)  # ~499 chars
        out = self._split(long_line, max_chars=50)
        assert len(out) > 1
        assert all(len(line) <= 50 for line in out)
        # Every word survives, and in order.
        assert " ".join(out).split() == long_line.split()

    def test_text_with_no_spaces_is_hard_sliced(self):
        # CJK has no word spaces, so the whitespace-preferring wrap must not loop
        # forever or give up.
        out = self._split("あ" * 120, max_chars=50)
        assert [len(line) for line in out] == [50, 50, 20]

    def test_the_line_count_is_capped(self):
        out = self._split("\n".join(f"line {i}" for i in range(50)), max_lines=10)
        assert len(out) == 10
        # The head is kept: a recording's beginning is what sets up everything after.
        assert out[0] == "line 0"

    def test_every_line_is_stripped_and_non_empty(self):
        out = self._split("  padded  \n\n\n   \n  also padded  ")
        assert out == ["padded", "also padded"]


# ---------------------------------------------------------------------------
# Refusals, and their order
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.asyncio
    async def test_no_live_meeting_is_409(self, app, monkeypatch):
        log = _patch(monkeypatch)
        async with client_for(app) as client:
            await client.post(f"{BASE}/meetings/standup/init", json={})
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "no_active_meeting"
        # And nothing was transcribed: the session check comes FIRST so an hour of
        # audio is not decoded on the way to an error we could give immediately.
        assert log["transcribed"] == []
        assert log["vetted"] == []

    @pytest.mark.asyncio
    async def test_an_expired_session_is_410_and_ends_the_meeting(
        self, app, fake_sessions, monkeypatch, root: Path
    ):
        """Shared with /dispatch through `_common.live_session`, side effects included."""
        _patch(monkeypatch)
        async with client_for(app) as client:
            await _start(client)
            session = _common.ACTIVE.get("standup")
            assert session is not None
            session.started_at -= k.MAX_SESSION_DURATION + 1

            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 410
            assert (await resp.json())["code"] == "meeting_session_expired"

            body = await (await client.get(f"{BASE}/meetings/standup")).json()
        # The expiry branch's side effect: the session is gone AND the meeting says so.
        assert _common.ACTIVE.get("standup") is None
        assert body["meta"]["status"] == k.STATUS_ENDED

    @pytest.mark.asyncio
    async def test_a_denied_path_is_403(self, app, fake_sessions, monkeypatch):
        log = _patch(monkeypatch, vet=("", "denied"))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import",
                json={"audio_path": "/home/someone/.aws/credentials"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "audio_path_denied"
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_a_missing_file_is_404(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, vet=("", "not_a_file"))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/gone.wav"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "audio_file_not_found"

    @pytest.mark.asyncio
    async def test_an_unsupported_format_is_400(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, vet=("", "unsupported_format"))
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/notes.pdf"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "audio_format_unsupported"

    @pytest.mark.asyncio
    async def test_unavailable_speech_to_text_is_503(self, app, fake_sessions, monkeypatch):
        """503, not 400: the request is fine and works once Settings is fixed."""
        log = _patch(monkeypatch, ready=False)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "transcription_unavailable"
        assert log["transcribed"] == []

    @pytest.mark.asyncio
    async def test_a_failed_transcription_is_502(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, transcript=None)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "transcription_failed"

    @pytest.mark.asyncio
    async def test_an_emptied_transcript_is_also_502(self, app, fake_sessions, monkeypatch):
        """The hallucination filter returns "" for a transcript that was all boilerplate.

        Reporting success with zero lines would put "Thanks for watching!" — or
        nothing at all — in front of the user as a completed import.
        """
        _patch(monkeypatch, transcript="")
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 502

    @pytest.mark.asyncio
    async def test_a_missing_path_is_400(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch)
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(f"{BASE}/meetings/standup/import", json={})
            assert resp.status == 400


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestImport:
    @pytest.mark.asyncio
    async def test_the_transcript_reaches_every_unmuted_agent(
        self, app, fake_sessions, monkeypatch
    ):
        """The point of routing through `broadcast`: agents, not a transcript file."""
        _patch(monkeypatch, transcript="we shipped it\nBob owns the rollback")
        async with client_for(app) as client:
            await _start(client)
            resp = await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            assert resp.status == 200
            body = await resp.json()

            assert body["lines"] == 2
            assert body["dispatched"] == 2
            # Read INSIDE the client block: the app's `on_cleanup` hook drains and
            # clears the active session, so the queues are gone once it exits.
            session = _common.ACTIVE.get("standup")
            assert session is not None
            for queue in session.agents.values():
                assert "we shipped it" in queue.queue
                assert "Bob owns the rollback" in queue.queue

    @pytest.mark.asyncio
    async def test_the_domain_dictionary_corrects_an_imported_line(
        self, app, fake_sessions, monkeypatch
    ):
        """Imported text goes through the SAME pipeline as speech, corrections included.

        This is the whole argument for dispatching rather than storing: nothing had
        to be re-implemented for import, and nothing can drift.
        """
        from kiro_crew.apps.builtins.meetings.backend.domain import session as sess

        sess.shared_dictionary().load_terms(
            [{"correct": "KiroCrew", "aliases": ["kiro crew"]}]
        )
        _patch(monkeypatch, transcript="we shipped kiro crew today")
        async with client_for(app) as client:
            await _start(client)
            await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            session = _common.ACTIVE.get("standup")
            assert session is not None
            queued = next(iter(session.agents.values())).queue
            assert any("KiroCrew" in line for line in queued)

    @pytest.mark.asyncio
    async def test_a_muted_agent_is_skipped(self, app, fake_sessions, monkeypatch):
        _patch(monkeypatch, transcript="one line")
        async with client_for(app) as client:
            await _start(client)
            await client.post(
                f"{BASE}/meetings/standup/mute",
                json={"agent_id": "note-taker", "listening": False},
            )
            await client.post(
                f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
            )
            session = _common.ACTIVE.get("standup")
            assert session is not None
            assert session.agents["note-taker"].queue == []
            assert session.agents["sketch-artist"].queue == ["one line"]

    @pytest.mark.asyncio
    async def test_lines_and_dispatched_are_reported_separately(
        self, app, fake_sessions, monkeypatch
    ):
        """The gap between them is what the noise gate dropped.

        A recording that yields lines of which NONE were dispatched is a real
        outcome — an empty room, filler — and must be visible rather than reported
        as a clean success.
        """
        _patch(monkeypatch, transcript="uh\num\nuh")
        async with client_for(app) as client:
            await _start(client)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/import", json={"audio_path": "/tmp/a.wav"}
                )
            ).json()
        assert body["lines"] == 3
        assert body["dispatched"] == 0

    @pytest.mark.asyncio
    async def test_the_canonical_path_is_reported_not_the_clients(
        self, app, fake_sessions, monkeypatch
    ):
        _patch(monkeypatch, vet=("/canonical/a.wav", ""))
        async with client_for(app) as client:
            await _start(client)
            body = await (
                await client.post(
                    f"{BASE}/meetings/standup/import",
                    json={"audio_path": "~/link-to-a.wav"},
                )
            ).json()
        assert body["path"] == "/canonical/a.wav"


# ---------------------------------------------------------------------------
# The path barrier
# ---------------------------------------------------------------------------


class TestPathBarrier:
    def test_the_shared_gate_is_used_and_the_predicate_is_not(self):
        """``validate_file_path``, never ``is_sensitive_path`` directly.

        Using the gate is what makes this route's answer identical to every other
        file read in the product; calling the predicate here would be a second
        opinion that can drift from it.
        """
        import inspect

        src = inspect.getsource(ai)
        assert "validate_file_path(" in src
        # The CALL, not the word — the module docstring names the predicate when it
        # explains what the gate enforces, and that prose is the point.
        assert "is_sensitive_path(" not in src

    def test_the_extension_is_checked_on_the_canonical_path(self, tmp_path: Path):
        """A symlink named ``.mp3`` must not smuggle in its target.

        ``validate_file_path`` resolves symlinks, and the suffix test runs on the
        RESULT — so the name the client chose is never what is checked.
        """
        target = tmp_path / "secret.pdf"
        target.write_bytes(b"%PDF-1.4")
        link = tmp_path / "innocent.mp3"
        link.symlink_to(target)

        canonical, reason = ai._vet_audio_file(str(link))
        assert canonical == ""
        assert reason == "unsupported_format"

    def test_a_real_audio_file_passes(self, tmp_path: Path):
        wav = tmp_path / "meeting.wav"
        wav.write_bytes(b"RIFF....WAVE")
        canonical, reason = ai._vet_audio_file(str(wav))
        assert reason == ""
        assert canonical == str(wav.resolve())

    def test_a_directory_is_not_a_file(self, tmp_path: Path):
        d = tmp_path / "recordings.wav"
        d.mkdir()
        assert ai._vet_audio_file(str(d)) == ("", "not_a_file")

    def test_every_accepted_extension_is_lowercase_and_dotted(self):
        # The check lowercases the suffix, so an uppercase entry here would be dead.
        for ext in k.IMPORT_AUDIO_EXTENSIONS:
            assert ext == ext.lower()
            assert ext.startswith(".")

    def test_an_uppercase_suffix_is_still_accepted(self, tmp_path: Path):
        wav = tmp_path / "MEETING.WAV"
        wav.write_bytes(b"RIFF....WAVE")
        assert ai._vet_audio_file(str(wav))[1] == ""


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    def test_the_route_is_registered(self):
        from aiohttp import web

        from kiro_crew.apps.builtins.meetings.backend.routes import register_routes

        app = web.Application()
        register_routes(app)
        assert any(
            route.method == "POST"
            and route.resource is not None
            and route.resource.canonical == f"{BASE}/meetings/{{meeting_id}}/import"
            for route in app.router.routes()
        )

    def test_both_producers_share_the_session_helper(self):
        """One copy of the expiry side effects, not two."""
        import inspect

        from kiro_crew.apps.builtins.meetings.backend.routes import agents as ag

        assert "live_session(" in inspect.getsource(ag.handle_dispatch_text)
        assert "live_session(" in inspect.getsource(ai.handle_import_audio)
        # And the side effects live in exactly one place.
        helper = inspect.getsource(_common.live_session)
        assert "drain_and_clear" in helper
        assert "end_meeting_meta" in helper

    def test_the_handler_does_no_blocking_io_inline(self):
        import inspect

        src = inspect.getsource(ai.handle_import_audio)
        assert "asyncio.to_thread" in src
        # The blocking work lives in the helpers the thread runs.
        assert "validate_file_path(" not in src
        assert "is_available(" not in src

    def test_transcribe_is_imported_lazily(self):
        """Not at module import: the STT stack pulls optional heavy dependencies.

        A gateway that registers this app must not pay for a decoder nobody asked
        for, and `faster-whisper` is deliberately not a declared extra.
        """
        import inspect

        header = inspect.getsource(ai).split("logger = ")[0]
        assert "from kiro_crew.transcribe import" not in header
