# M2a：prepare 请求级幂等

日期：2026-10-16（Asia/Shanghai）。范围：订单创建／取消的后端准备请求，未部署、未推送；不包含前端运行时身份与持久请求键实现。

## 需求与实现

- HTTP 创建确认与取消 DTO、应用用例及 ConfirmationService 接受可选 `request_id`（最长 128 字符）。仅带键请求获得本轮保证；缺键仍保持每次新操作的旧合同。模型工具签名不变，模型重复调用没有请求键，也不在本轮保证内。
- 作用域为 `(buyer_id, session_id, action, request_id)`。同键规范等价请求恢复原确认；不同内容返回 HTTP 409 `PREPARE_REQUEST_CONFLICT`。参数校验 `INVALID_ARGUMENT` 归 HTTP 422，资源缺失仍 404。
- 规范请求版本为 1。创建绑定聚合并排序后的 product_id／sku_id／quantity 及 Address 全字段 trim 后的值；country 不额外改变大小写，继续使用原 Address／配送合同。相同 SKU 对应不同商品明确拒绝，不偷偷合并。取消绑定 trim 后 order_id／reason。
- SHA-256 输入使用稳定 JSON，明确含 action 和 request_version，排除报价、标题、库存及确认有效期。新表只存摘要，不保存第二份明文地址或报价。
- `trade_prepare_requests` 新增复合买家／会话／动作／请求主键、唯一 confirmation_id、request_version、摘要及创建时间。原 `trade_confirmations.request_hash` 的报价绑定算法与 `snapshot_hash` 不改，也不回填或猜测旧请求键。
- 首次查询映射在目录、库存、报价或取消订单读取之前；`BEGIN IMMEDIATE` 内再次查询映射，先重放再验证报价／库存／状态。确认与请求映射在同一事务提交，插入或 commit 失败不会占住请求键。
- 首次 lookup 与 preflight 之间可能并发提交，预检异常时重查已提交映射；已提交但返回丢失也可恢复。事务重查得到其他请求的原确认时，不重复投递 confirmation.required。
- 原确认即使已过期、批准、拒绝或订单随后取消，仍按原状态、原快照及原有效期重放。不会延长 TTL、刷新原报价或再生成订单；重新核验／再次购买必须换新键。resolve 的原子扣补库存、审批和操作幂等语义保持不变。

## 关键源码与集成

- `app/domain/order/ports/trade_store.py`：规范化、版本化摘要、查找端口。
- `app/infrastructure/persistence/sql/trade_tables.py`、`trade_store.py`：增量表、启动版本校验、事务内查重与原子写入。
- `app/application/usecases/confirmation_service.py`、`order_usecases.py`：报价前重放、失败重查、可选键贯穿。
- `app/presentation/confirmations.py`、`dto.py`：创建／取消请求 DTO 和 HTTP 错误合同。
- `server.py` 的取消路由由 M2 集成提交负责人接入 `request_id=body.request_id`；本批未编辑该文件，真实路由测试覆盖这条连接。
- `tests/test_prepare_request_idempotency.py` 新增请求键合同；`tests/test_confirmation_routes.py` 补创建与取消 HTTP 合同。

## 验证与证据

最终聚焦回归：**137 passed，0 failed，0 skipped**，63.64 秒；代码与测试提交：`6c24f915a778f675338ed4b779096c252bdd78f9`。

全部使用 `scripts.ci.offline_env.isolated_env`、临时 SQLite、合成商品／买家／地址；没有真实模型、embedding、rerank、真实账号或外部服务调用。

- 最终聚焦回归及源码指纹见 [验证说明](../../../eval/verification/2026-10-16-m2a-prepare/README.md)、[最终日志](../../../eval/verification/2026-10-16-m2a-prepare/verified.log)、[JUnit](../../../eval/verification/2026-10-16-m2a-prepare/verified.xml)、[执行前后指纹](../../../eval/verification/2026-10-16-m2a-prepare/manifest-verified.json)。
- 覆盖同键并发、异载荷竞争、独立连接、spawn 双进程首次建库和准备、不同 buyer／session／action 隔离、顺序／重复 SKU／空白规范等价、冲突、目录不可用前重放、价格变化、新键新报价、库存耗尽、取消后重放、过期／批准／拒绝后重启恢复。
- 覆盖响应丢失后的创建／取消恢复、服务层预检失败后的并发映射重查、确认插入／映射插入／commit 注入失败全回滚、失败准备不占键、未知映射版本拒绝启动、旧库升级逐字段保留确认及哈希、旧审批继续可用、禁止用新键反向绑定旧 operation。
- 继续复用现有最后库存竞争、多 SKU 后段不足全回滚、resolve 独立连接一次提交、取消一次补库和任务中断释放锁测试。
- 初次回归 84 passed、4 failed：并行 M2b 尚未写入 `public_output.py`，真实 server 导入失败；保留 [原始失败](../../../eval/verification/2026-10-16-m2a-prepare/initial.log)。模块到位后 [扩展复测](../../../eval/verification/2026-10-16-m2a-prepare/expanded-initial.log) 119 passed；随后 HTTP 扩展 [132 项复测](../../../eval/verification/2026-10-16-m2a-prepare/final.log) 通过。HTTP 参数错误码与并发事件去重补齐后另行跑最终验证，不覆盖上述历史证据。
- [Harness impact](../../../eval/verification/2026-10-16-m2a-prepare/harness-impact-verified.log) 对本批源码／测试判定 `harness_changed: false`。本批无 Prompt、模型预算、上下文或输出 Harness 改动，不执行真实模型冒烟；M2b 的 Harness 验证单独归档。

## 启用、回退与边界

1. 初始化库存时，在写锁内 `metadata.create_all` 增加映射表，并检查映射版本；不 ALTER 旧表、不清空数据、不重置持久库存、不猜测历史请求键。
2. 前端／新客户端必须在发送前持久化本次尝试键，网络或解析错误继续原键；新会话／身份隔离，显式重新核验或再次购买才轮换。该前端能力由 M2 另一批实现，本记录不代替前端验收。
3. 回退前停止写请求并备份既有数据库；旧代码可忽略新增表，但不再提供请求级幂等。保留映射表与原交易数据，不通过删库或重置数据卷回退。禁止在旧、新版本同时接收交易写请求时声称端到端幂等。
4. 仅验证 SQLite 单个数据库文件的事务与同主机独立进程；不是多主机共享盘、PostgreSQL、真实部署或生产容量验收。本批未增加映射清理策略，删除映射会破坏原请求的重放保证。
5. 本地代码与测试提交为 `6c24f915a778f675338ed4b779096c252bdd78f9`；执行基线 HEAD 及逐文件源码版本见 manifest。M1 基线与结果见 [M1 验收](../../../eval/verification/2026-10-16-m1-baseline/README.md)。当日索引及总索引由 M2 集成提交维护。
