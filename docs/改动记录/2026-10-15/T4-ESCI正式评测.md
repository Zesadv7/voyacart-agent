# T4 ESCI 正式评测

**需求：** 生成正式 ESCI 目录和评测集，在开发集上比较检索策略并冻结，留出集只运行一次，产出可引用的检索指标。用户要求半小时内落地，因此确认不做 15,860 次商品级增强，改用查询预分类作为代理类目；中文翻译按用户指定使用已配置的 `gemini-3.8-flash-high`，并发 16。前次记录见 [T2 增强与翻译缓存入口](../2026-10-14/T2-增强与翻译缓存入口.md)、[T4 评测入口准备](../2026-10-14/T4-ESCI评测入口准备.md)、[T4 参数冻结与留出执行](../2026-10-14/T4-参数冻结与留出执行.md)。

分支 `feat/T4-esci-formal-eval` 从 `c4e50d5` 开始。先并入 `feat/T4-frozen-evaluation` 的 `98ef71f`（本分支提交为 `676eeeb`，仅合并了当日 README 的一处冲突），再提交本轮代码 `198899c`。评测运行时的 HEAD 为 `198899c`。

## 具体变更

- [`scripts/esci/proxy_categories.py`](../../../scripts/esci/proxy_categories.py)：取商品关联预选查询的 T1 预分类众数作为类目，同票按既有八类顺序，材质留空，零模型调用。标注不属于预选查询、查询缺少八类预分类、商品与标注不能一一对应时，直接报错。
- [`finalize_catalog.py`](../../../scripts/esci/finalize_catalog.py)：类目来源可以是 `product-enrich-v1` 或 `query-class-proxy-v1`，但同一目录只允许一种。每件商品的 `data_provenance.category_source` 和规模报告都记录类目来源。
- [`enrich_catalog.py`](../../../scripts/esci/enrich_catalog.py)：去掉写死的并发上限 2，改为直接使用 `LLM_MAX_CONCURRENCY`。本轮运行时用环境变量设为 16，`.env` 保持原样。
- [`run_esci.py`](../../../scripts/eval/run_esci.py)：留出档在同一次运行内追加中文派生集的兜底对比。远端嵌入客户端统一抛出连接错误，然后逐条确认实际路径是 `local_embedding`（第 3 级）或 `keyword_2gram`（第 4 级），路径不符就停止。本地索引的设置由 `local_index_settings` 统一生成，对比摘要写入 `approved-v1.json`。
- [`bm25.py`](../../../app/infrastructure/retrieval/bm25.py)：词项缓存容量从 8,192 改为 65,536。原容量小于 12,123 件的目录，按顺序扫描全库时缓存逐条被淘汰，命中率为 0，单次 BM25 约 1.2 秒；调大后预热完成的单次约 0.13–0.23 秒。算法和结果不变。
- `.gitignore` 放行正式目录、抽样、代理类目和中文译文缓存。

## 运行与证据

| 步骤 | 证据 | 结果 |
|---|---|---|
| 代理类目 | [category-proxy](../../../eval/verification/2026-10-15-esci-category-proxy/run.json) | 15,860 件，八类均有覆盖，模型调用 0 |
| 中文翻译 | [translate-zh](../../../eval/verification/2026-10-15-esci-translate-zh/) | 200 次调用全部成功，格式失败 0，合计 98,216 token |
| 定稿 | [finalize](../../../eval/verification/2026-10-15-esci-finalize/report.json) | 12,123 件商品；查询 100/300/100；中文 200 条；缺货比例 10.15%；CN 可配送 85.27% |
| 远端向量预建 | [index-prebuild](../../../eval/verification/2026-10-15-esci-index-prebuild/) | 首次顺序预建写入 270 件后读超时；16 路并发补齐，重试 92 次，用时 440.5 秒 |
| 本地向量预建 | 同上 `local-report.json` | 12,123 件，CPU 用时 898.2 秒 |
| 开发档 | [esci-dev](../../../eval/verification/2026-10-15-esci-dev/) | 四种策略完成，两种带精排的未执行（未配置精排服务） |
| 冻结 | `frozen-v1.json`（历史本地记录，不随公开快照发布） | 按开发全库 nDCG@10 选出 `embedding_only` |
| 留出档 | [esci-holdout](../../../eval/verification/2026-10-15-esci-holdout/) | attempt=1，`COMPLETED`，已写入 `approved-v1.json` |
| 汇总报告 | [esci-formal-eval](../../../eval/verification/2026-10-15-esci-formal-eval/README.md) | 每个数字附口径，分站点完整表 |

