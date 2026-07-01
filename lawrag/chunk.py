"""Character-based chunking with paragraph awareness and overlap.

Kept deliberately simple and robust: split each page/block into paragraphs, then
greedily pack paragraphs into ~chunk_chars windows with a small overlap so context
isn't lost at boundaries. Page numbers are preserved for citations.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import CONFIG
from .parsers import Block


@dataclass
class Chunk:
    chunk_index: int
    page: int | None
    content: str


def _split_paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    return paras or ([text.strip()] if text.strip() else [])


def chunk_blocks(
    blocks: list[Block],
    chunk_chars: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    chunk_chars = chunk_chars or CONFIG.chunk_chars
    overlap = overlap or CONFIG.chunk_overlap
    chunks: list[Chunk] = []
    idx = 0

    for block in blocks:
        buf = ""
        for para in _split_paragraphs(block.text):
            # A single huge paragraph gets hard-split.
            while len(para) > chunk_chars:
                head, para = para[:chunk_chars], para[chunk_chars - overlap :]
                chunks.append(Chunk(idx, block.page, head))
                idx += 1
            if len(buf) + len(para) + 1 > chunk_chars and buf:
                chunks.append(Chunk(idx, block.page, buf.strip()))
                idx += 1
                # carry an overlap tail into the next window
                buf = buf[-overlap:] + "\n" + para
            else:
                buf = f"{buf}\n{para}" if buf else para
        if buf.strip():
            chunks.append(Chunk(idx, block.page, buf.strip()))
            idx += 1

    return chunks
