# 比赛改造路线

## 总体实施与验收表（2026-09-16 自检）

状态口径：`[x]` 表示代码、测试和文档均有验收证据；`[ ]` 表示仍需实现或人工/外部环境完成，
不能因为已经有界面或机器草稿就提前打勾。

| O / 任务方向 | 状态 | 实现方式 | 主要技术栈 | 证据与剩余工作 |
| --- | :---: | --- | --- | --- |
| O1 录音与文本诉求输入 | [x] | 文本录入、录音上传、播放器、本地转写、失败人工补录 | Next.js、FastAPI、faster-whisper turbo | 18/18 真实录音可处理；见阶段 2、阶段 8 |
| O1 接线员/群众角色区分 | [x] | CAM++ 声纹聚类，低置信度回退并要求人工校对 | CAM++ ONNX、faster-whisper 时间戳 | 18 条机器草稿中 17 条检出双角色；1 条列为重点复核 |
| O1 要素抽取与标准工单 | [x] | LLM 结构化抽取与证据校验，异常时规则降级，统一 `StandardWorkOrder` | FastAPI、Pydantic、DeepSeek/OpenAI 兼容接口、规则引擎 | 文本/录音均可形成可编辑草稿；见 `intake-contract.md` |
| O1 缺失追问与多事项拆分 | [x] | 缺失字段、歧义、多事项候选及子工单状态管理 | Python、Pydantic、React | 定向测试覆盖，多轮补充后重新生成草稿 |
| O1 隐私与人工确认 | [x] | 脱敏、逐字段原文证据、草稿禁止自动转派 | 正则脱敏、RBAC、审计状态机 | `draft → confirmed`，外部模型默认关闭 |
| O2 12 类事项分类与部门 Top-K | [x] | 高区分度规则 + 官方文档 BGE 多原型 + 关键词融合 + 安全降级 | BGE-small-zh-v1.5、NumPy、DeptRouter | 冻结留出集 Top-1 / Top-K 95.83% / 95.83%；见阶段 3 |
| O2 工单交接与处置队列 | [x] | confirmed 工单入队、部门领取、处理中与办结回写 | FastAPI、MongoDB、Redis Stream、Worker | 显式状态机和三角色权限已实现 |
| O2 官方知识库与混合检索 | [x] | 264 份官方正文、语义切片、BM25 + BGE 混合检索、chunk 引用 | MongoDB、BGE、BM25、Python Chunker | 1,884 chunks；Recall@5 100%、MRR 0.875；见阶段 4 |
| O2 政策回复与合规门禁 | [x] | 带来源回复、重写/降级、引用/数字/隐私/越权承诺检查、人工终审 | RAG、LLM、`ReplyComplianceGate` | 离线门禁和模型烟测均通过；见阶段 5 |
| O3 旧学校/文书场景清理 | [x] | 活跃代码、提示词、部署和角色模型迁移到芜湖 12345 | Python、TypeScript、Docker、K8s、Helm | 仅保留明确标记的兼容清理常量和历史设计输入；见阶段 1 |
| O3 生产同构存储与任务栈 | [x] | Mongo 持久化、Redis Stream 重试/死信/心跳、真实向量和独立 Worker | MongoDB 7、Redis 7、BGE、NumPy、Docker | 100 次检索与100个作业压测零错误；见阶段 6 |
| O3 桌面语义运行态与自检 | [x] | 本地 BGE 向量缓存、双击启动、后台运行态面板与健康自检 | SentenceTransformers、NumPy、FastAPI、Next.js | 桌面实测 1,891/1,891 条真实向量已加载；见阶段 9 |
| O3 真实录音机器预标注 | [x] | 本地 ASR、角色时间片、工单 Agent 建议逐条落盘，可中断续跑 | faster-whisper、CAM++、FastAPI/Python CLI | 18/18 `machine_draft`、0 失败；私有文件不进 Git |
| O3 真实录音人工金标与 CER/DER | [ ] | 工作台逐条听审、修改、填写标注人并批准，再执行一次冻结复评 | HTML5 Audio、离线 JSON、BGE 语义评测 | 当前 0/18 `approved`；机器草稿不能替代人工金标 |
| O3 合成录音字段质量门槛 | [ ] | 现有字符阈值通过率 38.89%，另有语义字段 F1 79.78% | LLM Intake、字符/语义双评测、BGE | 原定“字符通过率 ≥60%”未达到；需在非冻结开发集改进抽取或正式重定义门禁，不能混用指标 |
| O4 比赛材料与答辩文档 | [x] | 方案、需求映射、5分钟脚本、答辩问答、12页 PPT | Markdown、python-pptx/Artifact Tool | 工程材料已生成并渲染验收；见阶段 7 |
| O4 最终提交与现场环境 | [ ] | 填团队信息、录视频、准备有效部署和账号、生成合规压缩包 | PowerPoint、视频工具、目标部署环境 | 属于人工/外部环境事项，见 `submission-checklist.md` |

