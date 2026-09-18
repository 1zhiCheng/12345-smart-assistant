# 阶段 9：桌面语义运行态与可观测性

## 目标

让可双击运行的桌面演示版与系统的可信 RAG 口径一致：管理员无需查看日志或源码，即可确认
当前是否真的加载了本地语义向量、官方知识库切片数量、缓存状态以及桌面/生产同构模式边界。

## 实现

1. `scripts/desktop_launcher.ps1` 在检测到 `models/bge-small-zh-v1.5` 后，固定以
   `EMBEDDING_PROVIDER=local`、`EMBEDDING_ALLOW_HASH_FALLBACK=false` 启动；模型缺失才显式提示
   并安全降级到 hash，避免将降级检索伪装成语义检索。
2. 启动阶段使用 `backend/app/retrieval/desktop_vector_cache.py` 对**官方公开 Chunk**的 BGE 向量
   建立带模型名和内容指纹的缓存；Chunk 内容或模型变更会自动重建，缓存损坏同样会安全重建。
3. `/api/v1/admin/dashboard` 新增非敏感 `runtime` 快照：运行模式、存储与向量后端、Embedding provider/
   model、官方切片数、实际装载向量数、桌面 BGE 缓存和 Worker 门禁状态。
4. 管理后台首页新增“运行与检索状态”面板。缓存命中时虽然不必再次加载模型，但仍会正确显示
   “真实语义向量已启用”，不会被误报为 hash 降级。
5. 桌面内存模式增加应用内本地作业消费者，复用生产 `async_worker` 的处理函数。这样文档入库、
   反馈和 Loop 作业会实际从 `queued` 变为 `completed`；Mongo/Redis 模式不启用它，仍由独立
   Redis Stream Worker 与心跳门禁负责处理。

## 本机验收（2026-09-16）

桌面启动器重启后，以系统管理员演示账号读取 `/api/v1/admin/dashboard`，得到：

| 项目 | 实测值 |
| --- | --- |
| `runtime.mode` | `desktop_demo` |
| Embedding provider | `local` |
| `uses_real_vectors` | `true` |
| `vector_count` / `official_chunk_count` | `1891 / 1891` |
| `desktop_vector_cache.ready` | `true` |
| 前端 / 后端健康检查 | HTTP 200 / HTTP 200 |
| 后端回归 | `151 passed` |
| 前端类型检查 | `tsc --noEmit --incremental false` 通过 |

随后新增桌面本地消费者并复跑回归，结果为 `152 passed`。管理员实测触发一个 `run_loop` 作业：
作业由 `queued` 自动进入 `completed`，处理 6 条脱敏反馈、识别 1 个 bad case、生成 1 条 Rule 和
1 条 Hook 候选。全局阶段为“人在环中”，因此发布数为 0，候选不会绕过管理员审核直接改变策略。

## 边界

- 上述结果证明桌面版本实际使用项目随附的本地 BGE 语义向量，并不等同于部署到真实政务生产网络。
- 生产同构栈的 MongoDB、Redis Stream、独立 Worker 与压测验证，见
  `phase6-production-stack.md` 及其 JSON 报告；生产模式仍由 `/readyz` 对 MongoDB、Redis、Worker 和
  非 hash 向量逐项门禁。本机未安装这些服务时，不能把桌面模式写成生产集群运行。
