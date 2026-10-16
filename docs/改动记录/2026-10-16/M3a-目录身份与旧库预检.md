# M3a：目录身份、批准绑定与旧库只读预检

## 需求与边界

运行仍使用 5,700 件 demo；v2 的 12,299 件仍是独立评测目录。demo/v2 的 2,000 个共用编号全部换绑，不能直接采用远端 36aba28 的目录切换，也不能以新建/删除数据卷代替迁移。无外部模型调用、不推送、不迁移用户库。

## 实现

- `catalog_identity.py`：商品身份含平台、站点、外部 ID/canonical；SKU 身份再绑定规格。无外部事实只声明 internal 身份，不虚构来源。价格与库存不参与身份；标题、配送集合等决定可批准事实版本。
- `trade_catalog_skus` 保存版本、身份、可批准事实摘要、generation/active。`initialize_inventory` 接收完整当前目录，在更新报价前验证全部身份；同 P/SKU 换商品拒绝，未知身份版本拒绝。移出的项停用但保留库存/订单；再次上架不会复活旧确认。
- 旧库出现库存/订单却没有可信身份登记，默认 `CATALOG_MIGRATION_REQUIRED`。只添加 schema，不删除或改写原业务数据，不从当前文件猜测历史身份。
- `trade_confirmation_catalog` 独立绑定新 create 确认与每 SKU 版本，不改历史 payload、request_hash 或 snapshot_hash。旧 pending 无绑定、目录事实变化、商品移出，均禁止批准；原 M2 请求键重放仍先于报价，并返回原确认。已提交决议仍可重放；历史取消回补不要求商品仍可售，但要求身份可信。
- `get_catalog_bindings(sku_ids)` 为后续候选核验提供最小服务端版本；不能给旧历史来源自动补签。
- 默认目录从 `configs/catalogs/runtime-demo-v1.json` 检查字节指纹；本地和镜像使用同一 manifest，防止空库静默切换。未冻结真实模型或部署镜像。
- `python -m scripts.catalog_preflight` 为只读 dry-run。`--database` 必须已有 SQLite，mode=ro，报告只含计数/商品 ID 等，不输出买家或地址；没有 apply/自动补签选项。source 文件只说明待比对材料，不证明旧库实际来源。

## 使用和恢复

```sh
.venv/bin/python -m scripts.catalog_preflight \
  --source-catalog data/catalog-demo.jsonl.gz \
  --target-catalog data/catalog-esci-v2.jsonl.gz \
  --database <已有数据库文件> \
  --output eval/verification/<新目录>/catalog-preflight.json
```

目录不兼容或旧库身份未证实时退出 2。保留原库、旧目录和一致性备份；在维护窗口核对来源后，另做稳定内部 ID 映射迁移。当前不提供自动迁移命令。不能通过删表、清空数据库或换空卷“修好”。需要恢复旧应用时停写并恢复匹配的代码/目录/数据库备份；保留新增表，旧代码不理解它们，回退会丢失目录防换绑保证。

## 验证与原始失败

证据：[m3a-catalog](../../../eval/verification/2026-10-16-m3a-catalog/)。
- `demo-v2-preflight.json`：明确 2,000 个 SKU 身份冲突，退出 2；未写入任何用户数据库。
- 首轮旧交易/请求键回归 73 passed、3 failed，保留 `initial.log`。两项旧合同期望未经身份验证自动迁移，改成失败关闭；一项以增量商品列表调用初始化，改成完整当前目录。没有降低错误/库存不变量。
- 新身份合同首轮 13 passed；复测交易/请求键/确认/身份共 117 passed，见 `retest.log`。额外检查默认目录 pin、服务端版本 API 后，最终结果以 M3 集成报告为准。
- 覆盖同价换 ASIN/站点、同 P 换新 SKU、旧库拒绝、停用/重新上架、旧批准侧绑定缺失、generation、一次取消回补、已登记历史订单占用恢复和超卖拒绝。

## 未完成

完整 v2 迁移与旧收藏/历史引用修复没有执行。旧库缺可信外部身份的材料仍需核对，不能由模型猜测。人工 200/50 抽检、真实模型 Agent 十场景、真实容器升级/回滚与公开部署均不是本次合同通过结论。

### 独立复核补强

- 复核发现只读预检原先仅比较同 SKU，若同 P 换 ASIN 并改所有 SKU 会误报兼容（运行期原本已拒绝）。新增 product_id→外部身份比较及数据库预检合同，修后 22 项身份测试通过，见 `review-retest.log`。
- 混合进程的旧内存目录不能将旧配送事实补签成当前 sidecar：持久登记保存可批准事实，prepare 在事务内重新核对目的地。原 `current-terms-retest.log` 59 项通过。
- Harness source/impact 补充新目录配置、来源测试、向量边界、配对 manifest 和独立 train 译文；M3 五组新增合同进入每次必跑清单。完整模型门禁仍由最终报告明确阻塞。

- 复核进一步发现同 SKU 保留外部 ID 却改内部 P 编号时，数据库预检与运行期不一致；预检改为同时核对 product_id 和 identity_hash。首个新增测试错用了 dataclass 字段名，保留 `registry-product-retest.log`；修正 fixture 后另存复测，不覆盖失败。

- 集成首轮固定合同 100 failed / 1094 passed：旧 skill_suite 的合成商品省略 ships_to，却生成 CN 配送确认；新增持久核验正确拒绝。保留完整失败与单例复现，只为合成 fixture 明确 CN 支持，不放宽生产检查；修后结果见集成报告。