### 当前优先顺序

1. 完成 18 条真实录音人工复核，优先处理 `official-health-001` 的未知角色片段。
2. 人工金标冻结后只执行一次 CER、DER、语义字段和路由正式复评，不据此继续调参。
3. 在合成/非冻结开发集继续提升工单抽取，明确“字符通过率”与“语义 F1”各自门槛。
4. 在目标评审环境完成含 ASR 和模型调用的全链路容量测试。
5. 完成团队信息、演示视频、有效账号与最终提交包等人工事项。

## Iteration 1：诉求受理可运行基线

- [x] 统一标准工单 Schema
- [x] 文本诉求要素提取
- [x] 缺失信息与歧义提示
- [x] 敏感信息脱敏
- [x] 标准工单生成与人工确认
- [x] 受理工作台
- [x] 真实 ASR 服务适配（本地 faster-whisper turbo / OpenAI 兼容接口）
- [x] CAM++ 本地双人声纹分离、角色转写与低置信度人工校对
- [x] 基于官方 Excel 的 12 类、18 条脱敏评测集

## Iteration 2：诉求理解质量提升

- [x] LLM 结构化提取器与规则基线融合
- [x] 多轮追问状态管理
- [x] 多事项识别、原文证据审核与子工单草稿生成
- [x] 字段准确率、完整率、忠实度评测脚本（待导入官方脱敏样例扩充数据集）
- [x] 录音转写校对和失败降级

## Iteration 3：分类转派与回复闭环

- [x] confirmed 工单接口交接与显式状态机
- [x] 处置待办队列、领取和结果回写接口
- [x] 将分类器接入结果回写接口
- [x] 政策依据与回复审核工作台（分类、部门 Top-K、官方文档引用、回复人工修改）
- [x] 市场监管与生态环境两类诉求端到端联调；最终办结仍保留人工确认

## Iteration 4：比赛质量门禁（当前下一阶段）

- [x] 冻结独立路由留出集：12 类、24 份未参与词表调优的官方文档
- [ ] 将合成语音工单字段通过率提升至 60% 以上；当前纯规则缓存复评为 23.15%，生产大模型复评为 38.89%（36/36 模型生成成功）
- [x] 建立录音人工标注契约、离线标注工作台和 CER/角色序列/DER/语义字段 F1 评测；合成开发集语义字段 F1 为 79.78%，不替代字符阈值或真实录音成绩
- [ ] 完成18条冻结真实录音的脱敏逐字稿与说话人时间戳双人复核；18/18 已有机器草稿，
  当前人工批准为 0/18，完成前不计算正式 CER/DER
- [x] 完成 12 类确定性路由覆盖；36 条开发集 Top-1 / Top-K 为 100% / 100%（非泛化成绩）
- [x] 在新留出集上验证部门 Top-1 / Top-K 达到 85% / 95% 以上；语义融合复评为 95.83% / 95.83%
- [x] 为政策回复评测补齐 citation correctness 与 answer consistency，已形成可重复运行的离线基线
- [x] 通过完整语义切片降级回答与多查询融合，将答案关键点一致性从 42.42% 提升至 81.82%
- [x] 为仅含机构元数据的 12315 文档补采职责正文
- [x] 使用 MongoDB、Redis、真实 BGE embedding 与独立异步 Worker 完成生产同构联调和压力门禁

## Iteration 5：按门禁顺序完成比赛交付

- [x] 阶段 1：清理旧文枢/高校场景残留并完成 12345 生产 Docker、K8s、Helm 安全默认配置
- [x] 阶段 2：建立真实比赛录音端到端评测集并输出可复现报告
- [x] 阶段 3：在不调参冻结集的前提下提升语义路由泛化并复评
- [x] 阶段 4：每个承办部门补齐不少于 20 份官方正文文档，并补采 12315 职责正文
- [x] 阶段 5：建立政策回复合规性评测和发布门禁
- [x] 阶段 6：完成 MongoDB、Redis、本地真实 embedding、异步 Worker 联调与压测
- [x] 阶段 7：整理比赛方案、演示脚本和答辩材料
- [ ] 阶段 8：18 条真实录音已全部生成 `machine_draft`，仍需人工批准并执行一次冻结复评

阶段 1 验收证据见 `phase1-production-migration.md`，阶段 2 验收证据见
`phase2-official-audio-evaluation.md`，阶段 3 验收证据见 `phase3-semantic-routing.md`，
阶段 4 验收证据见 `phase4-official-knowledge-base.md`，阶段 5 验收证据见
`phase5-reply-compliance-gate.md`，阶段 6 验收证据见 `phase6-production-stack.md`，
阶段 7 验收证据见 `phase7-competition-delivery.md`。七个阶段已全部完成；提交前仍需按
`submission-checklist.md` 填写团队信息、录制演示视频并核验评审环境。

