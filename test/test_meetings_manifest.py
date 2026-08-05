"""The Meetings manifest and its bundled skill.

These are the two files that describe the app to something OTHER than the code — the
App Store reads the manifest, and an agent working in a meeting folder reads the
skill — so they drift silently. Nothing fails when `app.json` under-declares an
endpoint or when the skill omits a file an agent should leave alone; the cost lands on
a user or on someone's notes instead.

So each assertion here ties a document back to a fact in the code.
"""

from __future__ import annotations

import json
from pathlib import Path

APP_DIR = Path("src/kiro_crew/apps/builtins/meetings")
MANIFEST = APP_DIR / "app.json"
SKILL = Path("src/kiro_crew/builtin_skills/meetings/SKILL.md")
FRONTEND = Path("website/src/apps/meetings")
DISPLAY_MEDIA = Path("website/electron/display-media.js")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _frontend_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FRONTEND.rglob("*.ts*"))
    )


class TestDeclaredApiSurface:
    """`permissions.api` must name every endpoint outside this app's own base path.

    It under-declared two of them for a while, and both arrived with a feature rather
    than by accident: `/api/ws/recording` (the recording socket, which is core's, not
    this app's) and `/api/file-raw` (how a pasted note image is served, reusing the
    hardened route instead of adding a second one). Nothing broke, which is exactly
    the problem — the manifest is a disclosure, and an incomplete one is worse than
    none.
    """

    #: Endpoints the app uses that are NOT under `/api/apps/meetings`, and the reason.
    _FOREIGN_ENDPOINTS = {
        "/api/ws/stt": "streaming speech-to-text",
        "/api/ws/recording": "core's recording socket",
        "/api/file-raw": "serving a note image through the hardened file route",
    }

    def test_every_foreign_endpoint_is_declared(self):
        declared = set(_manifest()["permissions"]["api"])
        missing = {
            endpoint: why
            for endpoint, why in self._FOREIGN_ENDPOINTS.items()
            if endpoint not in declared
        }
        assert not missing, f"permissions.api does not declare: {missing}"

    def test_the_two_sockets_are_opened_by_this_app(self):
        """The inverse direction: a declaration nobody uses is a stale claim."""
        source = _frontend_source()
        for endpoint in ("/api/ws/stt", "/api/ws/recording"):
            assert endpoint in source, f"{endpoint} is declared but unused"

    def test_file_raw_is_declared_because_the_note_renderer_reaches_it(self):
        """`/api/file-raw` is used INDIRECTLY, and the indirection is the design.

        This app adds no file-serving route of its own. The note supplies its
        absolute path as a ``BasePathCtx``, and the dashboard's shared
        ``MarkdownRenderer`` is what rewrites a relative ``<img src>`` into
        ``/api/file-raw?path=…`` — a route already hardened (content type from magic
        bytes, ``O_NOFOLLOW``, ``nosniff``). So the endpoint never appears in this
        app's source, and it still has to be declared: the app depends on it.

        Asserting the mechanism rather than a string is what keeps the declaration
        honest if either half moves.
        """
        assert "BasePathCtx" in _frontend_source(), "the note no longer provides a base path"
        renderer = Path("website/src/components/MarkdownRenderer.tsx").read_text(encoding="utf-8")
        assert "/api/file-raw?path=" in renderer

    def test_the_declared_list_is_clean(self):
        declared = _manifest()["permissions"]["api"]
        assert len(declared) == len(set(declared)), "duplicate entry in permissions.api"
        assert all(entry.startswith("/") for entry in declared)

    def test_the_apps_own_base_path_is_still_declared(self):
        declared = set(_manifest()["permissions"]["api"])
        assert {"/api/apps/meetings", "/api/apps/meetings/*"} <= declared


class TestSkillNamesWhatAgentsMustNotTouch:
    """The skill is loaded into a meeting agent's context, so omissions have teeth.

    An agent has file tools. The app never HANDS it a path inside `edits/`, `images/`
    or `_note.md` — that containment is asserted in `test_meetings_minutes.py` and
    `test_meetings_routes.py` — but containment only governs paths the app builds. An
    agent that decides to tidy its own directory is a different failure, and the only
    control for it is telling the agent these files are not its business.
    """

    def _skill(self) -> str:
        return SKILL.read_text(encoding="utf-8")

    def test_the_user_owned_paths_are_named(self):
        skill = self._skill()
        for path in ("_note.md", "images/", "edits/"):
            assert path in skill, f"the skill does not mention {path}"

    def test_the_instruction_is_do_not_write(self):
        # Whitespace-normalized: the prose is wrapped and markdown-emphasised, so the
        # phrase spans a line break and a `**`. The wording may change; the
        # prohibition may not.
        skill = " ".join(self._skill().lower().replace("*", "").split())
        assert "never read or write" in skill
        assert "never write into it" in skill

    def test_the_agents_own_outputs_are_still_documented(self):
        """The prohibition must not have crowded out the instruction that matters."""
        skill = self._skill()
        assert "OUTPUT_FILE:" in skill
        assert "note-taker.md" in skill
        assert "sketch-artist.html" in skill


class TestPlatformSupport:
    def test_windows_system_audio_is_latent_until_the_manifest_allows_windows(self):
        """A known, deliberate gap — recorded here so changing it is a decision.

        Step 3 of this work added a Windows-only branch to the Electron capture path,
        because Electron 43's `Streams.audio: 'loopback'` is documented as Windows-only.
        But `platform.os` is ``["macos", "linux"]``, and that list IS enforced
        (``PlatformConfig.supports_platform``), so the App Store will not install this
        app on Windows and the branch cannot run.

        The branch is kept rather than deleted because the constraint is inherited
        from the pre-port app (an internally built daemon), not from anything in the
        code today — and expanding the list would ship a claim ("this works on
        Windows") that has never been verified on a Windows build.

        **If you are adding ``windows`` to the manifest: verify a packaged Windows
        build first, then delete this test.** It exists to fail the moment the two
        halves stop disagreeing, so nobody changes one and forgets the other.
        """
        declared = set(_manifest()["platform"]["os"])
        assert declared == {"macos", "linux"}

        # The other half of the disagreement, so this test fails if the branch goes
        # away too — at which point the comment above is what needs deleting.
        assert 'new Set(["win32"])' in DISPLAY_MEDIA.read_text(encoding="utf-8")


class TestStoreCopyMatchesTheApp:
    """`highlights` is the App Store's description of what the app does."""

    def test_the_new_capabilities_are_advertised(self):
        # A user choosing whether to install cannot read the diff. Each of these was
        # a whole feature; a store listing that omits them describes a different app.
        blob = " ".join(_manifest()["highlights"]).lower()
        for claim in ("records both sides", "minutes stay yours", "another language", "paste a screenshot"):
            assert claim in blob, f"highlights do not mention: {claim}"

    def test_every_highlight_is_a_non_empty_string(self):
        for entry in _manifest()["highlights"]:
            assert isinstance(entry, str) and entry.strip()

    def test_the_author_field_is_unchanged(self):
        """The port's ATTRIBUTION promises this. It is not ours to take."""
        assert _manifest()["author"] == "adunuthu"
