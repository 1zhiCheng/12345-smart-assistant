# 部署与高并发方案

## 1. 本地开发（Docker，推荐）

```bash
cd program
cp .env.example .env     # 填写密钥
docker compose up --build
```

生产同构配置要求：

- 将 `models/bge-small-zh-v1.5/` 放入本地模型目录；容器内固定从
  `/app/models/bge-small-zh-v1.5` 加载，禁止请求时联网下载；
- `EMBEDDING_ALLOW_HASH_FALLBACK=false`，真实模型失败即停止启动；
- `/healthz` 只表示进程存活，`/readyz` 同时检查 MongoDB、Redis、真实向量和 worker 心跳；
- backend、worker 在 MongoDB/Redis 不可用时 fail-fast，不再以降级内存状态伪装成生产服务；
- Redis Stream 作业默认最多执行 3 次，崩溃消息 60 秒后由其他 worker 接管，最终失败进入
  `<ASYNC_STREAM_NAME>:dead`。

首次导入已通过质量门禁的官方知识库：

```bash
cd backend
python -m scripts.ingest_department_files --base ../wuhu_knowledge_base \
  --skip-conflicts --skip-metadata-llm
python -m scripts.verify_production_stack
python -m scripts.benchmark_production_stack --requests 100 --concurrency 20 --jobs 100
```

## 2. 生产部署（K8s）

`deploy/` 目录提供：

- `deploy/k8s/` —— 原生 YAML 清单（namespace / mongodb / redis / orchestrator / loop-engine / dept-agent / gateway / monitoring）
- `deploy/helm/wuhu-12345/` —— Helm Chart，模板化部署新部门 Agent

```bash
helm install wuhu-12345 deploy/helm/wuhu-12345 -n wuhu-12345 --create-namespace \
  -f values-private.yaml
```

### 部门 Agent 弹性伸缩

每个部门 Agent 是独立 Deployment + HPA，按 `wuhu12345_dept_agent_inflight` 自定义 Pods 指标伸缩：

- 低并发承办部门：`minReplicas=1`
- 高并发部门（城市管理/公安/市场监管等）：`minReplicas=2`、`maxReplicas=20`

```yaml
# 修改 values-private.yaml 中的 departments 列表后统一升级
helm upgrade wuhu-12345 deploy/helm/wuhu-12345 -n wuhu-12345 \
  -f values-private.yaml
```

## 3. 高并发关键设计

| 设计点 | 实现 |
|---|---|
| 部门级隔离 | 独立 Deployment + HPA，Pod 反亲和 |
| 共享检索 | Mongo 持久化真实 BGE 向量 + 30 秒进程内 NumPy 只读快照；新增向量最迟一个 TTL 被其他 Pod 读取 |
| 异步处理 | 入库/反馈唤醒/Loop 走 Redis Stream；支持 stale claim、3 次重试、死信流和 worker TTL 心跳；上传文件由 backend/worker 共享 RWX PVC |
| 会话记忆 | Redis TTL 状态只保存摘要、实体和 chunk ID；长期事件进入 Mongo TTL 集合 |
| 限流降级 | Ingress 限流；部门服务失败时使用共享检索降级并标记 degraded departments |
| 连接池 | motor 异步 MongoDB + redis.asyncio 连接池 |
| 预热 | 高峰前扩容 + 预加载热点 FAQ |

## 4. 可观测性

当前清单包含 Prometheus、Grafana 和 Prometheus Adapter；Loki/OpenTelemetry 尚未在仓库清单中落地。

- 指标端点：`/metrics`（prometheus_client）
- 部门 HPA 指标：`wuhu12345_dept_agent_inflight`
- 问答指标：`wuhu12345_query_latency_seconds`、`wuhu12345_answer_adoption_total`、`wuhu12345_skill_trigger_total`
- pi 执行指标：`wuhu12345_pi_agent_execution_total{agent,status}`，可区分 success/fallback/error/disabled
