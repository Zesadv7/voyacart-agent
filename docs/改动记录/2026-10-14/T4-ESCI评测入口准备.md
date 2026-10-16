# T4 ESCI 评测入口准备

本轮准备指标与开发档入口，使用独立构造夹具验证。真实 ESCI 效果、正式开发报告、留出基线与默认策略均待后续执行。分支 `feat/T4-esci-evaluation` 从 T2 提交 `31944b26` 继续，应用运行时代码与旧评测入口保持原样。

## 具体变更

- `scripts/eval/metrics.py` 增加 `graded_ndcg_at_k` 与 `judged_at_k`。分级增益直接取 E/S/C/I = 1.0/0.1/0.01/0，未标注商品增益为 0，理想顺序按全部人工增益排序；原有 `ndcg_at_k` 及其它旧函数逻辑保持原样。
- `judged_at_k` 统计前 K 条去重返回结果中有人工标注的比例，I 也计为已标注；分母为实际返回条数，空结果记为 0。报告同时给出返回数量，便于解读短结果。
- `scripts/eval/run_esci.py` 提供显式开发档入口。默认只运行关键词与 BM25；其它可选策略为纯向量、向量加精排、混合 RRF、混合加精排。缺少精排配置时对应策略标为未执行。
- 排序只给人工候选打分，包含零分候选，BM25 的文档频率和平均长度取完整目录。向量排序从现有 COSINE 索引按人工候选 ID 取分；混合排序复用现有 RRF；精排复用现有校验与接口。
- 全库召回调用真实 `CatalogSearchUseCase`，`top_k=50`，不传查询类目；硬约束使用 `top_k=5`，验证配送、目标币种预算、库存与声明的材质限制。查询类目保留为报告元数据。
- 所有密集策略复用同批查询向量，查询编码耗时单列。精确商品 ID 的直接查询保留，实际路径单独记录。向量或精排故障会保留实际降级路径并明确失败，硬约束泄漏与应为空用例错误也明确失败。
- 每次运行输出报告、失败样本、源码及输入内容指纹。向量策略使用独立评测集合 `voyacart_products_esci_eval`，不替换应用集合；建库不完整时停止评测。

## 留出与中文派生集

T2 的中文派生集来自最终留出查询。当前 CLI 只读取 `ranking-dev.jsonl`、`retrieval-dev.jsonl`、`constraints-dev.jsonl`，中文派生组与真实留出明确标为未执行，默认策略明确标为未选。通用核心的中文组只使用独立构造夹具验证。

正式冻结、留出仅运行一次的记录、`approved-v1.json`、实际中文效果报告与新 Harness 的真实目录工作流，均保留为 T4 后续事项。本轮未读取真实留出指标、未冻结参数、未写批准基线。

## 验证与证据

[首次夹具与旧指标回归](../../../eval/verification/2026-10-14-t4-esci-tests/pytest.log)：57 项通过。

补齐精确 ID 直查的回归后，[最终验证](../../../eval/verification/2026-10-14-t4-esci-final/pytest.log) 58 项通过。命令：

```sh
LLM_API_KEY=unit-test-key LLM_BASE_URL=http://127.0.0.1:1/v1 LLM_MODEL=unit-test .venv/bin/python -m pytest -q tests/test_esci_metrics.py tests/test_esci_evaluation.py tests/test_recall_metrics.py --basetemp eval/verification/2026-10-14-t4-esci-final/fixtures
```

验证包括手算分级增益、未标注结果、重复 ID、全库 BM25 统计量、6 个策略与 4 组独立夹具、硬约束 0 泄漏、精排未配置、向量与精排故障、精确 ID 直查，以及开发 CLI 的留出读取隔离。假服务与故障样本、夹具报告、源码清单保存在证据目录；真实模型与外部服务调用均为 0。

[Harness 范围诊断](../../../eval/verification/2026-10-14-t4-esci-final/harness-impact.json) 返回 `harness_changed=true`，要求确定性合同与真实模型档。本轮只执行上述小套件，完整 `make eval-harness` 门禁未执行，不能据此宣称该门禁通过。

## 启用方式与未完成项

