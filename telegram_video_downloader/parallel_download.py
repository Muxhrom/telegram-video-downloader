"""Bounded, disjoint range downloads through Telethon's public iterator."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Callable


CHUNK_SIZE = 512 * 1024
PARALLEL_MIN_SIZE = 32 * 1024 * 1024


async def download_document_parallel(
    client: Any,
    document: Any,
    path: Path,
    size: int,
    progress: Callable[[int, int], Any],
    pause_event: asyncio.Event,
    *,
    stream_count: int = 4,
) -> None:
    """Fetch a document in aligned ranges and write each range in place."""
    if size <= 0 or stream_count < 2:
        raise ValueError("Parallel download needs a known size and at least two streams")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        output.truncate(size)

    total_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    streams = min(stream_count, total_chunks)
    transferred = 0

    async def fetch_range(start: int, end: int) -> None:
        nonlocal transferred
        cursor = start
        # Telethon selects its direct request path when offset % limit == 0.
        # This generous chunk limit keeps aligned range starts on that path;
        # the range boundary below controls when each iterator stops.
        iterator = client.iter_download(
            document,
            offset=start,
            request_size=CHUNK_SIZE,
            chunk_size=CHUNK_SIZE,
            file_size=size,
            limit=CHUNK_SIZE,
        )
        try:
            with path.open("r+b", buffering=0) as output:
                output.seek(start)
                async for chunk in iterator:
                    await pause_event.wait()
                    remaining = end - cursor
                    if remaining <= 0:
                        break
                    data = memoryview(chunk)[:remaining]
                    written = output.write(data)
                    if written != len(data):
                        raise OSError("Incomplete local write during download")
                    cursor += written
                    transferred += written
                    result = progress(transferred, size)
                    if inspect.isawaitable(result):
                        await result
                    if cursor >= end:
                        break
            if cursor != end:
                raise RuntimeError(f"Telegram download ended early at {cursor} of {end} bytes")
        finally:
            await iterator.close()

    tasks = []
    for index in range(streams):
        start = (index * total_chunks // streams) * CHUNK_SIZE
        end = min(size, ((index + 1) * total_chunks // streams) * CHUNK_SIZE)
        tasks.append(asyncio.create_task(fetch_range(start, end)))
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
