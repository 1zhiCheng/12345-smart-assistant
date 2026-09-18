# 脚本

| 脚本 | 用途 |
|---|---|
| `seed_data.py` | 幂等种子数据：芜湖 12 部门 / 12345 术语 / Rules&Hooks / 3 条比赛基线 Skill |
| `doctor.py` | 检查 DeepSeek、中转站、Embedding/Reranker 等模型连接 |
| `ingest_department_files.py` | 将 `wuhu_knowledge_base` 官方文档切分并写入混合检索索引 |
| `migrate_wuhu_competition.py` | 备份并清理旧高校部门、文档、chunk、向量和账号 |
| `loop_worker.py` | 旧版定时 Worker（兼容保留；生产使用 `async_worker.py`） |
| `async_worker.py` | Redis Stream Worker：文档入库、反馈唤醒、Loop |
| `evaluate_rag.py` | 真实部门文档评测：Recall@5 / MRR / 引用正确率 / 答案一致性 |
| `audit_wuhu_rag.py` | 审计部门、文档、chunk、向量维度、官方来源和孤儿数据 |
| `evaluate_intake.py` | 成员 A 工单评测：字段准确率 / 信息完整率 / 事实忠实度，支持规则与 LLM 模式 |
| `migrate_memory.py` | 旧四层大文档记忆迁移到事实平面 + 五个记忆平面 |
| `wait_for_deps.py` | 启动前等待 MongoDB/Redis 就绪（Docker） |
| `audio_annotation_dataset.py` | 初始化/校验录音标注，使用本地 BGE 计算 CER、DER 与语义字段 F1 |

用法：

```bash
python -m scripts.seed_data
# 芜湖政务官方知识库（批量首次建库建议跳过逐文档 LLM 调用）
python -m scripts.ingest_department_files --base ../wuhu_knowledge_base --skip-conflicts --skip-metadata-llm
python -m scripts.migrate_memory
python -m scripts.async_worker
python -m scripts.evaluate_rag --output evaluation-report.json
python -m scripts.evaluate_rag --dataset ../backend/evaluation/real_document_qa.json --output ../docs/competition/wuhu_rag_evaluation.json
```

### 录音人工标注与语义评测

```bash
python -m scripts.audio_annotation_dataset init-synthetic
python -m scripts.audio_annotation_dataset init-official
python -m scripts.audio_annotation_dataset validate backend/evaluation/audio_annotations.synthetic.dev.json
python -m scripts.audio_annotation_dataset score
```

离线图形标注工作台为 `audio_annotation_workbench.html`。真实录音模板只写入被 Git 忽略的
`local_evaluation/`。完整说明见 `docs/competition/audio-annotation-guide.md`。
### 芜湖知识库增量采集与质检

- `crawl_wuhu_knowledge.py`：仅采集 `*.wuhu.gov.cn` 官方页面，支持栏目增量发现和人工核验 URL 清单。
- `quality_gate_wuhu_knowledge.py`：强制每类最低文档数，将低价值材料移动到可恢复的 `_quarantine`。
- `sync_wuhu_quality_rejections.py`：将隔离材料在 MongoDB 中归档并移除其向量索引。
- `audit_wuhu_rag.py`：核验 12 个部门、活跃文档、切片、向量维度和孤儿数据。
