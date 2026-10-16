# M3d：向量目录身份与完整性

日期：2026-10-16（Asia/Shanghai）  
起始代码：`41666a304d84b3033646941b7092b639b9502e67`；本记录关联同提交中的 M3d 源码。并行 M3a/b/c 改动不计入此功能的验收。  
状态：离线工程合同已验证；真实模型、远端 Qdrant 和生产效果未验收。

## 需求与范围

保留默认 `catalog-demo.jsonl.gz` 的 5,700 件商品、现有检索默认配置及模型选择。修复只按内部 ID/文本/模型复用导致的目录换绑风险，并区分“当前目录齐全”和“集合恰为当前目录”。不删除集合、旧点或数据库，不以重建数据卷替代迁移。

本功能只改 `app/infrastructure/vector/`、`scripts/sync_product_vectors.py` 与新增向量合同测试。没有改交易、composition、检索用例、评测 runner、前端或冻结目录数据。

## 实现

1. `embedding_identity.py`
   - 保留 endpoint/model/version/configured_dimension 模型身份键，密钥不进入键。
   - 新 `product-text-identity-v2` payload 绑定内部商品 ID、`source_platform/source_locale/external_product_id`、canonical ID、SKU ID/规格和检索文本。跨站点相同 ASIN 不等于同一外部身份。
   - `catalog_key` 是完整目录 ID→身份/文本映射的有序指纹，不冒充压缩目录文件的字节指纹。库存/价格变化不要求重新推理。
2. `qdrant_product_index.py`
   - 未绑定目录时查询返回空。bootstrap 在查旧向量前绑定完整目录快照；重复 ID 失败并清除旧绑定。
   - 只复用外部身份、文本、模型、schema、实际维度均验证一致的点；旧 v1 payload 不能凭相同 ID/文本自行补签外部身份。
   - 目录变化时，未变商品可零推理重绑。重绑是按点位、已验证 product_key、模型和 schema 的条件更新，阻断 retrieve 与 set_payload 之间同 ID 被其他进程换成另一商品的竞态。
   - 普通查询在 top_n 之前过滤 catalog_key 与 embedding_key；过滤查询再加调用者允许 ID。所有结果逐项验证完整预期 payload、确定性点位和维度，损坏数据不能映射到错误的当前商品。
   - 全量 scroll 验证点集、每点 payload、实际向量维度及有限值、集合 COSINE 配置。保留旧点但将 stale_points 计入不完整，不自动清理。
3. `index_bootstrap.py`
   - 已写批次仍可断点续建。成功写完不再直接宣称完整，必须通过完整枚举。
   - `catalog_complete` 只证明当前目录的有效点齐全；`complete` 还要求没有多余/无效点；`index_verified` 只表示枚举核验成功，不等于 complete。
   - 不支持验证接口的旧测试适配器标记 `readiness=unverified/index_verified=false`。Qdrant 路径没有静默跳过。
4. `scripts/sync_product_vectors.py`
   - 报告包含目录文件 blake2b、目录绑定键、模型键、集合、维度、距离、点数及完整性、向量实现/同步脚本/锁文件 sha256。
   - 索引身份指纹的 schema 明示为 `point-identity-payload-v1`，绑定点位、完整 payload 与集合维度/距离，不声称是向量数值字节指纹。

## 原始失败与复测

证据统一位于 [M3d 原始证据目录](../../../eval/verification/2026-10-16-m3d-vector-identity/)。所有运行通过 `scripts.ci.offline_env.isolated_env` 隔离环境，使用已安装 `.venv` 与临时本地 Qdrant，没有安装、真实模型调用、外部服务调用或推送。

- [基线](../../../eval/verification/2026-10-16-m3d-vector-identity/baseline/pytest.log)：原 persistence/retrieval/hybrid 46 passed。
- [首次兼容性复测](../../../eval/verification/2026-10-16-m3d-vector-identity/candidate-01/pytest.log)：46 passed。
- [新合同首次运行](../../../eval/verification/2026-10-16-m3d-vector-identity/candidate-02/pytest.log)：72 passed / 1 failed。唯一失败为冷/热重载后数值指纹不同。
- [原因诊断](../../../eval/verification/2026-10-16-m3d-vector-identity/candidate-02/normalization-diagnostic.log)：本地 COSINE 首次读取为归一化数值，重载后读取原始数值；同一点 payload 相同。改为明示的身份/payload/集合配置指纹，仍实际逐点验证向量存在、维度和有限值。
- [修复复测](../../../eval/verification/2026-10-16-m3d-vector-identity/candidate-03/pytest.log)：73 passed；[加固复测](../../../eval/verification/2026-10-16-m3d-vector-identity/candidate-04/pytest.log)：75 passed。
- [性能修复及重绑竞态合同](../../../eval/verification/2026-10-16-m3d-vector-identity/candidate-05/pytest.log)：76 passed。
- [最终源码复测](../../../eval/verification/2026-10-16-m3d-vector-identity/final/pytest.log)、[最终源码指纹](../../../eval/verification/2026-10-16-m3d-vector-identity/final/source.json)：76 passed，覆盖向量目录安全、旧持久化、普通检索、hybrid 和 local fallback。

