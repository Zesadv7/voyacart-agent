# ESCI Agent 场景准备计划

`preparation.json` 保存 10 个场景，全部绑定 T2 预选文件中的独立 `train` 查询及人工 Exact 商品，原文标题与 ASIN 可直接回查。该文件的状态为 `prepared`，`runnable=false`；正式 `suite.json` 和模型开发档尚待生成。

| 场景 | 站点 | 原始查询 ID | Exact ASIN |
|---|---|---|---|
| 中文查找美国站商品 | us | 1567 | B000MBUOOO |
| 中文查找西班牙站商品 | es | 189 | B08LSVSL84 |
| 中文查找日本站商品 | jp | 3136 | B000AYCEJQ |
| 保留目的地约束 | us | 1901 | B01MXRMREE |
| 更新预算并保留币种归属 | es | 2580 | B0010ZTKH8 |
| 保留有货约束 | jp | 5091 | B01BS3FQFG |
| 比较两件商品 | us | 2008 | B00PUY9DZY、B072194H55 |
| 生成确认卡并等待批准 | es | 2842 | B0771BBFMJ |
| 批准确认后创建订单 | jp | 8670 | B009AVPIIO |
| 远端嵌入故障后使用本地向量 | us | 2775 | B0002AS21W |

场景仍需最终抽样确认。商品 `product_id`/`sku_id` 由 T2 正式目录决定；价格、库存与配送范围来自目录内标注的合成字段。准备计划仅记录待绑定字段与断言，当前没有赋予商品正式编号或猜测交易约束。

中文开发场景需要单独的训练查询译文缓存。现有 200 条中文派生集来自留出查询，保留给后续一次性效果验收；本入口只选择开发训练查询。

从仓库根目录重新生成到新目录、或核对现有准备计划：

```sh
.venv/bin/python eval/harness/esci-v1/prepare.py --prepared eval/verification/2026-10-14-t2-input-plan --output eval/verification/<新的准备目录>
.venv/bin/python eval/harness/esci-v1/prepare.py --prepared eval/verification/2026-10-14-t2-input-plan --validate eval/harness/esci-v1/preparation.json
```

生成与校验仅使用标准库，记录三份输入的 blake2b 内容指纹。校验会拒绝输入变化、场景内容变化、测试划分混入、重复开发查询、缺失商品文本或不足的 Exact 候选。输出目录必须新建。

运行依赖为正式 ESCI 目录、最终 selection、开发 ranking 文件、独立训练译文，以及显式 `esci-v1` 开发合同和真实目录工作流。后续内核只对显式 ESCI 套件启用 10 场景开发合同，并将真实目录绑定到生产 Agent/SQLite/确认服务；现有 v1 的合同、商品、留出要求与默认入口保持原样。

针对性夹具检查见 [检查证据](../../verification/2026-10-14-t4-harness-preparation-final/)，真实训练来源核验见 [来源证据](../../verification/2026-10-14-t4-harness-source-verified/)。独立来源核验首次按通用行分隔规则读取原始商品时失败，失败日志保留在检查证据目录；文件逐行读取后的核验通过。真实模型档和完整 Harness 门禁尚未执行。

## 正式目录开发执行

十场景执行内核现已补齐，当前准备文件与既有来源证据保留原样。`build_suite.py` 从正式目录、最终训练开发查询、`ranking-dev` 和独立训练中文缓存重新选择并绑定十场景；新套件在新目录保存，商品 ID、SKU、价格、库存和配送规则全部来自输入目录。输入缺失或场景不满足时快速失败，准备态继续保留。

```sh
.venv/bin/python eval/harness/esci-v1/build_suite.py --translations data/esci/translations-train-zh-v1.jsonl --index-dir eval/index/esci-harness --local-model-cache eval/models/multilingual --output eval/verification/<新的绑定套件目录>
.venv/bin/python -m scripts.eval.harness run --suite eval/verification/<绑定套件目录>/suite.json --profile dev --output eval/verification/<新的开发执行目录>
```

训练译文行须包含 `locale`、`query_id`、对应原始 `query`、`split=train`、中文 `translation`、`prompt_version` 和 `status=ok`。准备计划中的原始查询仅用于前期来源记录；正式抽样后的绑定由输入文件决定，匹配的独立训练译文缺失时报告具体站点和查询编号。

独立 `esci-dev-v1` 合同只开放 `contracts` 与 `dev`，保留已有安全回归，旧 v1 留出门槛保持原样。执行复用生产 Agent 工厂、工具和隔离交易库；中文检索使用远端向量，故障场景复用 T5 本地多语种接口，模型缓存须预先准备。其它原文场景用生产关键词与硬约束／交易路径。原始请求、工具结果、逐轮计量、输入与源码内容指纹随执行报告保存。

本轮只有隔离合同验证，实际数据及真实模型档仍待执行。实现、验证和具体缺口见 [执行路径记录](../../../docs/改动记录/2026-10-14/T4-真实目录Harness执行路径.md)。
