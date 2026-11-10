"""Decoded-picture check of reviewer MP4s against their original segments.

Byte verification (every file read back uncached and compared with a known SHA-256) is what
makes an export COMPLETE. This module is the *additional* check that an MP4 shows exactly the
pictures of its originals. Levels (``[export] content_check``), recorded per MP4:

``full``      Every frame of the MP4, decoded from the bytes read back from the drive, must equal
              the concatenated per-frame SHA-256 lists of its source segments.
``sampled``   (1) the MP4's frame count (demux only) must equal the total of the sources' lists,
              (2) the first ``sample_edge_frames`` and last ``sample_edge_frames`` frames and
              ``sample_spans`` spans of ``sample_span_frames`` frames at pseudo-random positions
              (seeded by export id + file name, so a resume checks the same frames) are decoded
              from the file on the drive and compared by position. Requires the recorder's
              per-frame lists for every source; if one is missing the MP4 is recorded ``deferred``.
              Catches missing / duplicated / reordered segments (count + positions) and wrong
              content at the sampled positions; does NOT prove every frame.
``deferred``  No picture check yet. The manifest says "content not yet verified";
              ``python -m sentinel.export verify-content`` completes it later.

Damaged final frames: a recording cut off mid-frame (camera drop / crash) ends in a damaged
picture whose error-concealed result depends on decoder state (measured: 1 of 6 camera-drop
recordings, FFmpeg 9.0.1). A difference is accepted ONLY for the last ``DAMAGED_TAIL`` frames of
a source segment that is INTERRUPTED or whose decoding reports errors, and is listed in the
manifest as "unverifiable damaged frames". Any other difference fails the export (E-003).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Mapping

DAMAGED_TAIL = 2


def choose_spans(total: int, edge: int, spans: int, span_frames: int, seed: str) -> list[tuple[int, int]]:
    """Half-open frame-index spans to check, merged and sorted."""
    if total <= 0:
        return []
    out = [(0, min(edge, total)), (max(0, total - edge), total)]
    lo, hi = edge, total - edge - span_frames
    if spans and hi > lo:
        rng = random.Random(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16))
        for _ in range(spans):
            a = rng.randint(lo, hi)
            out.append((a, a + span_frames))
    out.sort()
    merged: list[tuple[int, int]] = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def tail_candidates(counts: list[int], tail: int = DAMAGED_TAIL) -> dict[int, int]:
    """frame index -> source position, for the last ``tail`` frames of every source."""
    out: dict[int, int] = {}
    off = 0
    for pos, n in enumerate(counts):
        for k in range(max(0, n - tail), n):
            out[off + k] = pos
        off += n
    return out


def mismatches(got: Mapping[int, str], expected: list[str]) -> list[int]:
    return sorted(i for i, h in got.items() if i >= len(expected) or expected[i] != h)


def accept_differences(diff: list[int], counts: list[int], is_damaged: Callable[[int], bool]) -> tuple[bool, list[int]]:
    """(ok, accepted indices). ``is_damaged(source_position)`` is only called for sources whose
    last frames differ, so the (costly) decoder check runs on at most those segments."""
    if not diff:
        return True, []
    cand = tail_candidates(counts)
    if any(i not in cand for i in diff):
        return False, []
    for pos in sorted({cand[i] for i in diff}):
        if not is_damaged(pos):
            return False, []
    return True, diff
