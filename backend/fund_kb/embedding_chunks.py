"""Lossless, deterministic plain-text chunking with a UTF-8 byte budget."""

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingChunk:
    """A zero-based chunk whose offsets are Python string indices, not bytes."""

    index: int
    start: int
    end: int
    text: str


_SENTENCE_END = re.compile(r"""(?:[。！？!?；;]+|\.(?=\s|["'”’)\]）】》」』]|$))["'”’)\]）】》」』]*[ \t]*""")
_WHITESPACE_END = re.compile(r"\s+")


def _preferred_boundaries(text: str) -> tuple[list[int], ...]:
    """Index blank lines, line ends, sentence ends and whitespace runs once."""
    paragraphs: list[int] = []
    lines: list[int] = []
    position = 0
    for line in text.splitlines(keepends=True):
        position += len(line)
        lines.append(position)
        if not line.strip():
            paragraphs.append(position)
    return (
        paragraphs,
        lines,
        [match.end() for match in _SENTENCE_END.finditer(text)],
        [match.end() for match in _WHITESPACE_END.finditer(text)],
    )


def split_text_for_embedding(text: str, *, max_bytes: int, overlap_bytes: int = 0) -> list[EmbeddingChunk]:
    """Split all of ``text`` into exact source slices without normalizing it.

    ``max_bytes`` must be a positive integer; ``overlap_bytes`` must be an
    integer in ``[0, max_bytes)``. Booleans are not accepted as integers.
    Valid empty input returns an empty list; whitespace-only input is retained.

    Blank-line, line, sentence and word boundaries are preferred, in that
    order, choosing the last fitting boundary at the first available priority.
    A final remainder that fits is kept whole. These are text heuristics, not
    a parser: hard splits may divide a grapheme cluster or Markdown construct,
    but never a Python character or its UTF-8 encoding.

    Overlap is the longest suffix of the preceding chunk within its byte
    budget that also advances the start and leaves room for the next unseen
    character. It can therefore be smaller than ``overlap_bytes`` (or zero).
    Every chunk advances both offsets and contributes previously unseen text.
    There is no total length or chunk-count limit.

    Raises ``TypeError`` for invalid argument types, ``ValueError`` for invalid
    budgets or a character wider than ``max_bytes``, and ``UnicodeEncodeError``
    for text containing surrogates that cannot be strictly UTF-8 encoded.
    The complete input is validated before any chunks are returned.

    Only source text consumes the budget here; callers must reserve any title,
    wrapper or model-specific overhead themselves. Byte counts are not tokens.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    for name, value in (("max_bytes", max_bytes), ("overlap_bytes", overlap_bytes)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer, not a boolean")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if overlap_bytes < 0 or overlap_bytes >= max_bytes:
        raise ValueError("overlap_bytes must satisfy 0 <= overlap_bytes < max_bytes")

    # Prefix byte counts let all later budget checks stay on character indices.
    byte_offsets = [0]
    for index, character in enumerate(text):
        width = len(character.encode("utf-8"))
        if width > max_bytes:
            raise ValueError(
                f"character at index {index} needs {width} UTF-8 bytes, exceeding max_bytes={max_bytes}"
            )
        byte_offsets.append(byte_offsets[-1] + width)

    if not text:
        return []
    if byte_offsets[-1] <= max_bytes:
        return [EmbeddingChunk(index=0, start=0, end=len(text), text=text)]

    boundaries = _preferred_boundaries(text)
    chunks: list[EmbeddingChunk] = []
    start = 0
    covered_end = 0
    while covered_end < len(text):
        end = bisect_right(byte_offsets, byte_offsets[start] + max_bytes, lo=start + 1) - 1
        if end < len(text):
            for candidates in boundaries:
                candidate_index = bisect_right(candidates, end) - 1
                if candidate_index >= 0 and candidates[candidate_index] > covered_end:
                    end = candidates[candidate_index]
                    break

        chunks.append(EmbeddingChunk(index=len(chunks), start=start, end=end, text=text[start:end]))
        if end == len(text):
            break

        # A wide next character may need more room than max_bytes - overlap_bytes.
        # Restrict overlap to this chunk and require at least one new character.
        earliest_byte = max(byte_offsets[end] - overlap_bytes, byte_offsets[end + 1] - max_bytes)
        start = bisect_left(byte_offsets, earliest_byte, lo=start + 1, hi=end + 1)
        covered_end = end

    return chunks
