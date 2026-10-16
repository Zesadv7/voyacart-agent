# Voyacart

Voyacart 是一个跨境电商选购 Agent：用中文描述用途、预算和收货地，检索多语种商品、保存候选、比较方案，再通过确认单创建未支付的订单意向。

项目围绕一条完整的选购链路实现：React 工作台、Agent 编排、带硬约束的检索、可重放的长任务、显式交易确认，以及可测试的状态恢复。默认面向单机、私有环境中的演示与工程验证。

> 商品价格、库存、配送范围及运税是演示数据或模拟估算。系统不连接真实支付、商家下单或物流。`purchase_intent` 是未支付订单意向；`merchandise_only` 表示确认金额仅含商品，不是含税含运费的最终结算价。

[运行与恢复手册](docs/交付与恢复手册.md) · [安全与身份边界](docs/安全与身份边界.md) · [目录数据卡](docs/数据卡/ESCI目录数据卡.md) · [MIT License](LICENSE)

## 能做什么

- **选购与比较**：查询商品、查看原文字段与来源说明、按商品/SKU 保存候选，维护比较集合。候选独立于当前搜索结果持久化，重新核验当前目录时无需调用模型。
- **Agent 协作**：MainAgent 负责会话和任务编排，可直接使用业务工具，也可派发 SearchAgent、TradeAgent。上下文整理、偏好、技能与工具护栏由独立模块管理。
- **明确交易确认**：模型生成确认请求；买家经 HTTP 批准后才写入订单意向。确认单绑定快照、有效期和操作号，重复批准幂等，库存不足时整单回滚。
- **长任务与重连**：运行不依赖某一条 HTTP 连接，事件写入持久日志，浏览器按 `Last-Event-ID` 续传。状态、工具进展和结果通过 AG-UI/SSE 传送；公开正文审核完成后一次发布，不逐 token 暴露原始模型输出。
- **可选多进程执行**：Redis Streams 支持入队、重投、死信和有界背压；Redis Pub/Sub 通知新事件，推送连接仍从持久日志按游标读取。
- **可复现验证**：后端回归、前端测试、HTTP 假模型链路、容器重启/恢复、故障演练和真实模型评测分别提供入口，分别报告结果。

## 架构与运行边界

```text
浏览器（React / AG-UI）
        │ HTTP / SSE
        ▼
FastAPI：身份、会话、候选、确认单、运行事件
        │
        ├── 默认：API 进程内 Agent 运行时
        └── 可选：Redis Streams → Worker
                          │
                 MainAgent / SearchAgent / TradeAgent
                          │
                 应用用例与领域端口
                  ├── 检索 → Qdrant / 嵌入 / 可选精排
                  ├── 交易 → SQLite 事务与库存条件扣减
                  └── 会话、运行日志、候选及其他状态库
```

代码分为 `domain`、`application`、`infrastructure`、`presentation`，依赖由 [`app/composition.py`](app/composition.py) 组装。默认使用 SQLite 和嵌入式 Qdrant，不要求 Redis；多 API/worker 形态使用 Qdrant 服务端，并共享单机状态卷。这不是跨主机分布式数据库方案。

### 可靠性机制

| 领域 | 当前实现与限制 |
|---|---|
| 运行与事件 | 持久化事件序号、运行租约、心跳和 owner fencing。可重放已写入的事件；进程故障可能将运行收口为 interrupted，不承诺恢复模型内部生成位置。 |
| 入队与准入 | 每进程活跃上限、运行期限、队列待处理上限与排队资格期。已启用的队列不可用时返回 503，不悄悄改成本地执行。期限控制不等于能强杀不合作协程。 |
| 交易与候选 | 确认快照、过期检查、操作幂等、条件扣库存；候选用 revision 防止旧页面覆盖新状态。保存候选不锁价，也不预留库存。 |
| 模型与工具 | 有界重试、可选回退模型、工具熔断；共享网关配额依赖 Redis 时，Redis 故障会拒绝模型调用。共享熔断需协调升级，不能假设旧新协议混跑安全。 |
| 检索降级 | 可选精排失败保留向量排序；远端向量不可用时，可配置本地多语种向量兜底，最终保留关键词检索。降级可用不代表相关性不变。 |
| 健康与恢复 | `/health` 检查部分已配置依赖，不是模型、worker、全部状态库和索引的端到端健康证明。恢复必须核对完整状态组。 |

