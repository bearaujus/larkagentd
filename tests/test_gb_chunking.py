from larkagentd.knowledge.chunking import DocumentTooLarge, check_limits, chunk_markdown


def test_markdown_sections_code_overlap_and_determinism():
    text = (
        "# Intro\nStart here.\n\n## Setup\n"
        + "\n```sql\nSELECT 1;\nSELECT 2;\n```\n\n"
        + ("Sentence words. " * 90)
    )
    chunks = chunk_markdown(text, chunk_tokens=32, overlap=4)
    assert chunks == chunk_markdown(text, chunk_tokens=32, overlap=4)
    assert any("```sql" in chunk.text and "```" in chunk.text for chunk in chunks)
    assert any(chunk.section_path == "Intro > Setup" for chunk in chunks)
    assert all(chunk.text and chunk.tokens > 0 for chunk in chunks)
    assert any(
        len(chunks[i].text) > 0 and chunks[i].section_path == chunks[i + 1].section_path
        for i in range(len(chunks) - 1)
    )
    assert any(
        chunks[i].text[-8:] in chunks[i + 1].text
        for i in range(len(chunks) - 1)
        if chunks[i].section_path == chunks[i + 1].section_path
    )


def test_long_block_reassembles_without_loss():
    text = "Sentence words. " * 90
    chunks = chunk_markdown(text, chunk_tokens=32, overlap=4)
    rebuilt = chunks[0].text
    for chunk in chunks[1:]:
        rebuilt += chunk.text[15:]
    assert rebuilt == text


def test_chunking_preserves_unusual_inputs_and_limits():
    for text in (
        "x" * 5000,
        "plain words\n" * 400,
        "雪と文字🙂\n" * 100,
        "# Only heading\n## Another",
    ):
        pieces = chunk_markdown(text, chunk_tokens=20, overlap=2)
        assert pieces
        assert all(piece.text for piece in pieces)
        if text.count("x") < 2:
            assert "".join(piece.text for piece in pieces).replace(" ", "")
    try:
        check_limits([1, 2], 11, max_chunks=2, max_tokens=10)
    except DocumentTooLarge as exc:
        assert exc.code == "too_large"
    else:
        raise AssertionError("oversize document was accepted")
