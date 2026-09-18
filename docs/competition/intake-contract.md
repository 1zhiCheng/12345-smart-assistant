# 受理阶段 → 处置阶段标准工单契约

受理阶段把录音或文本诉求转换为经过人工确认的标准工单；处置阶段只依赖本契约开展分类、转派、政策检索和回复生成。两段功能由项目统一维护，契约用于隔离模块而非划分人员归属。

## 官方示例字段映射

| 官方 Excel 字段 | StandardWorkOrder 字段 | 负责方 |
|---|---|---|
| 信件编号 | `case_id` | 受理阶段 |
| 受理时间 | `source.received_at` | 受理阶段 |
| 来源 | `source.source_channel` | 受理阶段 |
| 主题 | `title` | 受理阶段 |
| 内容 | `content`、`elements` | 受理阶段 |
| 区域 | `region` | 受理阶段 |
| 办理单位 | `routing` | 处置阶段 |
| 答复内容 | `reply` | 处置阶段 |

若群众未说明事件发生时间，受理阶段将 `source.received_at`（来电受理时间，Asia/Shanghai）填入 `elements.time`，并把 `elements.time_basis` 标记为 `received_at`；群众明确说明时间时标记为 `stated`。

## 人工确认边界

- 受理阶段输出的工单默认状态为 `draft`。
- 受理人员核对原始诉求、脱敏文本和关键要素后调用 `/api/v1/intake/confirm`。
- 只有 `confirmed` 工单才应进入处置阶段。
- `classification`、`routing`、`reply` 由处置阶段生成并经人工审核。

## 第一迭代 API

- `POST /api/v1/intake/analyze`：分析文本或已转写录音，生成工单草稿。
- `POST /api/v1/intake/clarify`：提交追问补充信息，在保留原工单编号和原始诉求的前提下重新生成草稿。
- `POST /api/v1/intake/transcribe`：上传脱敏录音并返回转写文本；使用 multipart 字段 `file`，失败时前端降级为人工补录。
- `POST /api/v1/intake/confirm`：保存人工确认后的标准工单。
- `GET /api/v1/intake/workorders`：读取已确认工单，供联调使用。

## 第三迭代交接状态机

`draft → confirmed/pending → processing → completed`

- 受理人员调用 `/confirm` 后，工单立即进入 `handoff_status=pending`，并记录 `handed_off_at`。
- 处置人员调用 `POST /api/v1/intake/handoffs/{case_id}/claim` 领取，状态变为 `processing`。
- 处置人员调用 `POST /api/v1/intake/handoffs/{case_id}/recommend` 获取分类、部门 Top-K、官方政策引用和待审核回复草稿；该操作不会自动办结。
- 处置人员调用 `POST /api/v1/intake/handoffs/{case_id}/result` 回写 `classification`、`routing` 和可选 `reply`，状态变为 `completed`。
- `GET /api/v1/intake/handoffs?status=pending|processing|completed|all` 用于读取交接队列；系统管理员可见全局，部门管理员只可见已转派到本部门的工单。
- 非领取人不能提交处理结果，系统管理员可在联调或异常恢复时接管。
