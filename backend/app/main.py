"""芜湖市12345政务 Agent 后端入口（FastAPI）。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import generate_latest
from starlette.responses import JSONResponse, Response

from app.api.router import api_router, root_router
from app.config import get_settings
from app.deps import build_container
from app.utils.logging import get_logger, setup_logging
from app.loop.default_skills import seed_default_skills
from app.domain.wuhu import seed_wuhu_departments
from app.demo_seed import seed_operational_demo_data
from app.retrieval.desktop_vector_cache import load_or_build_desktop_vectors
from app.async_jobs.local_worker import serve_memory_queue

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    container = build_container(settings)
    app.state.container = container

    # 连接外部依赖（memory 模式跳过）
    if settings.storage_mode == "mongo":
        try:
            await container.mongo.connect()
        except Exception as exc:  # noqa: BLE001
            logger.error("MongoDB 连接失败: %s", exc)
            if settings.dependency_fail_fast:
                raise RuntimeError("MongoDB 是生产必需依赖，启动已终止") from exc
    if settings.storage_mode == "mongo" and hasattr(container.session_store, "connect"):
        try:
            await container.session_store.connect()
        except Exception as exc:  # noqa: BLE001
            logger.error("Redis 连接失败: %s", exc)
            if settings.dependency_fail_fast:
                raise RuntimeError("Redis 是生产必需依赖，启动已终止") from exc

    # 生产启动时实际加载并调用 embedding；禁止配置写 local、运行却悄悄使用 hash。
    if settings.storage_mode == "mongo":
        try:
            vectors = await container.embeddings.embed(["芜湖市12345政务服务向量健康检查"])
            if container.embeddings.last_effective_provider in {None, "hash", "hash-fallback"}:
                raise RuntimeError("生产环境未使用真实 embedding provider")
            logger.info(
                "真实向量模型就绪: provider=%s model=%s dim=%d",
                container.embeddings.last_effective_provider,
                settings.embedding_model,
                len(vectors[0]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("真实向量模型不可用: %s", exc)
            if settings.dependency_fail_fast:
                raise RuntimeError("真实向量是生产必需依赖，启动已终止") from exc

    # 种子芜湖 12345 部门与默认 Rules / Hooks
    created_departments = await seed_wuhu_departments(container.store)
    if created_departments:
        logger.info("已创建 %d 个芜湖政务部门", created_departments)
    await container.rule_engine.seed_defaults()
    await container.hook_engine.seed_defaults()
    seeded_skills = await seed_default_skills(container.store)
    if seeded_skills:
        logger.info("已创建 %d 个可执行基线 Skills", seeded_skills)

    # 种子比赛演示账号（营业员/部门管理员/系统管理员）
    await container.auth.seed_users()

    # 离线比赛 Demo 使用现有官方知识库和脱敏评测结果恢复一组可追溯运行态。
    # Mongo/生产环境绝不自动注入演示业务数据。
    if settings.storage_mode == "memory" and settings.seed_operational_demo_data:
        seeded_demo = await seed_operational_demo_data(container.store)
        if seeded_demo:
            logger.info("已加载可追溯演示运行数据: %s", seeded_demo)

    # 回填部门 Loop 阶段与审核统计字段（兼容旧数据）
    await _backfill_departments(container)

    # 重建内存检索索引（BM25 + 向量）：MongoDB 中已入库的文档可跨进程/重启被检索
    try:
        chunks = await container.store.list_active_chunks()
        if chunks:
            for c in chunks:
                container.bm25.add(c)
            # 共享 Mongo 向量库已持久化，无需每个 Pod 启动时重复嵌入全量文档。
            if settings.vector_backend != "mongo":
                if settings.storage_mode == "memory" and settings.embedding_provider == "local":
                    vectors, cache_status = await load_or_build_desktop_vectors(
                        container.embeddings, chunks, settings.desktop_embedding_cache_path
                    )
                    logger.info("桌面 BGE 向量缓存: %s", cache_status)
                else:
                    vectors = await container.embeddings.embed([c["content"] for c in chunks])
                for c, v in zip(chunks, vectors):
                    await container.vector_store.add(
                        c["embedding_id"],
                        v,
                        {"doc_id": c["doc_id"], "dept_id": c["dept_id"], "chunk_index": c["chunk_index"]},
                    )
            logger.info("重建检索索引完成: %d chunks (BM25 + 向量)", len(chunks))
        else:
            logger.info("无已入库文档，跳过检索索引重建")
    except Exception as exc:  # noqa: BLE001
        logger.warning("重建检索索引失败(%s)，检索可能不完整", exc)

    # 桌面版没有 Redis Stream/独立 Worker，仍要让“上传文档、反馈、Loop”真正从 queued
    # 走到 completed。这里复用生产 Worker 的处理函数，但仅在 memory 模式启用；Mongo 模式
    # 继续强制使用独立 Worker 和 /readyz 心跳门禁，不能被应用内线程掩盖。
    desktop_worker_stop: asyncio.Event | None = None
    desktop_worker_task: asyncio.Task | None = None
    if settings.storage_mode == "memory":
        desktop_worker_stop = asyncio.Event()
        desktop_worker_task = asyncio.create_task(
            serve_memory_queue(container, desktop_worker_stop), name="wuhu-desktop-memory-worker"
        )
        logger.info("桌面内存作业消费者已启动（仅用于本机演示）")

    logger.info("%s 启动完成 (storage=%s)", settings.app_name, settings.storage_mode)
    yield

    # 关闭
    if desktop_worker_stop is not None:
        desktop_worker_stop.set()
    if desktop_worker_task is not None:
        desktop_worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await desktop_worker_task
    if settings.storage_mode == "mongo":
        await container.mongo.close()
    if settings.storage_mode == "mongo" and hasattr(container.session_store, "close"):
        await container.session_store.close()
    await container.pi_runtime.close()
    logger.info("应用已关闭")


async def _backfill_departments(container) -> None:
    """为已存在的部门补充 loop_phase / review_stats / fade_out 字段（幂等）。"""
    from datetime import datetime, timezone

    try:
        for dept in await container.store.list_departments():
            changed = False
            if "loop_phase" not in dept:
                dept["loop_phase"] = "human_in_loop"
                changed = True
            if "review_stats" not in dept:
                dept["review_stats"] = {"total": 0, "correct": 0, "accuracy": 0.0}
                changed = True
            if "admin_users" not in dept:
                dept["admin_users"] = []
                changed = True
            if changed:
                dept["updated_at"] = datetime.now(timezone.utc).isoformat()
                await container.store.upsert_department(dept)
        logger.info("部门 Loop 阶段字段回填完成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("部门字段回填失败: %s", exc)


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """将 Pydantic 默认 422 详情转换为界面可直接展示的中文校验提示。"""
    labels = {"username": "账号", "password": "密码", "name": "姓名"}
    issues: list[str] = []
    for error in exc.errors():
        loc = error.get("loc", [])
        field = str(loc[-1]) if loc else "参数"
        label = labels.get(field, field)
        error_type = str(error.get("type", ""))
        context = error.get("ctx") or {}
        if error_type == "missing":
            message = f"{label}不能为空"
        elif error_type == "string_too_short":
            message = f"{label}至少需要 {context.get('min_length', '')} 位"
        elif error_type == "string_too_long":
            message = f"{label}不能超过 {context.get('max_length', '')} 位"
        elif error_type == "string_pattern_mismatch" and field == "username":
            message = "账号仅支持字母、数字和下划线"
        else:
            message = f"{label}格式不正确"
        if message not in issues:
            issues.append(message)
    return JSONResponse(
        status_code=422,
        content={"detail": {"message": "注册信息不符合要求", "issues": issues or ["请检查输入内容"]}},
    )

# CORS：显式来源列表；通配符来源不允许携带凭据（浏览器规范）
_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(root_router)
app.include_router(api_router)


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type="text/plain; version=0.0.4")


@app.get("/")
async def index():
    return {"app": settings.app_name, "docs": "/docs", "health": "/healthz"}
