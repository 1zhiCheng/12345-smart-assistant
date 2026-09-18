# 场景迁移与 RAG 入库说明

## 迁移范围

项目运行场景已从高校行政问答迁移为芜湖市 12345 热线工单辅助。旧高校部门、学校制度语料、对应 chunk 和向量已从运行数据库移除；删除前分别保留数据库 JSON 备份和原文件压缩包，备份目录已加入 `.gitignore`，不会被误推送到公开仓库。

登录角色统一为：

- `operator`：营业员，负责录音转写、诉求整理、工单生成和人工确认。
- `department_admin`：部门管理员，只治理所属部门文档、审核单和反馈。
- `system_admin`：系统管理员，管理全局部门、知识资产和 Loop 策略。

## 官方知识库

知识源目录为 `wuhu_knowledge_base/`，按比赛的 12 个类别组织：城市管理、城乡建设、公共安全、公共服务、交通运输、经济财贸、科教文体、劳动和社会保障、农林水土、生态环境、市场监管、卫生健康。

每份文档保留标题、类别、牵头部门、发布部门、发布日期、原始 URL、抓取时间和质检状态。入库时 Markdown 首个一级标题作为真实文档标题，采集元数据不会混入正文 chunk。

## Chunk 与混合检索

入库流程为：解析正文与层级标题 → 按段落和条款切分 → 短块合并并控制最大长度 → 生成包含标题、类别、部门、章节路径和正文的 `retrieval_text` → 写入 MongoDB → 使用本地 `BAAI/bge-small-zh-v1.5` 生成 512 维向量。

在线检索同时执行：

1. jieba 分词后的 BM25 关键词检索；
2. 本地中文 bge 语义向量检索；
3. 使用 RRF 融合两路排名，并保留每个结果的分支得分和来源；
4. 将结果连同官方来源 URL 交给回答智能体，要求结论逐项引用，证据不足时明确拒绝编造。

## 当前持久化结果

- 部门：12 个，旧高校部门 0 个。
- 官方文档：264 份，12 个事项类别均不少于 20 份（20–25 份/类）。
- 质量门禁：累计隔离 26 份采购、预决算、培训、征求意见稿、错分类或重复材料；隔离文件保存在 `_quarantine`，不进入检索。
- 活跃 chunk：1561 个。
- 向量：1561 条，全部为 512 维；无孤儿 chunk、无孤儿向量。
- 缺失来源 URL、缺失 `retrieval_text`、孤儿 chunk、孤儿向量：均为 0。
- 12 类检索评测：Recall@5 = 1.000，MRR = 0.875；Top-5 结果均包含 BM25 与 vector 两个检索分支。
- 11 条可回答问题的离线回复评测：引用正确率 = 1.000，答案关键点一致性 = 0.8182。12315 机构页缺少职责正文，暂只纳入检索指标，不虚构答案标签。

评测明细见 `docs/competition/wuhu_rag_evaluation.json`。

## 可复现命令

```powershell
$env:PYTHONPATH='backend'
python -m scripts.ingest_department_files --base .\wuhu_knowledge_base --skip-conflicts --skip-metadata-llm
python -m scripts.audit_wuhu_rag
python -m scripts.evaluate_rag --dataset .\backend\evaluation\real_document_qa.json --top-k 5 --output .\docs\competition\wuhu_rag_evaluation.json
```

生产运行必须配置 MongoDB；`hash` embedding 只供单元测试，不能用于比赛知识库。
