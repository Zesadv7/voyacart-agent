# M1：完整离线基线与 CI

## 需求与版本

按整体方案 v2 实施 M1：保留当前 main `264ef633e3cd0b431e06e941e89136379eb46022` 的基线，补完整离线回归、前端生产构建、确定性 HTTP 与重启、Compose 配置及版本化证据。执行日期按 Asia/Shanghai 为 2026-10-16；原执行器日志时钟比对话时钟慢 7 小时，原始内容保留，耗时采用单调时钟。

起点 main 与工作区均干净；本地分支 `work/m1-baseline-ci`。没有推送、合并、公开部署或调用付费模型。基线逐文件指纹、最终源码指纹和工具版本见 [验收目录](../../../eval/verification/2026-10-16-m1-baseline/README.md)。本地实现提交由该文件所属 Git 提交标识；证据清单另记录被验收的代码提交。

## 具体变更

- 新增 `.github/workflows/offline-ci.yml`：后端含 ESCI、独立 Locust、真实本机 HTTP/mock、前端 Vitest＋TypeScript/生产构建、两份 Compose 配置、最终必需检查汇总。按实际需要验证各层，不用文档路径过滤令必需检查消失。
- `scripts/ci/scope.py` 与测试覆盖 frontend、docker、configs、所有 Harness 子目录、ESCI v1/v2 与 catalog 数据。已有 `Offline safety contracts` 继续每个 PR 执行。
- 真实模型层仅手动工作流中明确批准冻结套件调用预算后执行；普通 PR 的影响范围和待批准状态写入 Actions summary。缺凭据时原有 runner 仍失败，不把离线合同当作真实模型通过。
- 新增 `scripts/ci/backend.py` 和 `offline_env.py`：全回归只允许干净、无 `.env` 的 checkout，使用临时运行目录，丢弃宿主凭据、外部服务与代理配置。除明确委托独立解释器的 3 个 Locust 场景，意外 skip 会使入口失败。
- `uv.lock` 新增独立 `loadtest` 依赖组（Locust 2.46.7），既有包版本未升级。主回归安装冻结 `esci` 和 `optimization` extras；Locust 用独立解释器，避免影响 asyncio。
- Redis 测试夹具新增显式回环 TCP 选择。受限执行器不支持 Unix socket 时仍启动官方 Redis 独立进程；仅操作该进程，不连接外部 Redis。默认原 Unix 模式保留，CI 入口选择 TCP。
- `tests/test_retrieval.py` 固定 v1 中原始十件商品。原二元向量桩对多个灯具给出相同向量，不能用并列排序声称 P1008 唯一第一；保持原链路、硬约束、精排、卡片断言，用明确小夹具消除无效并列假设。没有修改生产排序算法或目录。
- `scripts/ci/mock_smoke.py` 用独立 API/假模型进程、临时 SQLite/Qdrant 和真实 HTTP，检查冷启动、检索、SSE 游标、显式确认、重复批准、服务重启及模型不重跑；保存索引数量、维度和文件指纹。只绑定 127.0.0.1。
- 删除已误跟踪的 `frontend/tsconfig.tsbuildinfo` 构建缓存；已有 ignore 生效。README 修正默认混合召回关闭、可缺精排、5700/12299目录差异和未支付意向单性质。旧改造总结只加当前进度入口，未重写历史。

## 失败、诊断与复测

全部原始日志保留在 [M1 基线证据](../../../eval/verification/2026-10-16-m1-baseline/)。

1. 默认 uv/npm 缓存目录只读，首次安装失败；改为 `/tmp` 缓存后锁文件安装通过。
2. 扩展到 `scripts/esci` 后首次收集出现 5 个缺 pyarrow 错误；安装冻结 esci/optimization extras 后继续完整运行。
3. 第一次扩展回归：2091 通过、11 失败、51 errors、3 skipped。51 errors 与 Redis 重启失败源于此执行器禁止 AF_UNIX；夹具显式改回环 TCP 后 Redis 专项 19/19 通过。其余失败是初版隔离入口误禁用 `.env` 解析与本机 OTLP 合同、以及二元桩并列排序。修复入口/夹具后完整复跑通过。中间直接运行聚焦测试又受宿主代理影响，10 项失败同样保留，最终采用清理代理的正式入口。
4. HTTP 首次因 `scripts/loadtest.py` 与同名目录冲突导致 mock 导入失败；改为 uvicorn `--app-dir` 后完整流程通过。后加索引指纹时 QdrantClient 不支持上下文管理器，报告阶段失败；改显式 close，再次完整复跑通过，不覆盖旧失败。

## 最终验证

| 检查 | 当前结果 | 证据 |
|---|---|---|
| 完整后端＋ESCI | 2157 passed，3 skipped（仅独立 Locust） | [日志](../../../eval/verification/2026-10-16-m1-baseline/backend-final.log)、[JUnit](../../../eval/verification/2026-10-16-m1-baseline/backend-final.xml) |
| 独立 Locust | 14 passed，含上述 3 个场景，不能简单与主回归相加 | [日志](../../../eval/verification/2026-10-16-m1-baseline/locust-tests.log) |
| 安全合同 | 869 passed，0 skipped；源码指纹一致 | [报告](../../../eval/verification/2026-10-16-m1-contracts/report.html) |
| 前端 Node 22 | 250 passed，TypeScript＋生产构建通过 | [测试](../../../eval/verification/2026-10-16-m1-baseline/frontend-node22-tests.log)、[构建](../../../eval/verification/2026-10-16-m1-baseline/frontend-node22-build.log) |
| HTTP / SSE / 重启 | 5 项业务检查通过；5700 件本地索引；111 条连续事件；库存50→49，重复批准/重启后仍49和1单 | [报告](../../../eval/verification/2026-10-16-m1-mock-verified/report.json) |
| Compose | 单机与 scale＋mock 两份配置通过；未运行容器部署 | [单机](../../../eval/verification/2026-10-16-m1-baseline/compose-single.log)、[scale](../../../eval/verification/2026-10-16-m1-baseline/compose-scale.log) |
| 完整真实 Harness | 明确 BLOCKED，make 退出2；契约仍869通过，真实调用0 | [缺配置报告](../../../eval/verification/2026-10-16-m1-model-blocked/report.html)、[原始退出](../../../eval/verification/2026-10-16-m1-baseline/harness-model-blocked.log) |

## 启用与回退

- 离线：`uv sync --frozen --extra esci --extra optimization`，安装官方 Redis，`make test-backend test-loadtest test-frontend`。自带 Redis 二进制可用 `VOYACART_REDIS_SERVER_BIN` 指定；CI 默认回环 TCP。不要删除用户 `.env`，需要时另建干净测试 checkout。
- `make smoke-mock` 自动使用临时运行目录，不需要现有数据库或索引；`make check-compose` 仅解析配置。真实模型没有在本轮启用。
- 回退本 M1 提交即可恢复原 CI 和测试入口。未改生产数据库格式、默认商品目录、模型策略或运行开关，没有数据迁移或删卷。

## 仍未完成

- 未授权推送，因此本地实现没有新 GitHub Actions 链接，远端分支保护是否要求新 `Offline regression complete` 尚未配置。
- 真实模型/embedding/rerank 成本和外部服务结果没有验证。完整真实 Harness 不能标成通过。
- 此执行器无 Docker daemon，Compose 实际容器冷启动、部署、TLS、备份恢复归 M6；M1 已验证进程模式的干净冷启动和持久恢复。
- 单机 mock 的启动耗时只用于复现，不是容量或生产性能承诺。