新增 13 个合同包括：未绑定重启、同号外部身份改变且中断同步、跨站点同 ASIN、移出商品旧点保留且 incomplete、旧点不能挤占普通查询 top_n、旧格式拒绝自证、距离错误不删集合、空/重复目录、绑定后商品变更拒写、默认 5,700 冷/热完整性、损坏 payload 返回再校验、configured_dimension 模型隔离，以及并发换绑时条件重绑不误贴目录标签。

## 5,700 点冷/热实测及性能诊断

通过正式 `sync()` 路径，替换为确定性 CountingEmbedding 桩，在临时本地 Qdrant 中验证。使用三维向量，仅验证机制，不代表线上 1,024 维或本地 384 维模型质量/成本/延迟。

- [首次冷/热记录](../../../eval/verification/2026-10-16-m3d-vector-identity/mock-cold-warm/summary.json)：5,700 点精确齐全，热启动新增向量 0；普通 32 项查询约 3.823 秒。原因是本地 Qdrant 每个点遍历全目录身份键/点位列表，形成平方级额外开销。
- 修复为条件重绑保证 catalog_key 标签来源，查询只使用 catalog/model 边界，保留结果身份再校验及调用者 ID 过滤。
- [同条件复测](../../../eval/verification/2026-10-16-m3d-vector-identity/mock-cold-warm-fast/summary.json)：普通 32 项查询约 0.111 秒；[冷启动](../../../eval/verification/2026-10-16-m3d-vector-identity/mock-cold-warm-fast/cold.json) 推理桩向量 5,700，集合恰为 5,700；[热启动](../../../eval/verification/2026-10-16-m3d-vector-identity/mock-cold-warm-fast/warm.json) 复用 5,700、新增 0，目录与索引身份指纹一致。
- 两次均为单次本机诊断，不能当成统计性能收益、真实模型耗时或生产 SLO。

## 启用、兼容与恢复

- 安全绑定随代码启用，不修改默认目录、环境文件或检索参数。应用现有启动流程继续负责 bootstrap，不需要 composition 集成补丁。
- 已有旧 v1 payload 不足以证明外部身份，不会零成本自动补签。现有建库流程只在所配置 embedder 成功返回后更新对应点；失败保持未验证/部分状态，查询不返回旧身份。升级之前应按现有运维流程备份索引，并为模型调用单独准备授权/预算；本次仅使用本地桩。
- 若多余旧点导致 `catalog_complete=true/complete=false`，同步脚本返回失败；不得把它改称完整。先保存报告并检查目录来源。需要独立集合时显式配置新名称；本代码不自动创建替代数据卷、不删除旧集合或额外点。
- 同一共享集合上的不同目录进程可能相互改变可检索点的目录标签，导致某一进程检索降级或核验 incomplete，但不会错绑成当前商品。每个冻结目录/模型仍建议使用独立集合。
- 恢复应选择经核验的备份/隔离集合与相匹配的代码、模型及目录；不能退回旧代码后继续信任一个混杂集合。没有交易数据迁移或库存改写需要回滚。

## 边界与未完成项

[最终 Harness impact](../../../eval/verification/2026-10-16-m3d-vector-identity/final/harness-impact.log) 在集成负责人补齐 watched 范围后报告 `harness_changed=true`，要求 `contracts` 和 `smoke`；早期 candidate-04 的 false 保留为历史工具结果，不能继续作为最终结论。总体 M3 最终源冻结后的 core/bound-v2 离线合同、真实 smoke 阻塞证据及统一验收由集成负责人执行归档，M3d 不重复运行完整 contracts。真实模型 smoke/release 因本轮禁止付费/外部调用而未执行，离线 76 passed 不替代真实检索/远端 Qdrant/模型门禁。未执行正式留出、人工审核、12,299 默认目录迁移或部署。并行 M3 的日期索引与总记录由集成负责人统一更新。
