# 芜湖政务 Agent 前端（React + Next.js）

基于 Next.js 15（App Router）+ React 19 + TypeScript 的聊天界面，与后端（Python FastAPI）**前后端分离**。

三种角色的运行方式、权限边界和比赛链路见项目根目录 `README.md` 与
`docs/competition/intake-contract.md`；`design_files/` 仅保留为历史原型，不代表当前界面。

## 功能

- 登录与角色分流：营业员 → 诉求受理工作台；部门管理员/系统管理员 → 治理后台
- 营业员工作台：录音转写、角色分离、诉求整理、工单生成和人工校对
- 管理后台（`AdminDashboard`）：
  - 总览：Loop 渐进退出进度（人在环中/环上/环外）与阶段说明
  - 部门管理 · 文档入库：新建部门、上传文档、3.2 Pipeline 八阶段可视化
  - 审核中心：审核单逐题判定、部门正确率与退出进度
  - Loop 流程 · Skill 进化：五阶段循环、Skills/Hooks/Rules、badcase→rubric 规则
    - 手动 Loop 自动跟踪异步作业并展示结构化结果，不再只返回 `job_id`
    - 可执行基线 Skill + 自动挖掘 Skill 的工作流、灰度、版本和指标卡片
  - 部门子 Agent：各 Agent 栈（Orchestrator/Intent/Retrieval/Answer/Verifier）可视化
- `/api/*` 经 Next.js rewrites 代理到 Python 后端（无需额外 nginx）

## 目录结构

```
web/
├── src/
│   ├── app/                # App Router（layout / page / globals.css）
│   ├── components/         # Login / Chat / AdminDashboard + admin/ 各面板
│   │   └── admin/          # Overview / Dept / Review / Loop / Insights / Agent
│   └── lib/                # api.ts 类型与调用封装（含鉴权 token）
├── scripts/                # 三角色浏览器烟测与 Loop 页面烟测
├── next.config.mjs         # rewrites 代理 + standalone 输出
├── Dockerfile
└── package.json
```

## 运行

### Docker（推荐，见根 docker-compose）

访问 http://localhost:8080 （Next.js 反代 `/api` 到 `backend:8000`）。

### 本地开发

```bash
cd web
npm install
BACKEND_URL=http://localhost:8000 npm run dev   # http://localhost:3000
```

### 生产构建

```bash
npm run build && npm start
```

## 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `BACKEND_URL` | Python 后端地址（rewrites 目标） | `http://localhost:8000` |
