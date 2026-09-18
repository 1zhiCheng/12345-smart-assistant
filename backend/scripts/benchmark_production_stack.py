"""Mongo 向量检索 + Redis Stream worker 的生产压测（不调用外部 LLM）。"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.deps import build_container

ROOT = Path(__file__).resolve().parents[2]
QUERIES = (
    "小区沿街商铺夜间占道经营",
    "公交车到站时间和线路调整",
    "消费者购买商品后申请退款",
    "医院疫苗接种如何预约",
    "农村宅基地和农田灌溉问题",
    "施工噪声和扬尘污染投诉",
    "社会保险待遇查询",
    "企业登记和市场监管咨询",
    "道路积水影响居民出行",
    "公共文化场馆开放时间",
    "治安案件和公共安全线索",
    "住房建设施工许可办理",
)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return ordered[index]


async def run(args: argparse.Namespace) -> dict:
    settings = get_settings()
    if settings.storage_mode != "mongo" or settings.vector_backend != "mongo":
        raise SystemExit("压测要求 STORAGE_MODE=mongo 且 VECTOR_BACKEND=mongo")
    if settings.embedding_provider == "hash" or settings.embedding_allow_hash_fallback:
        raise SystemExit("压测禁止 hash embedding/fallback")

    container = build_container(settings)
    await container.mongo.connect()
    await container.session_store.connect()
    job_ids: list[str] = []
    try:
        if not await container.job_queue.worker_is_alive():
            raise RuntimeError("worker 心跳不存在")
        vector_count = await container.vector_store.count()
        if vector_count == 0:
            raise RuntimeError("MongoDB 向量库为空，请先导入官方知识库")

        query_vectors = await container.embeddings.embed(list(QUERIES))
        cold_started = time.perf_counter()
        cold_hits = await container.vector_store.search(query_vectors[0], top_k=5)
        cold_search_ms = (time.perf_counter() - cold_started) * 1000
        if not cold_hits:
            raise RuntimeError("冷启动检索结果为空")
        semaphore = asyncio.Semaphore(args.concurrency)
        search_latencies: list[float] = []
        search_errors = 0

        async def search_once(index: int) -> None:
            nonlocal search_errors
            async with semaphore:
                started = time.perf_counter()
                try:
                    hits = await container.vector_store.search(query_vectors[index % len(query_vectors)], top_k=5)
                    if not hits:
                        raise RuntimeError("empty result")
                except Exception:  # noqa: BLE001
                    search_errors += 1
                finally:
                    search_latencies.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(search_once(index) for index in range(args.requests)))
        search_seconds = time.perf_counter() - started

        job_started: dict[str, float] = {}
        nonce = uuid.uuid4().hex
        started = time.perf_counter()
        for index in range(args.jobs):
            job = await container.job_queue.enqueue("health_probe", {"nonce": f"{nonce}:{index}"})
            job_ids.append(job["_id"])
            job_started[job["_id"]] = time.perf_counter()

        pending = set(job_ids)
        job_latencies: list[float] = []
        job_errors = 0
        deadline = asyncio.get_running_loop().time() + args.job_timeout
        while pending and asyncio.get_running_loop().time() < deadline:
            for job_id in list(pending):
                job = await container.store.get("async_jobs", job_id)
                if job and job.get("status") in {"completed", "dead_letter"}:
                    pending.remove(job_id)
                    job_latencies.append((time.perf_counter() - job_started[job_id]) * 1000)
                    if job.get("status") != "completed":
                        job_errors += 1
            if pending:
                await asyncio.sleep(0.05)
        job_errors += len(pending)
        job_seconds = time.perf_counter() - started

        total_searches = len(search_latencies)
        search_error_rate = search_errors / max(1, total_searches)
        job_error_rate = job_errors / max(1, args.jobs)
        metrics = {
            "vector_count": vector_count,
            "search": {
                "requests": total_searches,
                "concurrency": args.concurrency,
                "cold_start_ms": round(cold_search_ms, 2),
                "throughput_per_second": round(total_searches / max(search_seconds, 1e-9), 2),
                "error_rate": round(search_error_rate, 4),
                "p50_ms": round(percentile(search_latencies, 0.50), 2),
                "p95_ms": round(percentile(search_latencies, 0.95), 2),
                "p99_ms": round(percentile(search_latencies, 0.99), 2),
            },
            "worker": {
                "jobs": args.jobs,
                "throughput_per_second": round((args.jobs - job_errors) / max(job_seconds, 1e-9), 2),
                "error_rate": round(job_error_rate, 4),
                "p50_ms": round(percentile(job_latencies, 0.50), 2),
                "p95_ms": round(percentile(job_latencies, 0.95), 2),
                "p99_ms": round(percentile(job_latencies, 0.99), 2),
                "timed_out": len(pending),
            },
        }
        gates = {
            "search_error_rate_lt_1pct": search_error_rate < 0.01,
            "search_p95_lt_2000ms": percentile(search_latencies, 0.95) < 2000,
            "worker_error_rate_lt_1pct": job_error_rate < 0.01,
            "worker_p95_lt_5000ms": percentile(job_latencies, 0.95) < 5000,
        }
        report = {
            "status": "passed" if all(gates.values()) else "failed",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "embedding": {
                "provider": container.embeddings.last_effective_provider,
                "model": settings.embedding_model,
                "dimension": len(query_vectors[0]),
            },
            "metrics": metrics,
            "gates": gates,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        for job_id in job_ids:
            await container.store.delete("async_jobs", job_id)
        await container.session_store.close()
        await container.mongo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--job-timeout", type=float, default=120.0)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "docs" / "competition" / "wuhu_production_stack_benchmark.json",
    )
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
