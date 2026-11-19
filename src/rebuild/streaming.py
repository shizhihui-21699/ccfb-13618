"""千页文档的流式 chunk 调度：有界并发、背压、检查点、崩溃重试。

对应 architecture.md §7。worker 由调用方注入（真实实现是启动沙箱子进程，
测试时可传 fake），本模块只负责调度语义。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

CHUNK_PAGES = 25              # 每个渲染子进程（新 jail）处理的页数
MAX_INFLIGHT_PAGES = 8 * CHUNK_PAGES
SANDBOX_RETRIES = 1           # chunk 崩溃后换新沙箱重试一次


class ChunkStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Chunk:
    index: int
    page_start: int
    page_end: int            # exclusive
    status: ChunkStatus = ChunkStatus.PENDING
    attempts: int = 0
    artifact_key: str | None = None
    error: str | None = None


@dataclass
class CheckpointStore:
    """生产实现换成 Redis/Postgres；进程内 dict 仅用于测试与本地跑通。"""
    chunks: dict[int, Chunk] = field(default_factory=dict)

    def save(self, chunk: Chunk) -> None:
        self.chunks[chunk.index] = chunk

    def all_done_or_failed(self) -> bool:
        return all(
            c.status in (ChunkStatus.DONE, ChunkStatus.FAILED)
            for c in self.chunks.values()
        )


# worker 签名：(page_start, page_end) -> 产物 key（落在 spool/对象存储）
Worker = Callable[[int, int], Awaitable[str]]


def plan_chunks(total_pages: int) -> list[tuple[int, int]]:
    return [
        (start, min(start + CHUNK_PAGES, total_pages))
        for start in range(0, total_pages, CHUNK_PAGES)
    ]


async def run_streaming(
    total_pages: int,
    worker: Worker,
    store: CheckpointStore,
    *,
    allow_partial: bool = False,
) -> list[Chunk]:
    """按 chunk 调度渲染，内存中在途页数受信号量约束（背压）。

    - 新 worker 实例（崩溃重启场景）调用本函数时，store 中 DONE 的 chunk 直接跳过；
    - 单 chunk 重试 SANDBOX_RETRIES 次，仍失败标记 FAILED；
    - allow_partial=False 时任一 FAILED 整体失败；True 时失败页由上层放占位页。
    """
    inflight = asyncio.Semaphore(MAX_INFLIGHT_PAGES // CHUNK_PAGES)

    # 恢复检查点：已登记的 chunk 沿用旧状态，未登记的建 PENDING
    for idx, (start, end) in enumerate(plan_chunks(total_pages)):
        store.chunks.setdefault(idx, Chunk(idx, start, end))

    async def run_chunk(chunk: Chunk) -> None:
        if chunk.status == ChunkStatus.DONE:
            return
        async with inflight:  # 限制同时存活的渲染沙箱数 => 位图内存有界
            chunk.status = ChunkStatus.RUNNING
            while chunk.attempts <= SANDBOX_RETRIES:
                chunk.attempts += 1
                try:
                    # 每次尝试都是全新一次性沙箱，失败的进程不复用
                    chunk.artifact_key = await worker(chunk.page_start, chunk.page_end)
                    chunk.status = ChunkStatus.DONE
                    chunk.error = None
                    store.save(chunk)
                    return
                except Exception as exc:  # 沙箱超时/OOM/非零退出都走这里
                    chunk.error = repr(exc)
            chunk.status = ChunkStatus.FAILED
            store.save(chunk)

    await asyncio.gather(
        *(run_chunk(c) for c in store.chunks.values() if c.status != ChunkStatus.DONE)
    )

    failed = [c for c in store.chunks.values() if c.status == ChunkStatus.FAILED]
    if failed and not allow_partial:
        pages = ", ".join(f"{c.page_start}-{c.page_end - 1}" for c in failed)
        raise RuntimeError(f"渲染失败且未开启部分交付，失败页区间: {pages}")
    return sorted(store.chunks.values(), key=lambda c: c.index)
