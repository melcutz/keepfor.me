# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    token_count: int


def estimate_tokens(text: str) -> int:
    """Rough estimation of token count (~4 chars per token)."""
    return max(1, len(text) // 4)


def recursive_character_split(
    text: str, target_tokens: int = 400, overlap_tokens: int = 50, max_chunks: int = 50
) -> list[TextChunk]:
    """Split text using recursive delimiters (paragraph, line, sentence, word)."""
    text = text.strip()
    if not text:
        return []

    target_chars = target_tokens * 4
    overlap_chars = overlap_tokens * 4

    if len(text) <= target_chars:
        return [TextChunk(index=0, text=text, token_count=estimate_tokens(text))]

    separators = ["\n\n", "\n", ". ", "! ", "? ", "; ", " "]

    def _split(txt: str, sep_idx: int) -> list[str]:
        if len(txt) <= target_chars or sep_idx >= len(separators):
            return [txt] if txt else []

        sep = separators[sep_idx]
        parts = txt.split(sep)
        chunks = []
        current = ""

        for part in parts:
            candidate = f"{current}{sep}{part}" if current else part
            if len(candidate) <= target_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # Split oversized parts using the next separator.
                if len(part) > target_chars:
                    chunks.extend(_split(part, sep_idx + 1))
                    current = ""
                else:
                    current = part
        if current:
            chunks.append(current)
        return chunks

    raw_chunks = _split(text, 0)

    # Merge small chunks and apply overlap
    merged_chunks: list[TextChunk] = []
    chunk_index = 0

    for i, c in enumerate(raw_chunks[:max_chunks]):
        chunk_text = c.strip()
        if not chunk_text:
            continue

        # Add overlap from previous chunk if possible
        if i > 0 and overlap_chars > 0 and len(raw_chunks[i - 1]) > overlap_chars:
            overlap_prefix = raw_chunks[i - 1][-overlap_chars:].strip()
            # Try to break at a space
            space_pos = overlap_prefix.find(" ")
            if space_pos != -1:
                overlap_prefix = overlap_prefix[space_pos + 1 :]
            chunk_text = f"...{overlap_prefix} {chunk_text}"

        merged_chunks.append(
            TextChunk(
                index=chunk_index,
                text=chunk_text,
                token_count=estimate_tokens(chunk_text),
            )
        )
        chunk_index += 1

    return merged_chunks
