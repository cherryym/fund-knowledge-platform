"""Independent contract checks using synthetic text only; no app or model imports."""

import random
from dataclasses import FrozenInstanceError, asdict
from itertools import product

import pytest

from fund_kb.embedding_chunks import EmbeddingChunk, split_text_for_embedding


def assert_complete_coverage(text, chunks, max_bytes, overlap_bytes=0):
    """Rebuild the source from new chunk suffixes, without using splitter helpers."""
    if not text:
        assert chunks == []
        return

    assert chunks
    covered_end = 0
    previous_start = -1
    rebuilt = []
    for index, chunk in enumerate(chunks):
        assert isinstance(chunk, EmbeddingChunk)
        assert chunk.index == index
        assert 0 <= chunk.start < chunk.end <= len(text)
        assert chunk.start > previous_start
        assert chunk.start <= covered_end < chunk.end
        assert chunk.text == text[chunk.start : chunk.end]
        assert chunk.text != ""
        assert len(chunk.text.encode("utf-8")) <= max_bytes
        assert len(text[chunk.start : covered_end].encode("utf-8")) <= overlap_bytes
        rebuilt.append(chunk.text[covered_end - chunk.start :])
        previous_start = chunk.start
        covered_end = chunk.end

    assert chunks[0].start == 0
    assert covered_end == len(text)
    assert "".join(rebuilt) == text
    if overlap_bytes == 0:
        assert "".join(chunk.text for chunk in chunks) == text


def test_chunk_is_an_immutable_value_with_the_public_fields():
    chunk = EmbeddingChunk(index=0, start=1, end=3, text="中🙂")
    assert asdict(chunk) == {"index": 0, "start": 1, "end": 3, "text": "中🙂"}
    assert chunk == EmbeddingChunk(0, 1, 3, "中🙂")
    assert hash(chunk) == hash(EmbeddingChunk(0, 1, 3, "中🙂"))
    for field, value in (("index", 1), ("start", 0), ("end", 4), ("text", "changed")):
        with pytest.raises(FrozenInstanceError):
            setattr(chunk, field, value)


@pytest.mark.parametrize("max_bytes,overlap_bytes", [(1, 0), (4, 3), (1024, 100)])
def test_empty_text(max_bytes, overlap_bytes):
    assert split_text_for_embedding("", max_bytes=max_bytes, overlap_bytes=overlap_bytes) == []


@pytest.mark.parametrize(
    "text",
    ["a", " ", "\n\t\r\n", "中文原文", "Hello, world.", "🙂🚀", "e\u0301", "  原文\n\n尾部  "],
)
def test_fitting_input_is_one_verbatim_chunk_even_with_overlap(text):
    budget = len(text.encode("utf-8"))
    chunks = split_text_for_embedding(text, max_bytes=budget, overlap_bytes=budget - 1)
    assert chunks == [EmbeddingChunk(0, 0, len(text), text)]


@pytest.mark.parametrize(
    "text",
    [
        "English words, punctuation! The end.  ",
        "中文估值说明。下一句包含条件；保留例外！",
        "中文 English 🙂 emoji🚀 混排，尾部空白 \n",
        "👩🏽\u200d💻🇨🇳e\u0301\ufe0f" * 8,
        "\0\x7f\u0080\u07ff\u0800\ud7ff\ue000\uffff\U00010000\U0010ffff",
    ],
)
@pytest.mark.parametrize("max_bytes,overlap_bytes", [(4, 0), (4, 3), (7, 2), (19, 8), (100, 0)])
def test_unicode_byte_budgets_and_full_coverage(text, max_bytes, overlap_bytes):
    chunks = split_text_for_embedding(text, max_bytes=max_bytes, overlap_bytes=overlap_bytes)
    assert_complete_coverage(text, chunks, max_bytes, overlap_bytes)


def test_offsets_are_python_character_indices_not_utf8_byte_offsets():
    assert split_text_for_embedding("a中🙂b", max_bytes=4) == [
        EmbeddingChunk(0, 0, 2, "a中"),
        EmbeddingChunk(1, 2, 3, "🙂"),
        EmbeddingChunk(2, 3, 4, "b"),
    ]


@pytest.mark.parametrize(
    "text,max_bytes,first_chunk",
    [
        ("甲段。\n\n乙句。后面的文字很长", 20, "甲段。\n\n"),
        ("alpha\nbeta\ngamma continues", 12, "alpha\nbeta\n"),
        ("first\r\nsecond\r\nthird", 12, "first\r\n"),
        ("甲句。乙句。后面的文字很长", 22, "甲句。乙句。"),
        ("Hello world. Next sentence is long enough.", 20, "Hello world. "),
        ("“甲句。”后面的文字很长", 21, "“甲句。”"),
        ("alpha beta gamma", 8, "alpha "),
    ],
)
def test_natural_boundaries_are_preferred(text, max_bytes, first_chunk):
    chunks = split_text_for_embedding(text, max_bytes=max_bytes)
    assert chunks[0].text == first_chunk
    assert_complete_coverage(text, chunks, max_bytes)


