# 阶段 6：生产存储、真实向量、异步 Worker 联调与压测

## 验收结论

阶段 6 已完成。MongoDB、Redis Stream、本地 BGE embedding 与独立 worker 均使用真实进程
完成联调，不以 MemoryStore、hash embedding 或进程内假队列代替。264 份官方文档已通过生产解析、
chunk、embedding、Mongo 持久化链路入库；集成探针、失败重试和压力门禁全部通过。

本机没有 Docker，因此本轮在 Windows 上使用 MongoDB Community 7.0.43 官方发行包和支持
Redis 7.2 Stream 命令的 Windows 测试构建运行同构组件。最终 Linux/容器部署仍使用
`docker-compose.yml` 中的官方 `mongo:7.0`、`redis:7-alpine` 镜像；不能把本机测试进程视为
生产托管方案。

## 生产可靠性改造

- Mongo 客户端启动时主动 `ping` 并设置 server selection timeout；连接失败时生产模式 fail-fast。
- Redis 客户端设置连接/命令超时和周期健康检查；`/readyz` 动态检查 Mongo、Redis、真实向量
  provider 和独立 worker TTL 心跳。
- embedding 的 relay/local 失败默认抛错，只有显式配置
  `EMBEDDING_ALLOW_HASH_FALLBACK=true` 才允许开发降级；生产验收禁止该开关。
- Mongo 向量记录保存 provider、模型名和维度，维度不一致的历史向量不会参与打分。
- 1,884 条共享向量按维度构造成 NumPy 矩阵只读快照，以 30 秒 TTL 感知其他 Pod 的更新；
  避免每个请求从 Mongo 重传全量向量并在 Python 循环中阻塞事件循环。
- Redis Stream worker 支持 stale pending 自动接管、最多 3 次执行、错误历史、死信流和心跳；
  已完成/死信作业的重复消息会确认但不重复执行。
- backend/worker 容器均挂载离线 BGE 模型并增加健康检查，web 与可选 pi-agent 等待 backend
  真正 ready 后启动。

## 官方知识库生产入库

| 项目 | 结果 |
| --- | ---: |
| 官方文档 | 264 |
| 事项类别 | 12 |
| 各类别文档数 | 20–25 |
| 活动 chunks | 1,884 |
| Mongo 向量 | 1,884 |
| local BGE / 512 维向量 | 1,884 |
| hash 向量 | 0 |
| 入库失败 | 0 |

## 跨进程集成探针

`verify_production_stack.py` 使用唯一探针 ID 并在结束时精确清理，验证：

- MongoDB/Redis ping 和所需 Mongo 索引；
- BGE 512 维单位向量及相关语义排序；
- 向量写入、检索和 provider/model/dimension 元数据；
- Redis 会话 TTL 往返和 worker 心跳；
- Redis Stream 健康作业由独立 worker 一次完成；
- 强制失败作业执行 3 次后进入死信流。

最终报告状态为 `passed`。报告 SHA-256：
`0B966FB71F91AFED74127590BE072235AA828E93EDFDBF36231D9A1D9FEEAA61`。

## 压力门禁

固定数据为 1,884 条真实 BGE 向量；100 次向量检索并发 20，同时投递 100 个跨进程
Redis Stream 健康作业。

| 指标 | 结果 | 门槛 |
| --- | ---: | ---: |
| 向量检索错误率 | 0% | < 1% |
| 向量检索冷启动 | 72.54ms | 记录项 |
| 向量检索 P95 | 1.02ms | < 2,000ms |
| 向量检索吞吐 | 1,173.04 次/秒 | 记录项 |
| worker 作业错误率 | 0% | < 1% |
| worker 作业 P95 | 1,215.41ms | < 5,000ms |
| worker 吞吐 | 77.92 作业/秒 | 记录项 |
| worker 超时 | 0 | 0 |

第一次压测发现 Python 逐条余弦导致检索 P95 4,039.15ms，门禁未通过；改用 NumPy 矩阵
批量点积并加入并发安全快照后，以完全相同规模复测通过，没有放宽验收阈值。

最终压测报告 SHA-256：
`E93F0D12D878576D07DFD25BCCEA01707757D61859F9E0823318BB43EF023F15`。

## 验收证据

- `wuhu_production_stack_verification.json`
- `wuhu_production_stack_benchmark.json`
- `backend/scripts/verify_production_stack.py`
- `backend/scripts/benchmark_production_stack.py`
- 后端全量回归：142 passed。

上述延迟仅代表当前单机、1,884 向量规模，不外推为集群容量。K8s 多副本、Ingress 和外部 LLM
限流仍应在目标部署环境执行 k6 容量测试。
