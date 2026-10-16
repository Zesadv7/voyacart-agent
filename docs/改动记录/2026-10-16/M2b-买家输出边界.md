# M2b：买家输出先审核后写入

## 需求与版本

2026-10-16（Asia/Shanghai）实现 M2b 买家公开输出边界。前置基线为 [M1 完整离线基线与 CI](M1-完整离线基线与CI.md)；本文所属 Git 提交标识本批代码版本。当前为本地实现和确定性离线验证，没有推送、公开部署、调用真实付费模型或读取真实凭据。

目标是阻断“最终回复已经审核，但之前的流、工具参数、旧日志重放、旧 WebSocket 已经泄露”的旁路，同时保持商品历史、交易确认和协议 ID 可用。协同批次的 orchestrator、runtime、日志/trace 和身份变更由 M2 汇总记录分别说明。

## 具体变更

- 新增 `app/infrastructure/security/public_output.py` 纯投影函数。区分自由正文 `safe_text`、技术标签 `secret_text`、递归公开副本 `public_value`、已知工具参数/结果、确认/订单、商品、活动、状态、AG-UI 与旧 TradeEvent。工具名不经过正文内部工具名替换；业务 ID、商品/SKU、确认 hash 保留正常合同。
- 扩展 `output_guard.py`：合成 API key 形态、Bearer/Basic、JWT、常见凭据赋值，以及嵌套结构化凭据键。公开 URL 拒绝 userinfo、私网/本机/内部服务、非 HTTP(S)、敏感 query 和控制字符；允许合法商品图片目录与公开图片地址。所有规则均为本地确定性检查，不读取凭据库或请求 URL。
- `ag_ui_adapter.py` 不再发布原始 `TEXT_BLOCK_DELTA`。正文仅在 `finish` 获得完整最终回答并审核后，通过 `MESSAGES_SNAPSHOT` 发布；保留安全开始/结束/进度事件。错误、取消、未完成内容不补发原始正文。
- 原生 Thinking 只显示生命周期与固定状态，不发布其正文。原生 `TOOL_CALL_DELTA` 缓冲完整输入，END 后解析完整 JSON 并按工具白名单一次发布；未完成参数在关闭时丢弃。原生确认卡保留已知业务字段，但不会修改 SDK 的 `ToolCallBlock.input`。
- 工具结果只允许产品 hits、订单/确认合同等可展示数据；Skill 原文、专家原文、未知工具和 DATA 媒体块不透传。产品卡保留前端必需的 score、SKU、价格、来源、评分、图片及描述字段；商品历史仍按精确 `toolCallId`/`product_search_tool` 关联。
- `ag_ui_journal.py` 在 INSERT 原始事件之前投影，派生 state/messages 只消费该安全副本。新写逐块正文为空；旧 event 回放也清空原始正文 delta，完整 JSON 参数按工具名重投影。run、session、session 列表、工具商品历史均过滤旧数据；保留原序号、游标、顺序和运行归属，不重写旧事件。
- 旧多 TEXT_BLOCK 历史消息按连续角色联合检查，避免各消息片段分开看均未命中、连接后成为凭据。最终正常消息与正常商品历史保留。
- 公开输入/历史副本与执行输入分开：实际 runtime 继续把最初解析的 `intent` 交给 orchestrator；专测确认原始 user 内容与 SDK 工具执行输入不被投影修改。
- `connection.py` 旧 WebSocket 只发送 `public_trade_event` 的公开视图，直接丢弃 token.delta；未知事件只保留安全元数据和空 payload，异常使用固定公开摘要。
- 白名单按稳定排序输出，避免不同进程 `PYTHONHASHSEED` 导致工具结果内 JSON 字符串键序变化，破坏重启后的逐事件相等性。
- 保留 A2UI v0.9 的 createSurface/updateComponents/updateDataModel、恢复目的地、上下文整理原因与全部现有用量字段；这些不应被安全投影误删或赋予错误含义。

## 验证与证据

