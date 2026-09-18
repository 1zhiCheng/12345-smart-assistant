# 阶段 3：官方文档语义路由

## 验收结论

阶段 3 已完成。部门路由已从“高区分度事项词组 + 通用关键词 + LLM 失败后全部门”升级为：

1. 高区分度复合事项直接路由；
2. 本地 BGE 官方文档语义原型与通用关键词小权重融合；
3. 语义分支不可用时回退原关键词链路；
4. 本地语义和关键词均不可用时才调用 LLM，最终仍保留全部门安全降级。

冻结留出集 Top-1 从 62.50% 提升到 95.83%，Top-K 从 91.67% 提升到 95.83%，
达到路线要求的 85% / 95%。复评后未读取失败正文继续调参。

## 数据隔离

- 冻结集：`backend/evaluation/routing_holdout.frozen.json`，12 类、24 条。
- 原型数据：知识库清单中排除上述 24 份文档后的 240 份官方正文。
- 开发片：在 240 份非留出文档中，按
  `sha256(routing-dev-v1:relative_path)` 每类确定性切出约 20%，共 49 条。
- 固定策略只根据开发片选择；冻结集只在策略、参数、测试和原型隐私门禁通过后正式复评一次。
- 原型文件只保存归一化向量、模型/方法和计数，不保存官方文档正文或源路径。

## 固定策略

- 模型：`BAAI/bge-small-zh-v1.5`，512 维，本地只读加载。
- 每部门保留全部非留出官方文档向量，运行时取最相似 3 份的均值。
- 融合 10% 部门名称、类别和典型职责描述向量。
- 命中部门通用关键词时增加 0.08 分；高区分度复合事项仍保持直接命中。
- 输出前三个候选部门，供工作人员确认；系统不绕过人工审核直接转派。

## 结果

| 数据 | 样例 | Top-1 | Top-3 / Top-K |
| --- | ---: | ---: | ---: |
| 非留出开发片 | 49 | 93.88% | 95.92% |
| 冻结留出集（改造前） | 24 | 62.50% | 91.67% |
| 冻结留出集（语义融合） | 24 | 95.83% | 95.83% |

## 离线与降级

`sentence-transformers` 使用 `local_files_only=True` 加载 BGE，群众请求处理期间不会临时访问
Hugging Face。模型或原型缺失、模型名不一致、维度不匹配、向量计算异常时，语义分支返回空结果，
自动进入原关键词/LLM/全部门降级链路。

## 可复现命令

```powershell
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m scripts.build_semantic_routing_prototypes
.\.venv\Scripts\python.exe -m scripts.evaluate_routing_holdout `
  --output docs\competition\wuhu_routing_holdout_semantic_evaluation.json
```

冻结原型一旦生成不应随意覆盖；`--force` 仅用于开发片策略尚未锁定时的显式重建。

## 验收证据

- 原型：`backend/resources/routing_prototypes.bge-small-zh-v1.5.json`
- 原型 SHA-256：`30597B75573832D619CCACF4826AB094882E8C97E645D7914768CE145AB8021B`
- 冻结复评：`wuhu_routing_holdout_semantic_evaluation.json`
- 复评 SHA-256：`E81209992DEAAA252041EEA3700137DA30E6A5C0838E9E20C05C40B9DEA879E3`
- 本地断网式模型加载与 512 维向量检查：通过。
- 完整后端回归：116 passed，1 个第三方弃用警告。
