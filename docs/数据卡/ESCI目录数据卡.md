# Voyacart ESCI 与演示目录数据卡

版本：M3c，2026-10-16（Asia/Shanghai）。本卡基于本地冻结数据与历史证据。M3c 阶段未调用模型、未完成人工审核或上游许可核验；同日公开快照准备补录了下述固定版本许可。

## 使用边界

应用默认仍是 `data/catalog-demo.jsonl.gz` 的 5,700 件混合演示目录。`catalog-esci-v2` 的 12,299 件是独立评测目录，尚未作为默认运行目录安全迁移。以下字段用于检索与交易流程演示，不代表商家实时可售事实，也不能以模型推断材质保证防过敏、安全用途或“无塑料”。

内部 P 编号不是外部商品身份，不能跨版本按同号直接替换。demo 与 v2 共享的 2,000 个 P 编号指向不同商品。外部身份至少按 `(source_platform, source_locale, external_product_id)` 区分；同 ASIN 跨站点不能视为同一条记录。目录身份保护和迁移状态由对应 M3 工程记录说明，本卡不代表迁移完成。

## 来源与许可状态

ESCI 来源记录位于 [`data/esci/source.json`](../../data/esci/source.json)，固定来源提交为 `7916cdf6ab75a462e77f20ab40428a10923998d5`。该记录保存两份原始 parquet 的下载 URL、字节数和 blake2b：examples 为 51,286,808 字节，products 为 1,108,857,465 字节。URL 使用上游 main 路径，不能仅凭 URL 宣称版本固定，应同时核对提交和内容指纹。本轮只读该来源记录，没有重新下载、独立验证上游内容或访问商家页面。

M3c 初次记录时只找到设计文档中的 Apache-2.0 声明，未随固定版本保存 LICENSE/NOTICE。2026-10-16 公开快照准备时，已从上述固定提交取回并逐字节核对 [LICENSE](../../third_party/esci-data/LICENSE) 与 [NOTICE](../../third_party/esci-data/NOTICE)，原始文本随源码保存，并 COPY 到运行镜像 `/app/third_party/esci-data/`。[固定来源链接与说明](../../third_party/esci-data/README.md)单独保留。此补录解决已识别的许可文本与归属缺口，不替代商品事实审核，不声称商家背书，也不为整个项目设置许可证。

## 目录组成与角色

| 目录 | 商品 / SKU 数 | 来源及用途 |
|---|---:|---|
| demo | 5,700 / 9,105 | 默认运行目录：3,700 合成商品 + 2,000 ESCI train/Exact 原文字段商品 |
| esci-v1 | 12,123 / 12,123 | 历史查询代理类目评测目录，不与 v2 输入混用 |
| esci-v2 | 12,299 / 12,299 | 批量模型增强后的独立检索评测目录 |

本轮重新读取冻结 gzip 的数量、locale、来源字段分布及完整 blake2b，见 [`source-audit.json`](../../eval/verification/2026-10-16-m3c-provenance/source-audit.json)。三个目录未改写。