def test_english_decimal_point_is_not_a_sentence_boundary():
    text = "123.456789abcdefgh"
    chunks = split_text_for_embedding(text, max_bytes=8)
    assert chunks[0].text == "123.4567"
    assert_complete_coverage(text, chunks, 8)


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\u0085", "\u2028", "\u2029"])
def test_blank_line_paragraphs_retain_the_original_line_separators(separator):
    first = f"first{separator} \t{separator}"
    text = first + "second paragraph continues without a full stop"
    budget = len((first + "second").encode("utf-8"))
    chunks = split_text_for_embedding(text, max_bytes=budget)
    assert chunks[0].text == first
    assert_complete_coverage(text, chunks, budget)


@pytest.mark.parametrize("overlap_bytes", [0, 7, 30])
def test_whitespace_only_input_is_fully_preserved(overlap_bytes):
    text = " \t\r\n" * 3000 + " " * 20_000 + "\u3000\u00a0" * 1000
    chunks = split_text_for_embedding(text, max_bytes=31, overlap_bytes=overlap_bytes)
    assert_complete_coverage(text, chunks, 31, overlap_bytes)
    assert all(not chunk.text.strip() for chunk in chunks)


@pytest.mark.parametrize("max_bytes", [8, 19, 37, 101])
@pytest.mark.parametrize("overlap_bytes", [0, 3, 7])
def test_multiline_tables_and_code_remain_verbatim(max_bytes, overlap_bytes):
    text = (
        "\n# 合成示例\r\n\r\n"
        "| 字段 | 值 |\r\n| --- | --- |\r\n| emoji | 🙂 |\r\n"
        "\n```python\ndef sample():\n\treturn '中\\n文'  # trailing spaces  \n```\n"
        "\n  缩进和空行必须保留。\n\n\tend  \n"
    ) * 12
    chunks = split_text_for_embedding(text, max_bytes=max_bytes, overlap_bytes=overlap_bytes)
    assert_complete_coverage(text, chunks, max_bytes, overlap_bytes)


@pytest.mark.parametrize(
    "text", ["x" * 70_000, "基金估值" * 18_000, "无空格🙂文本" * 12_000, "." * 70_000 + "suffix"]
)
def test_very_long_unbroken_text_has_no_length_limit(text):
    chunks = split_text_for_embedding(text, max_bytes=513, overlap_bytes=31)
    assert_complete_coverage(text, chunks, 513, 31)


def test_paragraphs_of_tens_of_thousands_of_characters_are_not_truncated():
    paragraph = "这是合成中文测试句，含 English 和 emoji🙂，保持原样。" * 3000
    text = paragraph + "\n\n" + paragraph + "\n\n最后一行。  "
    assert len(paragraph) > 50_000
    chunks = split_text_for_embedding(text, max_bytes=997, overlap_bytes=103)
    assert_complete_coverage(text, chunks, 997, 103)


def test_more_than_ten_thousand_chunks_are_all_returned():
    text = "x" * 10_005
    chunks = split_text_for_embedding(text, max_bytes=1)
    assert len(chunks) == len(text)
    assert_complete_coverage(text, chunks, 1)


def test_ascii_overlap_is_the_requested_suffix_when_it_fits():
    text = "abcdefghijklmnop"
    chunks = split_text_for_embedding(text, max_bytes=8, overlap_bytes=3)
    assert chunks == [
        EmbeddingChunk(0, 0, 8, "abcdefgh"),
        EmbeddingChunk(1, 5, 13, "fghijklm"),
        EmbeddingChunk(2, 10, 16, "klmnop"),
    ]
    assert_complete_coverage(text, chunks, 8, 3)


def test_overlap_rounds_to_whole_utf8_characters_within_budget():
    text = "甲乙丙丁戊己庚辛"
    chunks = split_text_for_embedding(text, max_bytes=9, overlap_bytes=4)
    assert [(chunk.start, chunk.end) for chunk in chunks] == [(0, 3), (2, 5), (4, 7), (6, 8)]
    assert_complete_coverage(text, chunks, 9, 4)


def test_overlap_smaller_than_one_character_can_be_zero():
    text = "甲乙丙丁戊己"
    chunks = split_text_for_embedding(text, max_bytes=6, overlap_bytes=2)
    assert [(chunk.start, chunk.end) for chunk in chunks] == [(0, 2), (2, 4), (4, 6)]
    assert_complete_coverage(text, chunks, 6, 2)


def test_overlap_shrinks_to_make_room_for_the_next_wide_character():
    text = "abcd🙂e"
    chunks = split_text_for_embedding(text, max_bytes=4, overlap_bytes=3)
    assert chunks == [
        EmbeddingChunk(0, 0, 4, "abcd"),
        EmbeddingChunk(1, 4, 5, "🙂"),
        EmbeddingChunk(2, 5, 6, "e"),
    ]
    assert_complete_coverage(text, chunks, 4, 3)