SQLite 写入竞争、完整容量矩阵、生产 RPO/RTO 和真实业务收益仍需在目标环境独立验收，不以单测或短程演练推导容量承诺。

## 商品目录与检索

默认运行目录为 `data/catalog-demo.jsonl.gz`，包含 5,700 件商品：3,700 件合成商品，以及 2,000 件来自 Amazon ESCI 的商品文本记录。美国、西班牙、日本站点的来源与原文保留；模型推断、合成字段和未知信息在展示合同中区分。

`data/catalog-esci-v2.jsonl.gz` 的 12,299 件商品用于独立检索评测，不能直接替换默认运行目录。内部 P/SKU 编号不等于外部商品身份；目录清单与内容指纹绑定运行状态。升级前须预检，不能靠删除数据库或数据卷绕过身份冲突。

检索支持向量召回、可选精排、精确商品查询，以及代码层面的预算、目的地、库存、材质约束。BM25＋向量 RRF 混合召回和本地多语种嵌入兜底均需显式开启，默认关闭。启用本地向量需安装 `retrieval` 可选依赖并准备模型资源。

ESCI 的人工相关性标签用于检索评测；商品类目、材质及中文查询译文中的派生内容不等于人工审核。当前类目和译文的有效人工审核数为 0，准确率未知。真实 Agent 质量、降级检索质量和真实模型成本必须单独评测。

来源、字段处理和人工抽检规则见 [数据卡](docs/数据卡/ESCI目录数据卡.md)、[人工抽检规则](docs/数据卡/ESCI人工抽检规则.md)及 [ESCI 许可与归属](third_party/esci-data/README.md)。

## 快速开始

