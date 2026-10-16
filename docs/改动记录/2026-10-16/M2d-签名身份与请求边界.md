# M2d：签名身份与请求边界

## 需求与范围

执行日期：2026-10-16 Asia/Shanghai。承接 [M1 完整离线基线](M1-完整离线基线与CI.md)，实现运行时身份验证、双签名买家/管理员权限矩阵和直连 API 的有限请求边界。实现、离线验证和公网效果分别记录；没有读取真实 `.env`、产生真实密钥、调用真实模型或外部连接探针、推送或公开部署。

本功能的代码版本由该记录所属 Git 提交标识。服务器注册由 M2 集成提交负责；本轮已在共同工作区通过服务器实际注册测试。前端运行时身份入口和 prepare 重试分别由其它 M2 功能记录说明。

## 具体变更

- `app/infrastructure/identity.py` 新增不可变 `VerifiedIdentity` 和 `verify_identity()`，仅从验签后的固定 claims 返回买家及有效期。原 `verify()` 仍返回买家字符串，原 HS256、typ、issuer、audience、iat/exp 和 1 至 86400 秒 TTL 语义保持不变。
- `app/presentation/identity.py` 抽取签名身份依赖；无效、缺失、错签名、过期令牌返回 401、Bearer challenge 和 no-store。query token 不作为凭证；现有 WebSocket 子协议和 metrics 独立运维白名单保持。
- `app/presentation/identity_routes.py` 新增 GET `/commerce/identity/config`，只暴露 mode 和固定演示 `default_buyer_id=pao-coder`；GET `/commerce/identity` 只验证现有 Bearer，返回 `{mode: hmac, buyer_id, expires_at}`，有效期为 ISO UTC `Z` 字符串。两者 no-store，身份响应带 Vary: Authorization。没有公共签发接口，服务端已有签发 CLI 不变。
- `app/infrastructure/settings.py` 增加显式公网配置和 HTTP 限额，均有兼容默认值；没有改变演示买家或商品目录。
- `app/infrastructure/security/request_limits.py` 在业务路由之前完整读取有限正文，检查 Content-Length 和实际分片总字节，超限 413、无效/重复长度声明 400、正文读取超时 408。超限前不会调用业务路由；chunked、缺失长度或谎报长度均不能绕过实际计数。已通过真实断连继续传递和 SSE 重放兼容测试。
- 全进程滚动 60 秒请求计数只覆盖 `/commerce`、`/internal`，超限返回 429、Retry-After 和 no-store；计数容器有界，不使用可伪造的 X-Forwarded-For。健康检查和 OPTIONS 不计数。注册顺序将 CORS 放在外侧，拒绝响应仍可被允许的前端 Origin 读取。

## 配置、启用与回滚

| 变量 | 默认值 | 合同 |
|---|---:|---|
| PUBLIC_DEPLOYMENT | 0 | 显式选择公网启动检查；不自动部署 |
| HTTP_MAX_REQUEST_BYTES | 1048576 | 1 至 16777216 字节，包含直连 FastAPI |
| HTTP_BODY_TIMEOUT_SECONDS | 30 | 有限正值，不超过 300 秒 |
| HTTP_REQUESTS_PER_MINUTE | 0 | 每 API 进程 0 至 100000；0 只供私有环境关闭限速 |

公网模式必须满足：HMAC 身份、SESSION_OWNER_BINDING、OUTPUT_GUARD_ENABLED；至少 32 字节且不明显重复的独立服务端密钥；非空、明确 HTTPS CORS Origin（拒绝 wildcard、userinfo、路径、query、fragment、无效主机/端口）；TOKEN_BUDGET_TOTAL 和 HTTP_REQUESTS_PER_MINUTE 为正整数。明显弱密钥检查是长度及字符多样性启发式，不能证明随机熵；实际密钥仍必须由运维通过密码学随机方式产生并安全保存。

私有 demo 默认仍不要求令牌，也默认关闭速率限额；HTTP 正文限额/超时始终生效。私有 hmac 仍强制 owner binding。公网 opt-in 并不证明网络、TLS、集群或容量安全。

启用时由运维通过已有配置渠道提供上述参数，使用已有本机 CLI 签发短期令牌；本轮没有生成或存储实际部署凭据。回滚需要同步回退服务器路由/中间件注册与本功能代码；公网环境不应仅把 PUBLIC_DEPLOYMENT 改成 0 来绕过检查。没有数据库格式变更、迁移或删卷。

## 双买家与管理员验证

- 54 个 HTTP 入口分别验证缺令牌、过期、错签名、仅 query token、有效签名但篡改 buyer，共 270 个攻击请求断言；拒绝时业务存储哨兵及会话归属写入均未调用。
- 矩阵包括 intents/task、AG-UI run/session/SSE/停止/历史 tool-products、收藏、选购表单、个人 Skill/偏好、确认/订单、automation/run、context/operation、模型目录/选择/偏好/默认/连接 validate/save。
- 两个合成签名身份使用真实临时 SQLite 会话、AG-UI 日志、交易、automation、Skill、收藏及文件偏好存储。交叉路径不能读取、停止或修改另一买家的资源；自己的会话/SSE/历史商品正常可读；拒绝交易没有进入订单创建/取消实现且库存不变。
- 模型目录/选择/个人偏好与默认使用真实 ModelRegistry，连接验证只用固定离线 probe。管理员 alice 可以管理自己的连接，普通买家 bob 不能 validate/save，也不能借 profile/session ID 访问 alice 的私有配置；普通买家自己的内置模型偏好/默认仍可用。
- metrics 沿用既有独立运维读取白名单。交易归属错误保留兼容的 409 `OWNER_MISMATCH`，订单读取保留 404；不把错误状态码差异改写成成功。

