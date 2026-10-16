# T4 真实目录 Harness 执行路径

2026-10-14，Asia/Shanghai。用户继续优先后端与前端，本阶段在独立 `feat/T4-esci-evaluation` 数据工作树补齐原 T4 的十场景执行内核；起点为 `ef2011f`。功能实现与隔离合同验证完成，真实 ESCI Agent 开发评测仍待正式数据。本阶段未修改模型凭据、默认目录、生产运行时或冻结的容量实验工作树，未提交或推送。

## 变更与执行边界

- `eval/harness/esci-v1/build_suite.py` 从正式 ESCI 目录、最终 `selection.dev`、`ranking-dev` 和独立训练中文译文绑定十场景，创建新套件目录。商品 ID、SKU、ASIN、原文标题、原币报价、库存和目的地全部取自输入目录；预选准备文件保持原样。
- `scripts/eval/harness/esci.py` 检查开发来源、Exact 标注、完整目录引用及训练译文，选择十个独立训练查询，并冻结输入内容指纹。每次加载重新核对场景与输入，准备态、缺失目录／译文、未知商品 ID、输入变化与场景篡改明确失败。中文留出派生文件与 `ranking-holdout` 在读取前拒绝。
- 显式 `suite_type=esci-dev-v1` 使用独立 `contracts`／`dev` 合同，开发档完整覆盖十场景、各重复一次。沿用已有安全回归，加上绑定合同；原 v1 的全部留出覆盖、至少 28 个留出场景和至少 3 次重复条件保持原有分支，默认套件保持 v1。
- `runner`／`workflows` 复用生产 MainAgentFactory、检索工具、隔离 SQLite、ConfirmationService、逐轮调用计量和原始请求证据。独立 `esci_agent` 分支读取完整正式目录，按实际工具结果检查 Exact 命中、原文标题、目的地、更新后的预算、有货、SKU 原币关联和比较商品对。
- 等待确认场景检查确认内容、批准前无订单与库存不变；批准场景重复提交同一决策，检查单个订单与库存恰好减少一次。远端嵌入故障场景在索引就绪后明确注入客户端连接失败，检查实际故障记录及 `local_embedding` 路径，关键词降级保留为该场景失败。
- 中文场景使用独立远端集合 `voyacart_products_esci_harness`；故障场景复用既有 T5 多语种客户端和独立 `voyacart_products_esci_harness_local`。模型缓存只使用本地已有文件。其它原文场景使用生产关键词检索和真实硬约束／交易逻辑，实际策略记录在工具证据中。
- 源码清单加入独立套件的代码与 JSON，运行前后及 verify 阶段核对正式输入内容指纹。HTML 区分 ESCI 目录、演示交易字段与隔离买家；预算更新前后的原始检索记录全部保留。

关键源码：[训练绑定与十场景断言](../../../scripts/eval/harness/esci.py)、[套件生成](../../../eval/harness/esci-v1/build_suite.py)、[套件合同](../../../scripts/eval/harness/contracts.py)、[执行分派](../../../scripts/eval/harness/runner.py)、[共享工作流](../../../scripts/eval/harness/workflows.py)。

## 验证与证据

[首轮针对性检查](../../../eval/verification/2026-10-14-t4-esci-harness-execution-first/pytest.log) 17 项通过，原源码快照和结果保留。追加缺少独立译文检查与预算证据保留后，[最终检查](../../../eval/verification/2026-10-14-t4-esci-harness-execution-final/)保存最终源码、测试日志、差异及预检查结果。

小套件使用隔离构造夹具验证合同与生产 SQLite／确认服务，覆盖训练划分混入、未知目录 ID、场景及合同篡改、输入指纹变化、Unicode 行分隔字符、十场景分派、旧 release 条件和确认幂等。原有失败／取消报告检查继续通过。模型和嵌入调用均为 0，测试结果用于验证执行路径与合同，实际 ESCI 质量结果保持未知。

## 实际待办与启用

正式 `data/catalog-esci-v1.jsonl.gz`、`data/esci/selection-v1.json`、`eval/esci/ranking-dev.jsonl` 与独立训练中文缓存尚未生成。当前预检查快速失败，原 `preparation.json` 仍为 `prepared`、`runnable=false`，正式套件与真实 Agent 开发档均未执行。

数据就绪后生成到新目录，再运行显式开发档：

```sh
.venv/bin/python eval/harness/esci-v1/build_suite.py --translations data/esci/translations-train-zh-v1.jsonl --index-dir eval/index/esci-harness --local-model-cache eval/models/multilingual --output eval/verification/<新的绑定套件目录>
.venv/bin/python -m scripts.eval.harness run --suite eval/verification/<绑定套件目录>/suite.json --profile dev --output eval/verification/<新的真实开发报告目录>
.venv/bin/python -m scripts.eval.harness verify eval/verification/<真实开发报告目录>
```

训练译文缓存每行须包含 `locale`、`query_id`、原始 `query`、`split=train`、非空中文 `translation`、`prompt_version` 与 `status=ok`。该文件保持独立于 200 条中文留出派生集；本阶段未生成译文，也未读取或执行真实留出评测。

数据工作树尚未包含 T5 运行时组件，故障场景的真实执行在整合已有 T5 接口后进行。独立合同已验证绑定、分派和交易断言，真实 MainAgent、远端向量及本地降级效果仍待执行。`make eval-harness` 和真实模型门禁本轮均未执行，后续由统一交付阶段完成。此次增强内核会改变源码指纹，旧已通过模型报告按其原代码版本保留。

回退时继续使用原 v1 默认入口；保留各独立证据目录。应用开关与 `.env` 保持原样。