- demo 的 ESCI 部分是 US 1,000 / ES 500 / JP 500，类目由 train/Exact 关联查询的预分类多数票产生，同票按原类目顺序。查询类目本身是模型分类，商品类目是查询代理；未进行商品级人工核验。材质为空且状态为 unknown。
- demo 的合成部分中，3,200 条显式记录 `data_provenance="synthetic"`，500 条旧合成样例缺该字段。3,700 合成的来源有历史设计与生成脚本依据，但不能把缺字段的单条 DTO 在界面上自动伪造来源；前端保守显示“来源未说明”。参见 [历史数据说明](../DESIGN.md#21-原数据与原评测)、[`generate_catalog_fixture.py`](../../scripts/generate_catalog_fixture.py)、[`generate_catalog_multilingual.py`](../../scripts/generate_catalog_multilingual.py)。
- v1 类目来源为 `query-class-proxy-v1`，材质留空。它使用预选查询关联标签，不等同 demo 的仅 train/Exact 规则。
- v2 为 US 5,140 / ES 3,391 / JP 3,768；dev 100 条 train 查询，holdout 300 条 test 查询，distractors 100 条 train 查询；200 条中文查询是从 holdout 派生，不能充当独立 train 译文。数量与来源见 [v2 定稿报告](../../eval/verification/2026-10-15-esci-finalize-v2/report.json)、[`selection-v2.json`](../../data/esci/selection-v2.json)。

## 字段含义与处理

| 字段 | 来源与处理 | 展示与使用限制 |
|---|---|---|
| title、brand | 原始商品表字段，去首尾空白 | 原文，不声称已翻译 |
| description | HTML 转纯文本、合并空白、截到 1,000 字符 | 并非完整商家页面原文 |
| highlights | 原始 bullet 按行处理，每条最多 200 字符，最多 5 条；颜色可另加一条 | 可能截断；不把空字段补成事实 |
| SKU spec | 原颜色字段清洗；无颜色时为“默认” | 非真实完整 SKU 变体目录 |
| category | demo/v1 查询代理；v2 为 `product-enrich-batch-v1` 模型派生 | 明示“查询代理/模型推断，未人工核验” |
| material_tags | demo/v1 未知；v2 模型推断 | 空列表不是不存在该材质的证明；不能作为安全或过敏承诺 |
| origin_country | ESCI 依据 source_locale 映射得到 | 来源站点代理；不是制造产地、仓库地址或实际发货地 |
| source_locale / source_language | ESCI 站点及其语言映射 | US/ES/JP 为来源站点，不代表用户目的地 |
| price、stock、ships_to | 固定种子生成的演示商业字段 | 非商家报价、库存或履约保证；当前交易库存为本地演示账本 |
| 到手价、运费、税费 | 演示价格加模拟交易规则计算 | 不能作为真实结账报价或税务结论 |
| 图片/评分 | 示意图或缺图；目录样例评分如有 | 图片须保持示意标记；样例评分不能升级为实时评价 |

字段生成的直接证据是 [`catalog_records.py`](../../scripts/esci/catalog_records.py)、[`build_demo_catalog.py`](../../scripts/esci/build_demo_catalog.py)、[`finalize_catalog.py`](../../scripts/esci/finalize_catalog.py)。v1/v2 旧记录没有 `origin_country_method` 字段，但生成函数仍以 locale 生成 `origin_country`；前端据已知 ESCI 来源保守解释为站点代理，不回写冻结原数据。

## 派生模型与历史评测

历史批量增强使用 `gemini-3.8-flash-high`、提示词版本 `product-enrich-batch-v1`，默认每次 10 件，失败批拆成单件但沿用同版本。2026-10-15 18:01–19:23（Asia/Shanghai）共 5 轮，原始中断/失败保留。原始提示词、模型与开始时间位于各轮 manifest；请求、响应及重试计数保留在原运行目录，不由本卡复制重写。

- [批量增强目录与逐轮账本](../../eval/verification/2026-10-15-esci-enrich-batch/)、[统计](../../eval/verification/2026-10-15-esci-enrich-batch/stats.json)、[历史改动记录](../改动记录/2026-10-15/T2-批量商品增强.md)
- [中文翻译 manifest](../../eval/verification/2026-10-15-esci-translate-v2/manifest.json)：`translate-zh-v1`；翻译对象是查询，不是商品标题
- [历史 v2 检索评测](../../eval/verification/2026-10-15-esci-formal-eval-v2/README.md)：结果绑定当时的数据、代码与模型；不代表当前 M3 最终源码已重新验收或真实 Agent 十场景通过

批量与有效单件增强结果重合 740 件，全部为 ES 站点；类目一致率 78.5%、材质标签完全一致率 97.2%。这只是两种模型运行结果的描述性一致程度，不是对人工金标的准确率，也不能外推到所有商品/语言。历史 v1/v2 留出查询重合，两个结果并非相互独立的留出实验。

## 人工 QA 与完成状态

本轮读取原始表：类目 200 行、译文 50 行，“正确”和“备注”已填均为 0。有效人工审核数均为 0；准确率未知；原始定稿报告仍为 `manual_audit=pending`。没有把 AI 判断或空白行计为人工通过。

已准备 [固定审核规则](ESCI人工抽检规则.md) 与独立 [类目待审副本](../../eval/verification/2026-10-16-m3c-provenance/audit/category-review-200.csv)、[译文待审副本](../../eval/verification/2026-10-16-m3c-provenance/audit/translation-review-50.csv)。原抽检表和已消费的 holdout 均不改写。规则与工作表准备好不等于审核完成。

M3c 的前后端来源合同属于离线工程验证；真实模型 smoke、真实十场景及完整效果门禁仍需分别完成。M3c 阶段未安装软件、未运行外部服务/模型、未推送；同日许可补录与容器 CI 准备见 [容器 Mock CI](../容器Mock-CI.md)。公开源码快照不包含 `eval/verification/` 历史诊断证据，相关相对链接仅供保留原交付记录的本地副本使用。
