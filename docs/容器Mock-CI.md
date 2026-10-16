# 容器 Mock CI

此流程在 GitHub 标准 `ubuntu-24.04` hosted runner 运行，不部署服务，不上传镜像，不调用真实模型。公开仓库使用独立源码快照；原本地 Git 历史不改写。

## 运行与验收

- 向 `main` 或 `work/m1-baseline-ci` 推送，以及 PR、手工运行，会启动 `Container mock regression`。
- `Offline regression` 与 `Harness regression` 也监听上述两个分支。默认执行离线后端/ESCI、独立 Locust 契约、前端测试/构建、HTTP mock、Compose 合同以及 Harness 安全契约。
- 真实模型任务仍须手工运行并显式批准预算；本次推送不会触发真实模型调用。离线成功不代表真实模型门禁已通过。
- 在有 Docker daemon 和 Compose 2.24.4+ 的干净 Linux 工作树，可执行：

```bash
python3 -m scripts.ci.container_smoke --output eval/verification/local-container-01
```

每次使用新证据目录。脚本自动选择随机独立项目名，创建新命名卷；结束后只清理该项目的合成容器/卷，不执行全局 prune，不影响既有部署。未连接 daemon 时直接失败，不能把静态配置成功当作容器运行成功。

## 实际检查范围

1. 通过官方 Dockerfile 干净构建运行镜像，再基于该本地镜像构建探针层。镜像只保留在 runner，记录实际 image ID。
2. 复用基础 Compose 的 app 环境/资源限制和 `/health` 探针。用独立 mock 模型、mock embedding 和容器内 HTTP 探针完成冷启动。
3. 验证签名身份、未登录拒绝与跨买家隔离；运行真实 HTTP 搜索/SSE、游标重放、候选保存/比较/精确核验、订单批准与 prepare/resolve 幂等。
4. 执行真正的 `docker compose restart`，确认同一 container ID 的启动时间改变；再 `--force-recreate`，确认 container ID 改变而命名卷不变。每步重新验证持久业务结果，无新增 mock chat 请求。
5. 停止唯一写入者 app，检查其已退出且不是 OOM/强杀，再复用 `scripts.ops.state_snapshot` 生成完整应用快照。正常 SIGTERM 的容器退出码 143 与 0 均被接受。
6. 在原卷上执行一次快照后的合成订单取消，证明状态发生变化；停止 app，将快照恢复至全新卷，使用同一个 image ID 启动并重建派生索引。
7. 核对订单回到快照时的 CONFIRMED、库存、候选/比较、SSE与幂等；原卷仍保留 CANCELLED。恢复前后 Qdrant 均应为 5,700 点、1,024 维。

这验证同镜像/同代码的数据快照回退，不验证跨版本镜像、数据库 schema 或旧协议回滚。它不运行 Redis/worker/前端容器、不验收并发容量；相应离线与前端构建由既有工作流负责。生产 RPO/RTO、真实模型质量和公网部署仍未验证。

## 安全与证据

- 工作流仅授予 `contents: read`，Actions 固定完整提交 SHA，checkout 不持久化凭据。
- Docker 子进程使用新 HOME/DOCKER_CONFIG；不继承宿主 Docker context、GITHUB_TOKEN、代理或应用凭据，显式禁用 `.env`。源码工作树存在 `.env` 时拒绝运行。
- app、mock、probe 均不发布宿主端口；全部位于 `internal: true` 网络，不挂载 Docker socket或宿主目录。构建阶段仅下载公开基础镜像及锁定依赖，运行阶段不通外网。
- HMAC 和模型 API 字符串是公开合成夹具，不可用于真实部署。新建模型注册表主密钥只属于临时合成卷。
- Actions artifact 保存聚合 `report.json`、命令/容器日志、镜像/容器/卷身份与源码指纹；不导出快照、数据库、主密钥或 AOF。该 artifact 成功且报告 `status=passed` 才能说明本次容器链路通过。
- 公开源码快照排除整个 `eval/verification/` 历史诊断目录。历史文档中的相对证据链接可能只在原本地交付中存在，不能据此宣称公开仓库已携带或复现旧证据；新证据以对应提交的 Actions 运行与 artifacts 为准。
- Amazon ESCI 来源的 [LICENSE 与 NOTICE](../third_party/esci-data/) 随源码及运行镜像保留；不代表整个项目采用该许可证。

## 本轮本机验证边界

本机具备 Docker/Compose CLI，但当前访问 daemon 返回 permission denied。已完成官方 Compose 解析，以及探针的真实回环 HTTP、签名身份、快照/恢复与 5,700 点重建自测；本机自测只重启 Python 进程。实际容器 build/up/restart/recreate 必须等 GitHub hosted Actions 结果，当前不能标为已通过。

官方参考：[GitHub Actions 最小权限与固定 SHA](https://docs.github.com/en/actions/reference/security/secure-use)、[Compose 网络](https://docs.docker.com/compose/how-tos/networking/)。
