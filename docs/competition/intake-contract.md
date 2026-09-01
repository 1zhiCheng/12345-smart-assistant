# 成员 A → 成员 B 标准工单契约

成员 A 负责把录音或文本诉求转换为经过人工确认的标准工单；成员 B 只依赖本契约开展分类、转派、政策检索和回复生成，因此可以使用模拟工单并行开发。

## 官方示例字段映射

| 官方 Excel 字段 | StandardWorkOrder 字段 | 负责方 |
|---|---|---|
| 信件编号 | `case_id` | A |
| 受理时间 | `source.received_at` | A |
| 来源 | `source.source_channel` | A |
| 主题 | `title` | A |
| 内容 | `content`、`elements` | A |
| 区域 | `region` | A |
| 办理单位 | `routing` | B |
| 答复内容 | `reply` | B |

若群众未说明事件发生时间，成员 A 将 `source.received_at`（来电受理时间，Asia/Shanghai）填入 `elements.time`，并把 `elements.time_basis` 标记为 `received_at`；群众明确说明时间时标记为 `stated`。

## 人工确认边界

- A 输出的工单默认状态为 `draft`。
- 受理人员核对原始诉求、脱敏文本和关键要素后调用 `/api/v1/intake/confirm`。
- 只有 `confirmed` 工单才应进入 B 子系统。
- `classification`、`routing`、`reply` 是为 B 预留的字段，A 不做推断。

## 第一迭代 API

- `POST /api/v1/intake/analyze`：分析文本或已转写录音，生成工单草稿。
- `POST /api/v1/intake/clarify`：提交追问补充信息，在保留原工单编号和原始诉求的前提下重新生成草稿。
- `POST /api/v1/intake/transcribe`：上传脱敏录音并返回转写文本；使用 multipart 字段 `file`，失败时前端降级为人工补录。
- `POST /api/v1/intake/confirm`：保存人工确认后的标准工单。
- `GET /api/v1/intake/workorders`：读取已确认工单，供联调使用。

## 第三迭代交接状态机

`draft → confirmed/pending → processing → completed`

- A 调用 `/confirm` 后，工单立即进入 `handoff_status=pending`，并记录 `handed_off_at`。
- B 管理员调用 `POST /api/v1/intake/handoffs/{case_id}/claim` 领取，状态变为 `processing`。
- B 调用 `POST /api/v1/intake/handoffs/{case_id}/result` 回写 `classification`、`routing` 和可选 `reply`，状态变为 `completed`。
- `GET /api/v1/intake/handoffs?status=pending|processing|completed|all` 用于读取交接队列；系统管理员可见全局，部门管理员只可见已转派到本部门的工单。
- 非领取人不能提交处理结果，系统管理员可在联调或异常恢复时接管。
