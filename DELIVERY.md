# 芜湖12345智慧助手源码交付说明

本项目面向“12345热线工单智能生成与转派辅助智能体”比赛场景，交付受理、转派、政策检索、回复审核、评测和部署所需源码。

## 已包含

- `backend/`：FastAPI API、ASR/说话人处理、工单生成、分类转派、RAG、评测与测试
- `web/`：营业员、部门管理员、系统管理员三类工作台
- `services/pi-agent/`：可选概率性 Agent Runtime，默认关闭
- `wuhu_knowledge_base/`：经过质量门禁的芜湖官方政务文档
- `docs/competition/`：比赛契约、路线图和可重复运行的评测报告
- `deploy/`：芜湖12345命名空间的 Docker、Kubernetes、Helm、HPA 和监控配置
- `loadtest/`：生产联调阶段使用的负载测试脚本
- `.env.example`：不含真实密钥的生产配置模板

## 数据与模型边界

- 比赛原始录音、Excel 和网页资料仅保存在本地，不应提交到公开仓库。
- 外部 LLM 调用默认关闭；只有在完成授权和脱敏后才能设置 `INTAKE_LLM_ENABLED=true`。
- 默认使用本地 Embedding。启用外部 Embedding、重排或 pi Runtime 前必须确认数据外发范围。
- `SEED_DEMO_USERS` 和 `SEED_OPERATIONAL_DEMO_DATA` 在生产模板中均为 `false`。

## Docker 快速启动

```bash
cp .env.example .env
# 设置强数据库口令、AUTH_SECRET、INTERNAL_API_TOKEN，
# 并临时配置 BOOTSTRAP_ADMIN_USERNAME/BOOTSTRAP_ADMIN_PASSWORD。
docker compose config
docker compose up --build -d
docker compose exec backend python -m scripts.seed_data
docker compose exec backend python -m scripts.ingest_department_files \
  --base /app/wuhu_knowledge_base --skip-conflicts --skip-metadata-llm
```

默认命令不启动可选 pi Runtime；只有在完成数据外发授权并配置模型后，才使用
`docker compose --profile pi up --build -d`。

首次管理员创建成功后，应清空引导管理员密码并轮换部署 Secret。完整步骤见 `README.md` 和 `docs/deployment.md`。

## 不应打包

- `.env`、密钥、证书、真实群众录音和未脱敏工单
- `.venv/`、`node_modules/`、`.next/`、缓存和本地日志
- `data/`、MongoDB/Redis 数据卷和本地模型大文件
- 旧交付 ZIP、临时截图和测试浏览器缓存