留出档主要结果（`embedding_only`，300 条 test 查询，商品库 12,123 件）：
- 全库召回：nDCG@10 0.6120，Recall@10 0.5239，Recall@50 0.8029，judged@10 0.7950；
- 候选内排序 nDCG 0.8459；
- 硬约束：泄漏 0，应为空准确率 1.0；
- 中文派生集（200 条）Recall@50：远端向量 0.6988，本地多语种兜底 0.3780，关键词兜底 0.1522。

针对性测试（ESCI 评测、指标、召回、检索、`scripts/esci`）118 项通过，见 [pytest.log](../../../eval/verification/2026-10-15-esci-formal-code/pytest.log)。

## 未完成项

以下各项未执行：
- 商品级增强与类目人工抽检；
- 中文译文人工抽检；
- 两种带精排的策略；
- 本地兜底的多模型比较；
- 后端全量回归。

`make eval-harness` 未执行。`harness impact` 显示需要确定性契约和真实模型档，Harness 门禁按未通过记录。全量商品增强以后完成时，需要重新定稿：目录指纹会变化，因此要新开开发档，冻结清单和批准基线改用新版本号，现有的 `approved-v1.json` 保留为本轮代理类目口径的基线。

## 启用与回滚

本轮只新增评测数据和评测入口。运行时默认目录、`.env` 和开关都没有改动，只有 BM25 缓存容量这一项运行时改动。回滚方法：
- **评测数据：** 删除本轮新增的数据文件，旧目录和旧评测不受影响；
- **BM25 缓存：** 把容量改回 8,192 即可。

## v2：批量商品增强后的重评（同日补充）

商品级增强由 [T2 批量商品增强](T2-批量商品增强.md) 完成。用 `product-enrich-batch-v1` 的类目重新定稿，目录、评测集与基线放在 v2 位置，v1 的文件原样保留：

- 目录 [`data/catalog-esci-v2.jsonl.gz`](../../../data/catalog-esci-v2.jsonl.gz)，12,299 件；选择记录 `data/esci/selection-v2.json`；评测集 [`eval/esci-v2/`](../../../eval/esci-v2/)；基线 `eval/esci-v2/baselines/{frozen,holdout-attempt,approved}-v2.json`。
- 开发档 [esci-dev-v2](../../../eval/verification/2026-10-15-esci-dev-v2/)：四种策略完成，两种带精排的未执行；按全库 nDCG@10 冻结 `embedding_only`（0.6410）。
- 留出档 [esci-holdout-v2](../../../eval/verification/2026-10-15-esci-holdout-v2/)：attempt=1，`COMPLETED`。
- 汇总与 v1/v2 对照 [esci-formal-eval-v2](../../../eval/verification/2026-10-15-esci-formal-eval-v2/README.md)。

留出档主要结果（`embedding_only`，300 条 test 查询，商品库 12,299 件）：
- 全库召回：nDCG@10 0.6131，Recall@10 0.5018，Recall@50 0.7995，judged@10 0.7927；
- 候选内排序 nDCG 0.8528；
- 硬约束：泄漏 0，应为空准确率 1.0；
- 中文派生集（200 条）Recall@50：远端向量 0.7054，本地多语种兜底 0.3685，关键词兜底 0.1734。

两版 nDCG@10 与 Recall@50 相差不到 0.004，类目不进入全库召回。两版查询集不完全相同（留出集重合 239/300），v2 留出结果不独立于 v1 的留出结果，参数只按 v2 开发集选定。

向量预建的两次失败（`.env` 的 `QDRANT_URL` 使评测连向服务端；缺少 `fastembed`）已改为设空 `QDRANT_URL` 并用 `uv sync --frozen --inexact --extra retrieval` 安装可选依赖后重跑，`uv.lock` 未变，原始日志保留在 [index-prebuild-v2](../../../eval/verification/2026-10-15-esci-index-prebuild-v2/)。仍未执行的项目同上文：人工抽检、精排、多模型比较、`make eval-harness`、后端全量回归。

公开快照说明：本文为历史开发记录，六份 `eval/esci*/baselines/` 元数据及 `eval/verification/` 原始证据仅本地保留。公开仓库不能单独复现本文历史报告；当前验证以新提交的 CI 结果为准。
