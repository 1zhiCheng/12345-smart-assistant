# 录音人工标注与语义评测指南

## 数据边界

- 36 条合成双人通话属于开发集，可以反复分析和调试。
- 18 条比赛真实录音属于冻结测试集。只允许补充人工逐字稿和说话人时间戳，不得根据失败内容
  修改规则、提示词或阈值后继续把同一批数据称为泛化测试。
- 真实录音标注保存在 `local_evaluation/`，该目录已被 Git 忽略。不得把录音、逐字稿、真实姓名、
  电话、身份证号或详细住址写入公开报告。

## 初始化

```powershell
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m scripts.audio_annotation_dataset init-synthetic
.\.venv\Scripts\python.exe -m scripts.audio_annotation_dataset init-official
```

生成结果：

- `backend/evaluation/audio_annotations.synthetic.dev.json`
- `local_evaluation/audio_annotations.official.private.json`

## 半自动预标注

推荐先让本地 ASR 和工单 Agent 生成机器草稿，再由人工逐条修改：

```powershell
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m scripts.audio_annotation_dataset prefill-official
```

默认全程本地运行，输出到
`local_evaluation/audio_annotations.official.prefilled.private.json`。每处理完一条都会落盘，可以加
`--resume` 续跑。机器结果统一标记为 `machine_draft`，不会进入正式评测；只有人工听录音核对、填写
标注人并改为 `approved` 后，才会成为标准答案。

如显式使用 `--intake-mode production --allow-external-llm`，脱敏后的 ASR 文本会发送给已配置的
外部大模型。真实热线可能包含个人信息，使用前必须完成数据合规确认。

## 使用离线标注工作台

直接双击 `backend/scripts/audio_annotation_workbench.html`，或启动只监听本机的静态服务器：

```powershell
.\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory backend\scripts
```

打开 <http://127.0.0.1:8765/audio_annotation_workbench.html>，然后：

1. 优先载入半自动生成的 `local_evaluation/audio_annotations.official.prefilled.private.json`；如尚未运行
   预标注，再载入 `audio_annotations.official.private.json`。
2. 点击“选择录音文件夹”，选择比赛数据集中的“信件类别示例工单及录音”根目录。页面按
   JSON 的 `audio_ref` 自动匹配 18 条录音；“选择当前音频”只用于单条兜底。
3. 校对脱敏逐字稿，按段填写接线员或群众、开始秒和结束秒。
4. 把时间、地点、事件、诉求拆成每行一个可独立核验的要点。
5. 填写标注人，点击“批准并下一条”。批准前会自动执行当前记录校验；真实冻结录音只要存在
   缺失时间戳、`unknown` 角色或未同时标出接线员/群众，就不会写成 `approved`。
   `machine_draft` 只是候选内容，不得直接用于评测。
6. 校验全部并导出 JSON，覆盖前先保留备份。音频文件不会写进导出的 JSON，也不会上传。

为提高听审效率，工作台提供 0.75×—2× 播放速度、单个时间片“播放”、机器草稿数量、低置信度
角色风险标记，以及机器字段/部门建议与官方字段的并排参考。机器建议只用于发现差异，不会覆盖
比赛 Excel 已有标注。建议先复核 `official-health-001`：该条声纹分离模式为 `none`、置信度
0.2478，24 个时间片当前均为 `unknown`。

## 校验与评测

```powershell
.\.venv\Scripts\python.exe -m scripts.audio_annotation_dataset validate `
  backend\evaluation\audio_annotations.synthetic.dev.json

.\.venv\Scripts\python.exe -m scripts.audio_annotation_dataset validate `
  local_evaluation\audio_annotations.official.private.json --allow-pending

.\.venv\Scripts\python.exe -m scripts.audio_annotation_dataset score
```

评测默认使用 `models/bge-small-zh-v1.5`，禁止降级为 hash 向量。公开报告写入
`docs/competition/wuhu_audio_semantic_evaluation.json`，不含逐字稿、音频路径或字段正文。

指标含义：

- CER：脱敏逐字稿与 ASR 的字符编辑距离。
- 角色序列准确率：接线员/群众轮次序列的一致程度，不能替代 DER。
- DER：基于人工起止时间的漏检、误检和说话人混淆，仅在时间戳完整时计算。
- 语义字段 F1：地点、事件和诉求原子要点与工单字段之间的语义精确率和召回率。
- 支持精确率：系统生成的字段片段中，有多少能被人工参考要点支持。

当前36条合成开发集结果见 `wuhu_audio_semantic_evaluation.json`。它用于定位字符阈值低估的问题，
不能替代真实热线冻结评测。
