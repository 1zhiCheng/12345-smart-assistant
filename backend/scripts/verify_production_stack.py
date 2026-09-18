"""验收 MongoDB、Redis、真实 BGE 向量和独立 worker 的生产链路。"""
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


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right)) / (
        (math.sqrt(sum(value * value for value in left)) or 1.0)
        * (math.sqrt(sum(value * value for value in right)) or 1.0)
    )


async def run(args: argparse.Namespace) -> dict:
    settings = get_settings()
    errors: list[str] = []
    checks: dict[str, object] = {}
    timings: dict[str, float] = {}
    nonce = uuid.uuid4().hex
    probe_doc_id = f"phase6_probe_doc_{nonce}"
    probe_vector_id = f"phase6_probe_vector_{nonce}"
    probe_job_ids: list[str] = []
    dead_letter_entry_id: str | None = None

    if settings.storage_mode != "mongo":
        errors.append("STORAGE_MODE 必须为 mongo")
    if settings.vector_backend != "mongo":
        errors.append("VECTOR_BACKEND 必须为 mongo")
    if settings.embedding_provider == "hash" or settings.embedding_allow_hash_fallback:
        errors.append("生产验收禁止 hash provider 或 hash fallback")
    if errors:
        return await _save(args.output, checks, timings, errors)

    container = build_container(settings)
    try:
        started = time.perf_counter()
        await container.mongo.connect()
        timings["mongodb_connect_ms"] = round((time.perf_counter() - started) * 1000, 2)
        checks["mongodb_ping"] = await container.mongo.ping()

        started = time.perf_counter()
        await container.session_store.connect()
        timings["redis_connect_ms"] = round((time.perf_counter() - started) * 1000, 2)
        checks["redis_ping"] = await container.session_store.ping()

        started = time.perf_counter()
        vectors = await container.embeddings.embed([
            "占道经营影响群众通行",
            "商铺在人行道摆摊经营",
            "儿童疫苗接种预约",
        ])
        timings["embedding_three_texts_ms"] = round((time.perf_counter() - started) * 1000, 2)
        related = cosine(vectors[0], vectors[1])
        unrelated = cosine(vectors[0], vectors[2])
        checks["embedding"] = {
            "configured_provider": settings.embedding_provider,
            "effective_provider": container.embeddings.last_effective_provider,
            "model": settings.embedding_model,
            "dimension": len(vectors[0]),
            "normalized": all(abs(math.sqrt(sum(x * x for x in vector)) - 1.0) < 0.01 for vector in vectors),
            "related_cosine": round(related, 4),
            "unrelated_cosine": round(unrelated, 4),
            "semantic_ordering": related > unrelated + 0.05,
        }

        await container.vector_store.add(
            probe_vector_id,
            vectors[0],
            {"doc_id": probe_doc_id, "dept_id": "dept_city_management", "chunk_index": 0},
        )
        hits = await container.vector_store.search(vectors[0], top_k=5, dept_id="dept_city_management")
        checks["mongo_vector_roundtrip"] = any(hit["id"] == probe_vector_id for hit in hits)
        saved_vector = await container.store.get("vector_embeddings", probe_vector_id)
        checks["vector_metadata"] = {
            "provider": (saved_vector or {}).get("embedding_provider"),
            "model": (saved_vector or {}).get("embedding_model"),
            "dimension": (saved_vector or {}).get("embedding_dim"),
        }

        session_id = f"phase6:{nonce}"
        await container.session_store.set_session(session_id, {"nonce": nonce}, ttl=60)
        checks["redis_session_roundtrip"] = await container.session_store.get_session(session_id) == {"nonce": nonce}
        checks["worker_heartbeat"] = await container.job_queue.worker_is_alive()

        job = await container.job_queue.enqueue("health_probe", {"nonce": nonce})
        probe_job_ids.append(job["_id"])
        deadline = asyncio.get_running_loop().time() + args.worker_timeout
        completed = None
        while asyncio.get_running_loop().time() < deadline:
            completed = await container.store.get("async_jobs", job["_id"])
            if completed and completed.get("status") in {"completed", "dead_letter"}:
                break
            await asyncio.sleep(0.2)
        checks["worker_job_roundtrip"] = bool(
            completed
            and completed.get("status") == "completed"
            and (completed.get("result") or {}).get("nonce") == nonce
        )
        checks["worker_job"] = {
            "status": (completed or {}).get("status", "timeout"),
            "attempts": (completed or {}).get("attempts"),
        }

        failing = await container.job_queue.enqueue("phase6_forced_failure", {"nonce": nonce})
        probe_job_ids.append(failing["_id"])
        deadline = asyncio.get_running_loop().time() + args.worker_timeout
        dead_job = None
        while asyncio.get_running_loop().time() < deadline:
            dead_job = await container.store.get("async_jobs", failing["_id"])
            if dead_job and dead_job.get("status") == "dead_letter":
                break
            await asyncio.sleep(0.2)
        dead_rows = await container.session_store.redis.xrevrange(
            container.job_queue.dead_letter_stream, count=20
        )
        for entry_id, fields in dead_rows:
            if fields.get("job_id") == failing["_id"]:
                dead_letter_entry_id = entry_id
                break
        checks["worker_retry_dead_letter"] = bool(
            dead_job
            and dead_job.get("status") == "dead_letter"
            and dead_job.get("attempts") == settings.async_job_max_attempts
            and len(dead_job.get("errors") or []) == settings.async_job_max_attempts
            and dead_letter_entry_id
        )

        required_indexes = {
            "documents": "dept_id_1_status_1",
            "chunks": "dept_id_1_doc_id_1_chunk_index_1",
            "vector_embeddings": "dept_id_1",
            "async_jobs": "status_1_created_at_1",
        }
        index_checks = {}
        for collection, index_name in required_indexes.items():
            info = await container.mongo.db[collection].index_information()
            index_checks[collection] = index_name in info
        checks["mongodb_indexes"] = index_checks

        def failed(value: object) -> bool:
            if isinstance(value, bool):
                return not value
            if isinstance(value, dict):
                return any(failed(item) for item in value.values() if isinstance(item, (bool, dict)))
            return False

        for name, value in checks.items():
            if failed(value):
                errors.append(f"检查未通过: {name}")
    except Exception as exc:  # noqa: BLE001 - 必须把完整阶段失败写入报告
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await container.vector_store.delete_by_doc(probe_doc_id)
            for probe_job_id in probe_job_ids:
                await container.store.delete("async_jobs", probe_job_id)
            if dead_letter_entry_id:
                await container.session_store.redis.xdel(
                    container.job_queue.dead_letter_stream, dead_letter_entry_id
                )
            await container.session_store.delete_session(f"phase6:{nonce}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"探针清理失败: {type(exc).__name__}: {exc}")
        await container.session_store.close()
        await container.mongo.close()

    return await _save(args.output, checks, timings, errors)


async def _save(output: Path, checks: dict, timings: dict, errors: list[str]) -> dict:
    report = {
        "status": "passed" if not errors else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "timings": timings,
        "errors": errors,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-timeout", type=float, default=30.0)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "docs" / "competition" / "wuhu_production_stack_verification.json",
    )
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