首批提交聚焦回归：**285 passed，0 skipped**（后续独立审查与最终修复复测见文末），没有真实模型调用。范围包括所有 `test_ag_ui*.py`、表单测试、既有 Harness guards 和新增 `test_public_output_boundary.py`；环境使用 `scripts.ci.offline_env.isolated_env`，临时 SQLite、合成秘密与真实回环 Redis。

- [最终原始日志](../../../eval/verification/2026-10-16-m2b-complete/tests.log)
- [最终 JUnit](../../../eval/verification/2026-10-16-m2b-complete/tests.xml)
- [测试命令、逐文件 SHA-256 与源码稳定性](../../../eval/verification/2026-10-16-m2b-complete/manifest.json)

新增覆盖：真实 AgentScope Text/Thinking/ToolCall/ToolResult DATA/RequireUserConfirm 类型；逐字符、跨块、失败/取消；参数完整才发布；确认内容和实际执行输入不变；SQLite 新写三表无合成秘密；所有旧历史断点游标安全且单调；历史原事件未改写；商品卡关联与规格/价格；旧 WebSocket；多层投影幂等；不同解释器 hash seed；实际 A2UI v0.9；完整用量字段保真。

### 保留的失败与复核

1. [初次回归](../../../eval/verification/2026-10-16-m2b-initial/tests.log)：118 passed、10 failed。旧测试依赖原始正文 delta 才断连、保留未审核 partial/Thinking、透传非 JSON 工具结果。按新安全合同调整为仍检验安全进度、断连/取消、完整最终快照及不同工具调用的精确关联，没有删除其业务断言。
2. [第二次聚焦](../../../eval/verification/2026-10-16-m2b-focused/tests.log)：195 passed、3 failed、1 skipped。新增 DATA 夹具错误地同时传 data 和 url，被真实 SDK 拒绝；改为两个真实事件。缺本机 Redis 路径导致的 skip 在后续显式提供 `/tmp/voyacart-redis/src/redis-server` 后消除。
3. [扩大回归](../../../eval/verification/2026-10-16-m2b-candidate/tests.log)：270 passed。随后补齐交易结果字段和跨进程稳定性检查，不能将此阶段当最终源码证据。
4. [幂等复核失败](../../../eval/verification/2026-10-16-m2b-final/tests.log)：282 passed、1 failed。非法 query_order_tool 结果第一次保留固定 notice，第二次投影删除 notice；统一规范为 unavailable 后修复。
5. [修复后复核](../../../eval/verification/2026-10-16-m2b-verified/tests.log)：283 passed。自审再补 A2UI v0.9 与 context reason 保真后，重跑形成上述最终 285 passed 的独立目录。
6. 集成方 [首次 HTTP 重启检查](../../../eval/verification/2026-10-16-m2-http-initial/report.json) 暴露逐事件一致性失败；本批确认并修复跨进程 JSON 字符串键序的不稳定性。最终签名 HTTP/重启/交易链路和完整 Harness 契约由 M2 汇总另列，不以本批聚焦测试替代。

Harness impact 确认本批需要 contracts 和 smoke。M1 已保留确定性基线；本批最终候选、比较与源码核验归 M2 集成验收。真实模型 smoke/release 未授权，**不能声称完整真实 Harness 或生产收益通过**。

## 启用、兼容与回退

不新增开关、数据库 schema 或离线迁移。上线该代码后所有买家出口强制使用公开投影；关闭旧 `output_guard_enabled` 不能关闭此底线（集成方负责 orchestrator 兼容处理）。正常完整回答改为最终快照一次出现，Thinking 正文不再显示；不能把该行为误称为逐字正文流。

旧数据库仅在读取时过滤，旧原始事件仍保留作为证据，不代表已离线清除历史秘密。若需要清理既有数据库或备份，应另行制定授权迁移和保留策略。本批没有自动删除、重建库或修改业务库存/订单/确认。

回退本批代码会重新暴露原来的流式/重放输出边界风险，不应在面向买家的联网服务上直接回退；如必须回退，先停止相关外部输出入口再处理。没有数据结构回滚或删卷步骤。

## 范围与局限