阶段 8 属于交付后的质量增强，验收证据见 `phase8-audio-annotation.md`。它不改变阶段 2 的真实
录音冻结基线，也不把合成开发成绩写入真实录音结论。

阶段 9 补齐桌面演示运行态的可观察与可验证性，验收证据见
`phase9-desktop-runtime-observability.md`；它不改变生产环境的依赖门禁，也不替代阶段 6 的持久化栈压测。

## 统一端到端责任

- 一条链路统一覆盖 ASR/说话人分离、字段抽取、分类、部门 Top-K、RAG 政策依据和回复草稿。
- `intake-contract.md` 继续作为模块边界，便于分别测试受理和处置阶段，但不再代表人员分工。
- 不得绕过人工确认直接转派草稿；模型与规则输出都必须保留来源证据和可审计状态。

当前处置工作台位于系统/部门管理员侧“工单处置”。智能研判结果保存为
`b_recommendation`；只有管理员领取工单并审核分类、承办部门和回复正文后，才可提交
`completed` 结果。

## 当前评测基线

- 官方脱敏工单开发验证集：12 类、18 条，见 `backend/evaluation/intake_cases.official.deidentified.json`。
- 离线规则基线：字段准确率 1.000、信息完整率 1.000、事实忠实度 1.000，报告见 `wuhu_intake_evaluation.rules.json`。
- 官方开发验证集路由：优化前 Top-1 / Top-K 为 44.44% / 66.67%，当前为 100% / 100%；前后报告分别见 `wuhu_official_routing_baseline_before_tuning.json`、`wuhu_official_validation_evaluation.json`。
- 合成录音规则缓存复评：地点相似度 0.6622、区域准确率 100%、字段通过率 23.15%、内容支持精确率 93.89%，报告见 `wuhu_synthetic_audio_reevaluation.json`。
- 合成录音生产大模型复评：36/36 模型生成成功、无规则降级，地点相似度 0.7120、区域准确率 100%、字段通过率 38.89%、内容支持精确率 79.29%，12 类开发集路由 Top-1 / Top-K 100% / 100%，报告见 `wuhu_synthetic_audio_production_reevaluation.json`。当前字段通过率采用字符相似度硬阈值，能够严格检出表达差异，但会低估语义等价改写，后续需增加独立语义评分而不能直接放宽阈值。
- 合成录音本地 BGE 语义复评：36/36，CER 6.39%、角色序列准确率96.67%、语义字段 F1 79.78%、召回率81.48%、支持精确率79.01%；因无人工时间戳，DER 明确标记为不可用。报告见 `wuhu_audio_semantic_evaluation.json`。
- 官方文档 RAG：Recall@5 100%、MRR 0.875、引用正确率 100%、答案关键点一致性 95.45%，报告见 `wuhu_rag_evaluation.json`。
- 政策回复发布门禁：11 条离线官方回复通过率 100%、关键点一致性 95.45%；3 条已配置大模型烟测通过率、引用正确率和关键点一致性均为 100%，所有草稿仍必须人工审核，报告见 `wuhu_reply_compliance_evaluation.json` 和 `wuhu_reply_compliance_llm_smoke.json`。
- 生产同构栈：264 份官方文档、1,884 个 chunk/真实 BGE 512 维向量；100 次并发检索零错误、P95 1.02ms，100 个 Redis Stream worker 作业零错误、P95 1.215s，报告见 `wuhu_production_stack_verification.json` 和 `wuhu_production_stack_benchmark.json`。
- 冻结路由留出集：12 类、24 份官方文档，Top-1 / Top-K 为 62.5% / 91.67%，报告见 `wuhu_routing_holdout_evaluation.json`；本轮不再根据失败明细调词表。
- 官方文档语义路由：排除冻结集的 240 份官方正文生成本地 BGE 多原型，49 条开发片为 93.88% / 95.92%；冻结集唯一正式复评为 95.83% / 95.83%，报告见 `wuhu_routing_holdout_semantic_evaluation.json`。
- 比赛真实录音离线基线：18/18 完成、112.42 分钟语音、ASR 成功率 100%、双角色检出率 94.44%、字段通过率 5.55%、部门 Top-1 / Top-K 为 33.33% / 50.00%，报告见 `wuhu_official_audio_e2e_evaluation.json`。该集仅作为未调参真实基线，不再根据失败明细调规则。
- 上述结果只代表当前开发数据，规则已参考这些样例调优，不能视为未知数据上的泛化成绩；新增官方样例后必须冻结为独立留出集重新评测。
