"""
app/services/document_loader.py

Loads .md/.txt files from a folder and splits them into retrievable
chunks. This is retrieval-backend-agnostic -- vector_knowledge_base.py
uses it today; a keyword-based backend could reuse it too.
"""

import re
from pathlib import Path


def load_chunks(folder_path, chunk_size=500):
    """Return (chunks, sources) for every .md/.txt file in folder_path."""
    chunks, sources = [], []
    folder = Path(folder_path)

    for file_path in folder.glob("*.md"):
        text = file_path.read_text(encoding="utf-8")
        for section in _split_markdown(text):
            chunks.append(section)
            sources.append(file_path.name)

    for file_path in folder.glob("*.txt"):
        text = file_path.read_text(encoding="utf-8")
        for paragraph_group in _split_by_size(text, chunk_size):
            chunks.append(paragraph_group)
            sources.append(file_path.name)

    return chunks, sources


def _split_markdown(text):
    """Split on '##' headers so each FAQ/topic section stays its own
    chunk. This keeps a question's answer from getting diluted by
    unrelated neighboring sections, which hurts retrieval accuracy."""
    sections = re.split(r"\n(?=##\s)", text)
    return [
        section.strip()
        for section in sections
        if section.strip() and not section.strip().startswith("# ")
    ]


def _split_by_size(text, chunk_size):
    """Fallback for plain text: group paragraphs up to chunk_size chars."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    buffer = ""
    out = []
    for p in paragraphs:
        if len(buffer) + len(p) < chunk_size:
            buffer += ("\n\n" if buffer else "") + p
        else:
            if buffer:
                out.append(buffer)
            buffer = p
    if buffer:
        out.append(buffer)
    return out
