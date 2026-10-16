# CI：GitHub hosted 容器 Mock 与快照回退

日期：2026-10-16（Asia/Shanghai）。基线 `01db556f9e085ce3a4051fc21c48a98e4f9e4fd3`；在保留历史的独立工作副本实现，原项目目录不改动。此前 M1/M6 的代码与恢复合同继续复用，详见 [M1 CI](M1-完整离线基线与CI.md)、[M6 一致快照](M6-一致快照工具.md)。本记录描述 CI 准备与本机验证，不冒充 GitHub 已成功运行。

## 需求与实现

- 为新公开仓库准备标准 GitHub hosted Ubuntu 的容器构建、健康、签名 mock HTTP、数据持久化、容器重启和回退检查；无真实凭据、付费模型、云部署或镜像发布。
- 新增 `.github/workflows/container-mock.yml`：`contents: read`、完整 SHA 固定 Actions、checkout 不保留凭据、40 分钟上限、失败也保存合成报告。三个工作流均监听 `work/m1-baseline-ci` 与 `main`；既有真实模型任务仍只允许显式手工预算批准。
- 新增 `docker/docker-compose.ci.yaml`，复用基础 app 的健康/资源合同，覆盖为单进程 mock、嵌入 Qdrant、关闭 Redis/队列/追踪；全部服务只在 internal 网络、不发布端口。`Dockerfile.ci` 仅向本次运行镜像补充探针层。
- `scripts/ci/container_smoke.py` 控制干净 build/up、实际 restart/recreate、检查 container/image/volume ID、统一停写、新卷恢复与只清理独立项目。新 HOME/DOCKER_CONFIG、显式空 env-file，不继承真实服务/代理/token。
- `scripts/ci/container_probe.py` 复用 `scripts.ops.mock_recovery.seed_http`、现有 SSE 解析与 `state_snapshot`。每阶段核对签名、跨买家隔离、订单/库存、幂等、候选/比较、完整与游标 SSE，验证重放未增加 mock chat 调用。
- 快照后在原卷取消合成订单，再恢复至新卷；同 image ID 下订单回到 CONFIRMED，原卷仍为 CANCELLED。Qdrant 索引恢复后重建至 5,700 点/1,024 维。
- 补齐固定上游 ESCI LICENSE/NOTICE 与来源说明，COPY 至运行镜像，更新数据卡中“尚未保存许可”的历史状态；顶层 MIT 由同轮独立授权任务添加，本功能同时将其 COPY 到运行镜像。
- 新 [运行手册](../../容器Mock-CI.md)说明启用、证据、清理和边界，并从 [交付与恢复手册](../../交付与恢复手册.md)链接。

## 阶段验证与原始证据

1. 初次本机定向回归：75 通过、1 失败、8 跳过。失败是自测错误地要求 uvicorn 的 SIGTERM 退出为 0；该版本完成关闭后返回 -15。修正为主机 0/-15、容器 0/143，仍拒绝 OOM 与强杀137。8 个 Compose 合同因 CLI 未在 PATH 跳过，之后显式提供官方二进制重跑。[初始原始日志](../../../eval/verification/2026-10-16-container-ci-local/pytest.log)
2. 修正复测：84 项通过、零跳过。包含真实回环 HTTP、停止/重启进程、快照、新目录恢复与索引重建；此项不属于 Docker 容器实跑。[复测日志](../../../eval/verification/2026-10-16-container-ci-retest/pytest.log)
3. 最终定向回归在 Python 3.12.14 下追加官方 Compose 隔离解析及超时日志保留测试：86 项通过、零跳过；运行镜像仍固定 Python 3.11，镜像内检查由 hosted CI 执行。结果见 [最终日志](../../../eval/verification/2026-10-16-container-ci-final/pytest.log) 与 [JUnit](../../../eval/verification/2026-10-16-container-ci-final/pytest.xml)。测试源码在 `tests/test_container_ci.py`，同时运行既有 CI 环境/作用域、Compose 与状态快照测试。
4. 实际容器尝试在 `docker version` 的 daemon 连接阶段返回 permission denied 并退出 1；没有进入 build/up。[原始尝试报告](../../../eval/verification/2026-10-16-container-ci-daemon-check/attempt/report.json)记录 `status=failed`、空 checks，不能称为容器通过。使用现有 Docker CLI，未启动 daemon 或提升宿主权限。
5. 顶层 MIT 通知加入运行镜像后，重新运行同一86项定向套件：86 通过、零跳过，55.63 秒；结果见 [MIT镜像通知后最终日志](../../../eval/verification/2026-10-16-container-ci-license-final/pytest.log) 与 [JUnit](../../../eval/verification/2026-10-16-container-ci-license-final/pytest.xml)。
6. 官方 Compose 5.6.0 已完成配置解析；Python 编译、`git diff --check` 和已跟踪路径已知格式秘密扫描通过。这里只扫描已知格式，不将其升级为完整安全认证。

以上证据保留在本地交付的 `eval/verification/`。新公开源码快照有意排除整个历史诊断目录；这些相对证据链接在公开快照中不一定存在。公开仓库本次容器结论必须等待对应提交的 Actions artifact，不复用历史报告冒充。

## 启用与回滚

启用：推送至新公开仓库支持分支，或在有 Docker daemon/Compose 2.24.4+ 的 Linux 工作树运行 `python3 -m scripts.ci.container_smoke --output <新目录>`。不需要真实 secrets。全部镜像仅本地构建，全部测试数据为临时合成数据。

代码回退：撤回本功能提交即可移除新工作流/探针及分支监听；ESCI 来源许可与 NOTICE 应继续随派生数据保留，不应因停止 CI 而删除必要归属。

演练仅支持同镜像的数据快照回退，不证明跨版本 schema/旧协议回滚；不验证真实模型、Redis/worker 多副本、前端容器、容量或生产 RPO/RTO。快照之后的变更被保留在原卷、不会出现在恢复卷，不能声称 RPO=0。

关联版本：以包含本记录的 `ci: add hosted mock container lifecycle and snapshot rollback` 提交为准；GitHub 对应公开快照提交与运行编号另记于该次 Actions 结果。

## 首次 GitHub 运行与修复

- 首次公开提交 `bbd8caa` 的安全契约 job 实际执行 1,348 项通过、99 项因缺少 `redis-server` 跳过，严格门禁判失败；保留 run `38851030382`，不将跳过计为通过。
- 给安全契约 job 补与后端回归一致的官方 Ubuntu `redis-server` 安装步骤，保留原有完整性门禁；修复后结果以新提交的 Actions 为准。
- 首次容器 run `38851030312` 在最后原卷保留探针失败：只读挂载的 SQLite WAL 库需要 sidecar 写入；此前构建、启动健康、签名 HTTP、restart/recreate、停写快照、新卷恢复与同镜像回退已执行，但整条 workflow 判失败。
- 修复仅将已停写原卷的 DB 与现存 WAL/SHM 复制到临时可写目录查询；原卷继续只读，不忽略 WAL、不改业务恢复逻辑。新增回归覆盖已 checkpoint 与保留 committed WAL 两种状态，并核对源文件指纹/权限不变。
- 本地修复验证：CI 配置合同 15 项通过；两项只读/WAL 新回归与真实 loopback 全链探针共 3 项通过（55.40 秒），9 项与本轮探针修复无关的容器单测未在该定向命令选择。GitHub 容器复测尚待新提交运行。