正式数据就绪后，在项目根目录可使用以下命令。以下实际 ESCI 命令本轮未执行：

```sh
.venv/bin/python -m scripts.eval.run_esci --catalog data/catalog-esci-v1.jsonl.gz --dataset-dir eval/esci --output eval/verification/<新的开发报告目录>
.venv/bin/python -m scripts.eval.run_esci --catalog data/catalog-esci-v1.jsonl.gz --dataset-dir eval/esci --strategies keyword_2gram bm25 embedding_only embedding_rerank hybrid_only hybrid_rerank --output eval/verification/<新的完整策略开发报告目录>
```

向量和精排通过已有 `EMBEDDING_*`、`RERANKER_*` 配置启用。回退时继续使用现有旧入口，保留本轮独立报告和评测向量集合。`.env`、应用默认目录、运行时开关均保持原样。

实际目录、正式开发与留出结果、中文跨语种效果、冻结及一次留出门禁、人工抽检准确率、ESCI Harness 真实目录工作流与完整 Harness 评测均待完成。T4 尚未交付完成。

## 独立 Harness 场景准备

追加 `eval/harness/esci-v1/`：10 个开发场景绑定真实 T2 预选训练查询和 11 件人工 Exact 商品，覆盖 3 个站点的中文查找、目的地/预算/库存、比较、等待确认、批准下单及嵌入服务故障。每个场景记录原始查询、原文标题、ASIN、预期断言与待绑定字段；整体明确 `prepared`、`runnable=false`、模型调用 0。

`prepare.py` 使用标准库生成并校验准备计划，校验三份输入内容指纹及场景内容。当前保持执行内核、`scripts/eval/`、`tests/` 和旧 v1 文件原样；针对性检查源与日志放在证据目录，静态准备计划不会改变旧 v1 的默认源码指纹范围。

[初次针对性检查](../../../eval/verification/2026-10-14-t4-harness-preparation/pytest.log) 7 项通过，初始入口和检查源快照保留在同目录。入口收敛到独立套件后，[最终针对性检查](../../../eval/verification/2026-10-14-t4-harness-preparation-final/pytest.log) 同样 7 项通过；覆盖确定性生成、10 个独立训练查询、比较商品对、计划篡改、查询/标注划分混入、重复查询、商品缺失、Exact 缺失和输入字节变化。

[独立来源核验首次失败](../../../eval/verification/2026-10-14-t4-harness-preparation-final/real-source-verification.log)：原始商品标题含 Unicode 行分隔符，通用 `splitlines()` 将合法 JSON 字符串拆断，导致 `JSONDecodeError`。场景生成器使用文件逐行读取，生成与小套件保持通过；核验脚本改用文件逐行读取，失败原稿与日志保持原样。

[真实来源复核](../../../eval/verification/2026-10-14-t4-harness-source-verified/real-source-verification.json) 逐场景核对训练查询、E 人工标签和商品原文；重生成的准备文件逐字节一致。源码指纹、原始日志、检查脚本和输入来源保存在各阶段证据目录。全部检查使用本地文件，模型与 HTTP 调用 0；真实 Agent 开发档、检索效果与完整 Harness 门禁保持未执行。T2 共用读取入口的同类根因随后修复，过程见 [ESCI JSONL 原文读取](T2-JSONL原文读取.md)。

当前具体依赖为 `data/catalog-esci-v1.jsonl.gz`、`data/esci/selection-v1.json`、`eval/esci/ranking-dev.jsonl`、独立训练查询中文译文，以及显式 ESCI 开发合同与真实目录工作流。全量商品增强尚未完成，正式编号和交易约束无法绑定。中文 200 条来自留出集，保留给一次性效果验收，开发计划仅使用 `train` 查询。

后续内核最小方案为：显式 ESCI 套件启用 10 场景开发合同，准备态在模型创建前拒绝；独立 `esci_agent` 执行分支绑定真实目录，复用生产工厂、本地交易服务及逐轮计量；依据真实工具结果校验硬约束、确认链路和降级路径。用户本轮优先后端与前端，此方案保持待办，当前交付仅为可核验准备计划。生成和校验方式见 [独立套件说明](../../../eval/harness/esci-v1/README.md)。
