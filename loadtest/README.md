# 部门 Agent 弹性负载测试

分别将目标部门 Deployment 固定为 1 和 20 副本，在相同环境、模型和数据集上执行：

```bash
k6 run --summary-export one.json department-agent.js
k6 run --summary-export twenty.json department-agent.js
python3 compare_results.py one.json twenty.json
```

通过标准：错误率低于 1%、P95 小于 8 秒，20 副本的有效吞吐至少达到 1 副本的 2 倍。
实际增益受外部 LLM 限流影响，因此生产压测需同时观察模型侧 TPM/RPM。

MongoDB、Redis Stream、真实 BGE 与独立 worker 的基础压力门禁不调用外部 LLM：

```bash
cd backend
python -m scripts.verify_production_stack
python -m scripts.benchmark_production_stack --requests 100 --concurrency 20 --jobs 100
```

默认门槛为检索/worker 错误率均低于 1%，检索 P95 小于 2 秒，worker 完成 P95 小于 5 秒。