## 失败、诊断与复测

原始证据位于 [M2d 验证目录](../../../eval/verification/2026-10-16-m2d-identity/)，每次执行独立日志，不覆盖失败。

1. [首次聚焦](../../../eval/verification/2026-10-16-m2d-identity/initial.log)：90 passed、3 failed。WS `test.ready` 的旧测试预期 payload 原样返回，与 M2b 未知事件仅保留元数据的新安全合同冲突；改为同时检查类型保留和 payload 为空。另两项是新增矩阵错误预期：session 目录实际字段是 id、交易归属错误既有合同是 409 `OWNER_MISMATCH`，分别按已核实合同修正，未修改权限实现来迎合断言。
2. [首次复测](../../../eval/verification/2026-10-16-m2d-identity/retest-1.log)：93 passed。
3. [扩展回归](../../../eval/verification/2026-10-16-m2d-identity/final.log)：239 passed，含现有模型工作区、历史商品、automation、交易确认路由测试；[JUnit](../../../eval/verification/2026-10-16-m2d-identity/final.xml)。新增源码已包括 54 入口矩阵、额外 CORS 主机校验和设置读取测试。
4. 增补完整服务器身份注册与大正文 413/CORS 断言后，聚焦 98 passed，结果见 [最终日志](../../../eval/verification/2026-10-16-m2d-identity/verification-final.log) 和 [JUnit](../../../eval/verification/2026-10-16-m2d-identity/verification-final.xml)。
5. 进一步使 PUBLIC_DEPLOYMENT 拼写错误失败关闭（只接受 0/1/false/true），最终复测 102 passed，见 [最终配置复核](../../../eval/verification/2026-10-16-m2d-identity/config-final.log)；源码指纹见 [清单](../../../eval/verification/2026-10-16-m2d-identity/config-final-sha256.json)。

最终聚焦命令为 `.venv/bin/python -m pytest -q tests/test_identity.py tests/test_identity_matrix.py tests/test_request_limits.py`，另输出 JUnit 到证据目录。

以上 pytest 子进程均使用 `scripts.ci.offline_env.isolated_env`，临时运行目录与合成凭证；未继承宿主代理、真实服务凭据或追踪配置。固定模型 probe 没有网络请求。仅有既存 Starlette/httpx 弃用警告。

## 未完成及边界

- HTTP 限额是单 API 进程总额，不是按买家公平限流、集群共享限额或 DDoS 防护；不管理 WebSocket 准入、后台运行队列/执行容量、worker 并发。M5/M6 继续验证运行准入、反向代理、部署及容量。
- 不新增账号中心、刷新 token、撤销服务或公共签发。已有 token 仍遵守原有限期与密钥轮换语义。
- 本批不改变 Harness 模型/Prompt/工具合同；M2b 的输出合同及 Harness 验证在其专项记录，最终整体回归由 M2 集成记录负责。本功能没有真实模型收益/质量验收、公网 TLS 或多进程负载效果证据。

## 补充：显式空模型配置下的测试独立性

最终 M2 合同套件增加上述身份与请求边界测试后，常规隔离环境通过，但执行显式空 `LLM_API_KEY` 的真实模型入口时，安全合同阶段提前失败。原始整体结果为 1074 passed、6 failed、26 errors，保留在 [原始合同失败](../../../eval/verification/2026-10-16-m2-model-blocked/contracts.log)。其中本功能的 26 个 fixture errors 来自 `server` 模块导入时加载模型必需配置，5 个配置单测 failures 来自默认 `load_settings()` 的模型必需配置检查；另外一个输出边界用例由对应功能独立修复。

本次只调整测试，不改生产代码或 Harness runner：

- `tests/test_identity.py` 的 `secured` 夹具在导入 server 前提供合成占位 key 和不可用的回环 URL `http://127.0.0.1:1/v1`，随后继续用固定容器/编排器替身；monkeypatch 在用例结束恢复原环境。
- `tests/test_identity_matrix.py` 继续复用该夹具，无须修改。
- `tests/test_request_limits.py` 的配置单测显式清空模型 key，使用已有 `load_settings(require_llm_key=False)`，使被测配置守卫不依赖与该测试无关的模型凭据。生产启动的模型必需配置检查保持不变。

通过 `isolated_env` 创建临时环境，再显式将 `LLM_API_KEY` 与 `EMBEDDING_API_KEY` 置为空，三个文件独立重跑：102 passed、0 skipped、1 个既存 Starlette 弃用警告，用时 26.80 秒，退出码 0。见 [日志](../../../eval/verification/2026-10-16-m2d-empty-model-env/focused.log)、[JUnit](../../../eval/verification/2026-10-16-m2d-empty-model-env/focused.xml)、[命令与环境说明](../../../eval/verification/2026-10-16-m2d-empty-model-env/invocation.json)、[源码指纹](../../../eval/verification/2026-10-16-m2d-empty-model-env/source-sha256.json)。旧失败证据不删除、不覆盖。

这只证明离线安全合同不依赖真实模型 key，不能当作真实模型可用或完整真实 Harness 已通过；整体入口复核由 M2 集成记录继续报告。
