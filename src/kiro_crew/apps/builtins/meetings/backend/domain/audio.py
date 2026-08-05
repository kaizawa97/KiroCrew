"""Meetings — turning a whole-file transcript into dispatchable lines.

An imported recording comes back from ``transcribe_audio`` as ONE string, but every
consumer downstream of :meth:`MeetingSession.broadcast` is built around a line: the
domain dictionary corrects per line, the noise gate judges per line, the agent
batcher coalesces lines over a window, and the translation queue spends one model
call per line. Handing that pipeline a single hour-long blob would defeat all four —
one prompt too large to be useful, one noise verdict for the whole meeting, nothing
to batch, and one translation of everything.

So this module holds the split, as a pure function with no IO, because the interesting
part is the boundary rules rather than the plumbing.
"""

from __future__ import annotations

import re

#: Sentence-final punctuation, Latin and CJK. Used only when the transcriber gave us
#: no line structure of its own.
#:
#: The lookahead requires whitespace or end-of-string AFTER the mark, which is what
#: keeps "3.5" and "e.g." from becoming boundaries — a decimal point has a digit
#: after it, not a space. CJK marks are followed by neither in practice, hence the
#: separate alternative that needs no trailing space.
_SENTENCE_END = re.compile(r"(?<=[.!?])(?=\s)|(?<=[。！？])")


def split_transcript(text: str, *, max_chars: int, max_lines: int) -> list[str]:
    """Break *text* into lines suitable for :meth:`MeetingSession.broadcast`.

    Three tiers, in order of how much the transcriber told us:

    1. **Its own line breaks**, when there are any. Whisper-family models emit one
       line per segment, and a segment is the closest thing available to "one thing
       somebody said" — better than anything punctuation can reconstruct.
    2. **Sentence boundaries**, when the whole transcript arrived as one line (AWS
       Transcribe returns a single paragraph).
    3. **A hard wrap at *max_chars***, applied to whatever tier 1 or 2 produced, so
       one runaway sentence cannot exceed what ``broadcast`` would truncate anyway.
       Truncating instead would DROP the tail of a long sentence; wrapping keeps it.

    Empty and whitespace-only pieces are dropped. The result is capped at
    *max_lines*; the tail is discarded rather than merged, because merging would
    produce exactly the oversized line this function exists to avoid.
    """
    if not text or not text.strip():
        return []

    pieces = [line.strip() for line in text.splitlines()]
    pieces = [line for line in pieces if line]
    if len(pieces) <= 1:
        # Tier 2. Split the single line the transcriber gave us.
        single = pieces[0] if pieces else ""
        pieces = [part.strip() for part in _SENTENCE_END.split(single)]
        pieces = [part for part in pieces if part]

    wrapped: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            wrapped.append(piece)
            continue
        # Tier 3. Wrap on whitespace where possible so a word is not cut in half,
        # and fall back to a hard slice for text with no spaces at all (CJK).
        wrapped.extend(_wrap(piece, max_chars))
        if len(wrapped) >= max_lines:
            break

    return wrapped[:max_lines]


def _wrap(piece: str, max_chars: int) -> list[str]:
    """Break one over-long piece at whitespace, or hard-slice when there is none."""
    out: list[str] = []
    remaining = piece
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind(" ")
        # A space too close to the start would make almost no progress, so only
        # honour one in the last third of the window; otherwise slice.
        if cut < max_chars // 3:
            cut = max_chars
        out.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        out.append(remaining)
    return [line for line in out if line]
