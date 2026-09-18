# 阶段 2：真实比赛录音端到端评测

## 验收结论

阶段 2 已完成。18 条比赛真实热线录音全部完成本地端到端处理，覆盖 12 个信件类别，
无运行失败。评测报告只保存脱敏样例 ID、类别、聚合指标和必要诊断码，不保存原始信件号、
文件路径、逐字转写、群众诉求正文、承办单位原文或答复原文。

## 数据与边界

- 数据源：比赛提供的 12 类 Excel 示例工单及配套 18 条真实录音。
- 标签对齐：用类别和受理时间把原始 Excel/录音映射到
  `backend/evaluation/intake_cases.official.deidentified.json` 的匿名样例 ID。
- 运行模式：`faster-whisper turbo` 本地 CPU/int8、CAM++ 本地说话人分离、规则工单整理，
  全程不调用外部 LLM。
- Excel“内容”是整理后的工单，不是人工逐字稿，因此不宣称或计算 CER/WER。
- Excel没有说话人时间戳，因此不宣称或计算 DER；角色指标仅表示系统是否接受双角色结果。
- 本报告是未调参真实基线。不得根据这 18 条失败明细反向修改词表后再把同一批数据当作泛化成绩。

## 结果

| 指标 | 结果 |
| --- | ---: |
| 样例完成率 | 18/18（100%） |
| 真实语音时长 | 112.42 分钟 |
| 评测运行时间 | 24.79 分钟 |
| ASR 成功率 | 100% |
| 官方工单内容二元组召回 | 42.20% |
| 声学说话人分离接受率 | 94.44% |
| 双角色检出率 | 94.44% |
| 工单信息完整率 | 97.22% |
| 严格字段准确率 | 6.94% |
| 字段阈值通过率 | 5.55% |
| 工单内容支持精确率 | 97.83% |
| 区域准确率 | 33.33% |
| 部门 Top-1 | 33.33% |
| 部门 Top-K | 50.00% |

结果说明：规则链路已经能稳定完成真实音频解码、转写和多数双角色检测，但整理后的事件/诉求
与官方工单表达差异较大，且转写噪声会继续放大路由误差。阶段 3 应在不读取本批失败文本、
不调整冻结留出集的前提下，用独立开发文档建立语义路由，再只对冻结集执行一次复评。

## 稳定性修复

本机 `ctranslate2` 能发现 CUDA 设备，但 CUDA/cuDNN 推理运行时不完整。原实现每条录音都会
重新尝试同一个不稳定 GPU 路径，导致第二条录音长时间无输出。`AsrService` 现在会在首次真实
CUDA 推理失败后记住 CPU 回退状态，后续录音直接复用 CPU/int8 模型。定向回归覆盖了这一状态转换。

## 可复现命令

```powershell
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m scripts.evaluate_official_audio --validate-only
.\.venv\Scripts\python.exe -m scripts.evaluate_official_audio `
  --intake-mode rules --device cpu --compute-type int8 --resume `
  --output docs\competition\wuhu_official_audio_e2e_evaluation.json
```

## 验收证据

- 公开报告：`wuhu_official_audio_e2e_evaluation.json`
- SHA-256：`108A42100BA5E571A6D6DFCA67DD4D975FE8E96530CECC905D4DD0C730DFE72F`
- 公开载荷隐私与完整性检查：通过。
- 后端完整回归：115 passed，1 个第三方弃用警告。
- ASR/真实录音定向回归：9 passed。
