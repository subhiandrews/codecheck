"""Split a large change into reviewable chunks without ever dropping a file."""

from collections.abc import Mapping


def chunk_files(sizes: Mapping[str, int], *, max_files: int, max_chars: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    chars = 0
    for path, size in sizes.items():
        if current and (len(current) >= max_files or chars + size > max_chars):
            chunks.append(current)
            current, chars = [], 0
        current.append(path)
        chars += size
    if current:
        chunks.append(current)
    return chunks