需要 Python 3.11–3.13、[uv](https://docs.astral.sh/uv/) 和 Node.js 22。全量后端回归另需 `redis-server`；容器回归需 Docker Engine 和支持当前 Compose 文件语法的 Docker Compose v2。

### 1. 不使用真实模型的本机验证

在没有 `.env` 的干净 checkout 中执行。脚本创建独立临时状态，启动回环假模型和 API，完成检查后退出，不使用真实模型凭据。

```bash
uv sync --frozen
npm --prefix frontend ci
uv run --no-sync python -m scripts.ci.mock_smoke --signed \
  --output eval/verification/local-signed-http
npm --prefix frontend test
npm --prefix frontend run build
```

每次使用新的报告目录，避免覆盖前次结果。该入口检查签名身份、检索、SSE 重放、候选、确认幂等和进程重启；它不是持续运行的网页服务，也不替代真实浏览器操作验证。持续假模型网页演示见[运行手册](docs/交付与恢复手册.md#持续本机网页演示)。

### 2. 接入自己的模型服务

需要兼容 OpenAI 协议、支持流式工具调用的模型服务。实际模型和嵌入调用可能产生费用。

```bash
cp .env.example .env
# 编辑 .env：设置 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL，核对嵌入配置。
# 将 DATA_DIR 改为独立运行目录，例如 ./data/runtime，不与冻结目录混放。
uv run python -m uvicorn app.presentation.server:app \
  --host 127.0.0.1 --port 8000 --workers 1
```

另开终端：

```bash
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

打开 <http://127.0.0.1:5173>。健康检查在 <http://127.0.0.1:8000/health>，API 文档在 <http://127.0.0.1:8000/docs>。再次运行离线全量回归请使用另一份干净 checkout，不要删除真实配置来迁就测试。

### 3. Docker Compose

使用已核对的 `.env` 启动单机全栈，默认前端端口为 5173，API 为 8000：

```bash
docker compose --env-file .env -f docker/docker-compose.yaml up -d --build
```

多进程形态通过 Nginx 提供 8080 端口：

```bash
docker compose --env-file .env -f docker/docker-compose.scale.yaml \
  up -d --build --scale api=2 --scale worker=2
```

默认只向宿主回环地址发布应用入口，Redis/Qdrant 不发布宿主端口。停止服务使用对应配置的 `down`，不要加 `-v` 删除业务卷。Compose 配置通过不等于实际部署完成；目标机器的依赖就绪、资源限制、TLS 和恢复结果仍需核验。

独立容器假模型回归由专用驱动运行，不读取 `.env`，不发布宿主端口，只创建并清理自己的随机项目和合成数据卷：

```bash
python3 -m scripts.ci.container_smoke --output eval/verification/local-container
```

该项验证真实镜像构建、签名 HTTP、容器重启、保卷重建与同镜像快照恢复；不覆盖前端浏览器、多 worker 容量或跨版本代码/schema 回滚。

## 配置与安全

完整变量见 [`.env.example`](.env.example)，解析与默认值见 [`settings.py`](app/infrastructure/settings.py)。

| 配置 | 用途 |
|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 主模型网关；`LLM_FALLBACK_MODEL` 留空可关闭回退 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | 嵌入服务与维度；需和索引身份一致 |
| `QDRANT_URL` | 空值使用本地嵌入式 Qdrant；多进程使用服务端 |
| `RERANKER_BASE_URL` / `RERANKER_MODEL` | 可选精排；`RERANKER_MODE=disabled` 显式关闭 |
| `REDIS_URL` / `QUEUE_ENABLED` / `AGUI_QUEUE_ENABLED` | 共享依赖及两种入口的队列开关；AG-UI 入队默认关闭 |
| `RUN_MAX_ACTIVE` / `RUN_TIMEOUT_SECONDS` / `QUEUE_MAX_AGE_SECONDS` | 默认每进程 8 个活跃运行、运行期限 300 秒、排队资格 900 秒；不是容量调优结论 |
| `HYBRID_RECALL_ENABLED` / `LOCAL_EMBEDDING_FALLBACK` | 混合召回和本地向量兜底，默认关闭 |
| `DATA_DIR` / `MODEL_MASTER_KEY` | 运行状态目录与模型配置加密主密钥；恢复必须保留原密钥 |
| `IDENTITY_MODE` / `PUBLIC_DEPLOYMENT` | 默认私有 demo；签名身份使用 `hmac`，公网部署须显式启用启动护栏 |

所有 `VITE_*` 变量都会进入浏览器构建产物，不要放密钥；构建会拒绝 `VITE_API_TOKEN`。签名模式通过运行时登录使用短期 token。公开部署须配置 HTTPS Origin、随机服务端密钥和正值请求预算/速率；开启 `PUBLIC_DEPLOYMENT=1` 不会自动创建 TLS，也不代表完成安全审计或具备完整 DLP。

## 测试分层与 CI

| 层次 | 入口 | 证明范围 |
|---|---|---|
| 后端离线回归 | `make test-backend` | `tests` 与 ESCI 数据管线；使用隔离状态，检查非预期跳过 |
| Locust 合同 | `make test-loadtest` | 在独立解释器中验证压测客户端行为，不是容量压测 |
| 前端 | `make test-frontend` | Vitest、TypeScript 与生产构建，不是浏览器端到端录像 |
| HTTP 集成 | 上述带 `--signed` 的本机驱动 | 真实本机 HTTP 与确定性假模型，含进程重启 |
| 容器集成 | 上述容器驱动 | 镜像、持久卷、容器生命周期及同镜像数据回退 |
| 隔离恢复演练 | `python -m scripts.ops.mock_recovery --output <新报告目录>` | 合成状态组与 Redis 恢复；需官方 `redis-server`，不操作真实库 |
| Harness 离线合同 | `make eval-harness-contracts` | 确定性安全合同，不调用真实模型 |
| Harness 真实模型 | `make eval-harness` / `make eval-harness-release` | 另需凭据和预算；发布档还检查质量、消耗与延迟门禁 |

完整离线回归前安装冻结依赖和官方 `redis-server`：

```bash
uv sync --frozen --extra esci --extra optimization
make test-backend
make test-loadtest
make test-frontend
make eval-harness-contracts
uv run --frozen python -m scripts.ci.secret_scan
make check-compose
```

工作流定义在 [Offline regression](.github/workflows/offline-ci.yml)、[Harness regression](.github/workflows/harness-eval.yml) 和 [Container mock regression](.github/workflows/container-mock.yml)。离线层按路径选择；安全合同对每个 PR 执行。真实模型层仅在手动触发并明确批准预算后运行，还需配置相应 secrets，不能从离线绿灯推断真实模型通过。

工作流文件描述覆盖范围，不是运行成功证明。请在仓库的 **Actions** 页面查看目标提交对应的任务、日志和报告附件。历史本机诊断归档未包含在本公开快照中；旧设计与改动记录中的历史证据链接可能不可用，历史数字不能替代当前提交的测试结果。

## 数据恢复与升级

- 先关闭入口并停止所有 API、worker、自动化和维护写入者。单库 SQLite backup 不能单独保证多个状态库与 Redis 的一致快照。
- 备份完整状态组、Redis 队列/AOF、冻结目录与索引配置，并安全保留原模型主密钥；只复制 `voyacart.db` 不够。
- 恢复工具只写全新目录，保留原状态；从冻结目录与匹配的嵌入配置重建派生索引，再核对订单、库存、幂等、候选、SSE 和队列终态。
- 回退快照会丢失快照后的写入。同镜像数据恢复不证明跨版本 schema 回滚可行，也不等于生产 RPO=0。

工具入口为 `python -m scripts.ops.state_snapshot --help`；完整前提、升级流程与限制见[交付与恢复手册](docs/交付与恢复手册.md)。

## 目录结构

```text
app/domain/          实体、业务边界与存储/检索/队列端口
app/application/     Agent、工具、用例、记忆与 Harness
app/infrastructure/  持久化、检索、队列、模型、上下文治理和安全实现
app/presentation/    FastAPI 路由、AG-UI、身份及各工作台接口
frontend/            React + TypeScript 工作台与组件测试
data/                版本化商品目录及 ESCI 来源/派生记录
scripts/             数据构建、评测、CI、运维与负载/故障演练
tests/              后端与基础设施回归测试
eval/               Harness 场景和检索评测集；运行报告在本机或 CI 附件
docker/             普通、多进程及隔离 CI 的 Compose 配置
third_party/        第三方许可、NOTICE 与来源说明
docs/               设计、数据卡、运维手册与改动记录
```

进一步阅读：[设计与取舍](docs/DESIGN.md)、[执行计划](docs/PLAN.md)、[改动记录](docs/改动记录/README.md)。其中历史计划和验收状态需结合当前代码及目标提交的 CI 结果阅读。

## 许可证

Voyacart 的自有代码采用 [MIT License](LICENSE)，Copyright (c) 2026 Zesadv7。

第三方数据、依赖及其附带材料仍适用各自的许可证和归属要求，不因本项目采用 MIT 而重新许可。Amazon ESCI 的固定版本 LICENSE、NOTICE 和加工说明保留在 [`third_party/esci-data/`](third_party/esci-data/README.md)；Python 与前端依赖分别由 `uv.lock` 和 `frontend/package-lock.json` 锁定，应同时遵守各依赖的许可。