@pytest.mark.parametrize(
    "text", ["a\n\nbcdefghijklmnopqrstuvwxyz", "aa\nbb\ncc\ndd\nee\n", "甲乙🙂丙a丁🚀e" * 30]
)
@pytest.mark.parametrize("max_bytes", [4, 7, 10])
def test_near_full_overlap_and_early_boundaries_always_advance(text, max_bytes):
    chunks = split_text_for_embedding(text, max_bytes=max_bytes, overlap_bytes=max_bytes - 1)
    assert len(chunks) <= len(text)
    assert_complete_coverage(text, chunks, max_bytes, max_bytes - 1)


def test_maximal_ascii_overlap_stops_after_the_first_complete_coverage():
    text = "abcdefghijk"
    chunks = split_text_for_embedding(text, max_bytes=5, overlap_bytes=4)
    assert [(chunk.start, chunk.end) for chunk in chunks] == [(start, start + 5) for start in range(7)]
    assert_complete_coverage(text, chunks, 5, 4)


def test_exhaustive_small_unicode_inputs_and_overlap_budgets():
    # 5,456 combinations exercise character widths, newline heuristics and progress.
    for size in range(5):
        for characters in product("a中🙂\n", repeat=size):
            text = "".join(characters)
            for max_bytes in (4, 5, 7):
                for overlap_bytes in range(max_bytes):
                    chunks = split_text_for_embedding(text, max_bytes=max_bytes, overlap_bytes=overlap_bytes)
                    assert_complete_coverage(text, chunks, max_bytes, overlap_bytes)


@pytest.mark.parametrize("seed", [0, 17, 20260910])
def test_seeded_synthetic_inputs_are_deterministic_and_lossless(seed):
    rng = random.Random(seed)
    alphabet = "ab09中估。！？.!? \t\n\r🙂🚀\u0301\u200d\u00a0\u2028|`"
    for _ in range(100):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(200)))
        max_bytes = rng.randrange(4, 50)
        overlap_bytes = rng.randrange(max_bytes)
        chunks = split_text_for_embedding(text, max_bytes=max_bytes, overlap_bytes=overlap_bytes)
        assert chunks == split_text_for_embedding(text, max_bytes=max_bytes, overlap_bytes=overlap_bytes)
        assert_complete_coverage(text, chunks, max_bytes, overlap_bytes)


@pytest.mark.parametrize("text", [None, b"bytes", 123, ["text"], {"text": "value"}])
def test_invalid_text_types_raise_type_error(text):
    with pytest.raises(TypeError, match="text"):
        split_text_for_embedding(text, max_bytes=10)


@pytest.mark.parametrize("parameter", ["max_bytes", "overlap_bytes"])
@pytest.mark.parametrize("value", [None, True, False, 3.0, "3", float("nan"), float("inf")])
@pytest.mark.parametrize("text", ["", "abc"])
def test_budget_parameters_require_integers_even_for_empty_text(parameter, value, text):
    kwargs = {"max_bytes": 10, "overlap_bytes": 0, parameter: value}
    with pytest.raises(TypeError, match=parameter):
        split_text_for_embedding(text, **kwargs)


@pytest.mark.parametrize(
    "max_bytes,overlap_bytes,parameter",
    [
        (0, 0, "max_bytes"),
        (-1, 0, "max_bytes"),
        (4, -1, "overlap_bytes"),
        (4, 4, "overlap_bytes"),
        (4, 5, "overlap_bytes"),
    ],
)
@pytest.mark.parametrize("text", ["", "abc"])
def test_invalid_budget_ranges_raise_value_error(max_bytes, overlap_bytes, parameter, text):
    with pytest.raises(ValueError, match=parameter):
        split_text_for_embedding(text, max_bytes=max_bytes, overlap_bytes=overlap_bytes)


@pytest.mark.parametrize(
    "text,max_bytes,index,width",
    [("中", 2, 0, 3), ("🙂", 3, 0, 4), ("é", 1, 0, 2), ("a" * 25_000 + "中", 2, 25_000, 3)],
)
def test_a_character_that_cannot_fit_raises_with_its_source_index(text, max_bytes, index, width):
    with pytest.raises(ValueError, match=f"character at index {index} needs {width} UTF-8 bytes"):
        split_text_for_embedding(text, max_bytes=max_bytes)


@pytest.mark.parametrize("text", ["\ud800", "prefix\udfff", "\ud83d\ude00", "x" * 10_000 + "\ud800"])
def test_invalid_unicode_is_rejected_without_replacement(text):
    with pytest.raises(UnicodeEncodeError):
        split_text_for_embedding(text, max_bytes=16)


def test_byte_budget_arguments_are_keyword_only():
    with pytest.raises(TypeError):
        split_text_for_embedding("abc", 3)
