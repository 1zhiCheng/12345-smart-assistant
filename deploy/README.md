# 部署（Docker / Kubernetes / Helm）

## 目录结构

```
deploy/
├── README.md
├── k8s/                     # 原生 K8s 清单
│   ├── namespace.yaml
│   ├── secrets.yaml.example
│   ├── mongodb.yaml
│   ├── redis.yaml
│   ├── backend.yaml         # Orchestrator + API（全局服务，HPA）
│   ├── loop-engine.yaml     # Loop Engine（独立后台 Worker）
│   ├── dept-agent.yaml      # 部门 Agent（模板化示例）
│   ├── ingress.yaml
│   ├── monitoring.yaml      # Prometheus / Grafana
│   ├── prometheus-adapter.yaml # 部门 Agent 自定义 HPA 指标
│   └── uploads-pvc.yaml     # backend/worker 共享上传文件
└── helm/
    └── wuhu-12345/              # Helm Chart（模板化部署新部门 Agent）
        ├── Chart.yaml
        ├── values.yaml
        └── templates/
```

## 1. Docker（本地，推荐）

见项目根目录 `docker-compose.yml` 与根 `README.md`。

## 2. Kubernetes 原生部署

```bash
kubectl apply -f deploy/k8s/namespace.yaml
# 先创建 secret（复制样例并填真实密钥）
cp deploy/k8s/secrets.yaml.example deploy/k8s/secrets.yaml
kubectl apply -f deploy/k8s/secrets.yaml
kubectl apply -f deploy/k8s/mongodb.yaml
kubectl apply -f deploy/k8s/redis.yaml
kubectl apply -f deploy/k8s/uploads-pvc.yaml
kubectl apply -f deploy/k8s/backend.yaml
kubectl apply -f deploy/k8s/loop-engine.yaml
kubectl apply -f deploy/k8s/dept-agent.yaml
kubectl apply -f deploy/k8s/monitoring.yaml
kubectl apply -f deploy/k8s/prometheus-adapter.yaml
kubectl apply -f deploy/k8s/ingress.yaml
```

## 3. Helm 部署（推荐，部门 Agent 模板化）

```bash
helm install wuhu-12345 deploy/helm/wuhu-12345 -n wuhu-12345 --create-namespace \
  -f values-private.yaml
```

`values-private.yaml` 不得提交到仓库，至少覆盖 `secrets.authSecret`、`secrets.internalApiToken`、
MongoDB/Redis 口令与连接串，并通过 `secrets.bootstrapAdminUsername` 和
`secrets.bootstrapAdminPassword` 创建首个系统管理员。生产默认使用本地 embedding，关闭诉求 LLM 外发、
pi Runtime 和固定口令演示账号；需要外部模型时再单独配置密钥并履行数据授权。

### 新增部门 Agent

```bash
# 修改 values-private.yaml 中的 departments 列表后统一升级
helm upgrade wuhu-12345 deploy/helm/wuhu-12345 -n wuhu-12345 \
  -f values-private.yaml
```

当前 Chart 已在 `values.yaml` 的 `departments` 列表中配置 12 类承办部门。新增部门时应修改该列表，
而不是传入不存在的单部门 `department.*` 参数。

## 4. 高并发设计（对应技术方案 7.3）

| 设计点 | K8s 实现 |
|---|---|
| 部门级隔离 | 每部门独立 Deployment + HPA，Pod 反亲和 |
| 弹性伸缩 | HPA 经 Prometheus Adapter 按部门 Agent 在途请求数扩缩，热门部门 maxReplicas=20 |
| 限流降级 | Ingress/Gateway 层限流；LLM 失败降级 FAQ/原文检索 |
| 可观测性 | Prometheus（`/metrics`）+ Grafana |

`uploads-pvc.yaml` 需要集群支持 `ReadWriteMany`。部署后确认自定义指标与 HPA：

```bash
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 | grep wuhu12345_dept_agent_inflight
kubectl get hpa -n wuhu-12345
```

1→20 Pod 的负载验证命令和通过阈值见 `../loadtest/README.md`。

## 5. 镜像构建

```bash
# 后端
docker build -t wuhu-12345-agent:v1.0 -f ../backend/Dockerfile ../backend
# 前端
docker build -t wuhu-12345-web:v1.0 ../web
```

## 6. pi Agent Runtime + Next.js 前端

Python 负责控制平面，pi 负责统一概率性 Agent 执行。服务拓扑为：

| 服务 | 清单/构建 |
|---|---|
| Python 后端 | `backend.yaml` + `../backend/Dockerfile` |
| pi Agent Runtime（Node/TS） | `pi-agent.yaml` + `../services/pi-agent/Dockerfile` |
| Next.js 前端 | `../web/Dockerfile` |

本地全栈一键启动见根 `docker-compose.yml`（已包含 mongodb/redis/backend/pi-agent/web）。
生产模板默认关闭 pi Runtime 和诉求外发；明确授权并配置后可启用，服务不可用时 Python 自动降级到本地 Agent。所有执行接口要求内部 Token。