这是已知工具/协议白名单加确定性凭据/URL检查，不是任意秘密、任意编码或自然语言私密信息的通用 DLP，也不是 DNS 解析或外站信誉审计。未知公开数据默认不展示；已授权本买家确认所需的收件信息、商品规格、数量、金额和 hash 继续保留。真实供应商模型、真实网关、生产部署和成本/效果指标仍未在本批验证。


## 同日独立审查补充：数字主机、活动正文与带标签凭据

首批提交 `32f513cbad53a12d6353854105ca4bec89f12a97` 后，独立审查给出可复现旁路，保持原失败证据并作同日小提交修复；此前 285 项通过不能覆盖这些新增发现。

### 修复内容

1. URL 主机先做 IDNA/全角归一化。标准 IP 使用 `ipaddress` 检查；浏览器可能解释为 IPv4 的十六进制、八进制、整数、缩写、混合和空 hex 尾段别名直接拒绝，不把它们当普通公开 DNS 主机。保留标准公网 IPv4、普通公开域名及合法商品图片相对路径。
2. `public_activity` 按 kind 选择字段。合法工具活动只有工具身份和安全摘要，没有 content；Thinking content 固定为空；未知 kind 不展示正文/工具摘要，使用固定步骤标签。覆盖新写入和旧 journal 的 events/run/session 读取，不改写历史原事件。
3. 正文凭据标签与结构化键共享 `is_secret_key`，覆盖 private_key、access_key、credentials、Set-Cookie、Cookie、Authorization 等不带供应商前缀的 opaque 值。带引号字段仅删除字段值；无引号凭据保守移除余下整行，避免 Cookie 多值或空白分隔的尾部秘密漏出。普通 product_id/quantity/currency 标签保持可读。
4. 补回权威订单 snapshot 的 `amount_scope=merchandise_only`、`order_kind=purchase_intent`，同时用于 query_order_tool 和 confirmation.result；使用真实交易存储准备/批准后的快照做完整相等性断言。
5. 商品工具结果保留真实 `recall_strategy`、`rerank_applied`、检索版本、向量可用标记和白名单检索诊断。reranker_error 仅返回固定 `RERANK_UNAVAILABLE`，不回传异常原文；缺失的精排证据不由商品出现推断或补造。

### 失败证据与最终验收

- [旁路复现](../../../eval/verification/2026-10-16-m2b-review-failed/tests.log)：13 failed、12 passed，确认混合数字主机和工具/未知活动 content 可绕过。
- [opaque 标签复现](../../../eval/verification/2026-10-16-m2b-credentials-failed/tests.log)：14 failed、1 passed、25 deselected；保留完整失败，不把正则只识别供应商 key 当作足够保护。
- [首次修复专项](../../../eval/verification/2026-10-16-m2b-review-candidate/tests.log)：145 passed。
- [扩大复核](../../../eval/verification/2026-10-16-m2b-review-complete/tests.log)：341 passed。随后真实 Node WHATWG 交叉核查发现空 hex 尾段 `0x7f.0.0.0x` 仍映射为 `127.0.0.0`，再次修复，不以此阶段作为最终证明。
- [WHATWG 对照](../../../eval/verification/2026-10-16-m2b-numeric-review/whatwg.log)、[空 hex 修复前](../../../eval/verification/2026-10-16-m2b-numeric-review/before.log)、[20 类别名修复后](../../../eval/verification/2026-10-16-m2b-numeric-review/after.log)：仅执行本地 Node URL 解析，没有访问这些 URL。
- **最终当前源码：343 passed，0 skipped**。[原始日志](../../../eval/verification/2026-10-16-m2b-review-final/tests.log)、[JUnit](../../../eval/verification/2026-10-16-m2b-review-final/tests.xml)、[逐文件指纹与 source_stable=true](../../../eval/verification/2026-10-16-m2b-review-final/manifest.json)。范围增加 `test_public_output_review.py` 和实际应用 `test_verify_ag_ui.py`，其余保留全部 AG-UI、表单、既有 guard 与新输出边界回归。

完整后端和固定 Harness 测试清单由集成批次在此冻结源码上重新执行；不能把此 343 项聚焦通过声称为全库或真实模型 smoke/release 通过。本次仍无真实模型调用、无外部网络请求、无推送或部署。
