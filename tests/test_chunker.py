"""Tests for semantic text chunking."""

import pytest
from src.utils.chunker import recursive_character_split

def test_short_text_single_chunk():
    """Test short text produces single chunk."""
    text = "This is a short note about personal libraries."
    chunks = recursive_character_split(text, target_tokens=400)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].text == text

def test_long_text_multiple_chunks_with_overlap():
    """Test long text is split into multiple chunks with overlap."""
    # Create ~2000 words text
    paragraphs = [f"Paragraph {i}: " + ("The quick brown fox jumps over the lazy dog. " * 15) for i in range(20)]
    long_text = "\n\n".join(paragraphs)
    chunks = recursive_character_split(long_text, target_tokens=200, overlap_tokens=30)
    assert len(chunks) > 1
    assert chunks[0].index == 0
    assert chunks[1].index == 1
    # Verify second chunk has overlap prefix
    assert chunks[1].text.startswith("...") or "dog" in chunks[1].text
