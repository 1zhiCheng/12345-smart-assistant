# 第 1 阶段验收：12345 场景清理与生产模板

验收日期：2026-09-15

## 完成范围

- 运行角色统一为营业员、部门管理员、系统管理员；生产默认不创建固定口令演示账号。
- 支持通过一次性 `BOOTSTRAP_ADMIN_*` Secret 创建首个系统管理员，密码至少 12 位。
- 清除活跃代码、前端、pi Runtime、负载测试和部署说明中的高校问答场景与旧品牌。
- Helm Chart 迁移为 `deploy/helm/wuhu-12345`，配置 12 类芜湖承办部门。
- Docker、K8s、Helm、指标、数据库、Redis Stream 与前端包名统一为芜湖 12345 命名。
- 生产模板默认使用本地 embedding，关闭诉求 LLM 外发、pi Runtime、外部重排和演示运行态。
- Docker 默认 profile 仅启动 MongoDB、Redis、backend、worker、web；pi Runtime 需显式使用 `--profile pi`。
- 修复原生 K8s 部门 Agent 反亲和 YAML 缩进和监控组件命名空间 DNS。
- pi Runtime 的意图、回答、校验、工具和 Loop 提示词迁移到工单受理、分类转派、政策依据与回复审核场景。

## 验收证据

- 后端单元测试：112 passed。
- Next.js 生产构建：通过。
- pi Runtime TypeScript 构建：通过。
- pi Runtime 生产依赖安全审计：0 vulnerabilities。
- `docker compose config --quiet`：通过。
- 原生 K8s 清单、Helm `values.yaml` 与 Compose YAML：全部可解析。
- `git diff --check`：通过。
- 默认 Compose 服务：redis、mongodb、backend、web、worker；显式 `pi` profile 才增加 pi-agent。

## 有意保留的兼容项

- `LEGACY_DEPARTMENT_IDS` 和 `legacy_wenshu_backup_*` 只用于识别、备份和清除旧高校数据，不参与当前业务。
- `design_files/` 作为历史原型保留，不作为当前运行与交付依据。
- 本阶段只完成生产模板和安全默认值；MongoDB/Redis/真实 embedding/Worker 的实际联调与压测属于第 6 阶段。

## 阶段结论

第 1 阶段通过，可以进入第 2 阶段“真实比赛录音端到端评测集”。
