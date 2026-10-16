# Voyacart 改造执行计划

本文件把 [DESIGN.md](DESIGN.md) v2 拆成一张张任务卡，每张可以交给一个 agent 独立完成。"为什么这样做"看 DESIGN.md；本文件只写做什么、改哪里、怎么验收。

**2026-10-14 执行调整**：用户要求优先完成后端和前端界面，数据准备继续后台收尾。本轮先实现 C1–C3 与 F1，采用针对性测试、构建和关键流程检查推进；完整数据评测、容量矩阵及大规模故障演练后续补测。改造前版本固定为 `7f29c08`，保留短程锁竞争基线，后续容量对照使用同一版本。各项完整验收的未执行状态单独记录。

用户随后确认后端和前端完成后统一交付一个 PR；本轮在整合分支连续开发，各任务卡的实现与验证分别记录。

**2026-10-14 本轮收尾范围**：用户明确将完整数据、性能和质量验收转为后续待办，以现有环境可运行、核心流程可演示、必要证据齐全、原 PR 与延期清单更新作为本轮完成标准。保持 v3 默认目录和已确认模型配置；停止新增评测、架构与依赖扩展。容量实验在收到指令时的当前用户档位收尾后停止；干净机器部署、ESCI 全量增强、正式留出、模型比较及人工抽检本轮延期。独立数据分支的新评测 WIP 保留在本地，本轮统一 PR 继续使用已验证的整合源码。下面原任务卡作为后续计划保留。

**后续接入调整**：用户要求先接入 2,000 件真实亚马逊商品，执行 train／E 小批选品，复用演示交易字段，追加原 3,700 件目录。完整增强、正式评测与人工抽检保持延期；本次仅完成目录、索引和实际页面流程。

## 0. 所有 agent 共同遵守

1. **开工前**：先读本仓库的 `AGENTS.md`、`docs/DESIGN.md` 和自己的任务卡。任务卡与 DESIGN.md 冲突时，以任务卡为准，并在 PR 里写明冲突点。
2. **分支与 PR**：每张任务卡单独开分支 `feat/<任务编号>-<简短英文名>`，完成后向 fork 的 `main` 发 PR，由用户审核合并。agent 不直接推送 `main`。
3. **署名**：commit 不加 Claude 的 Co-Authored-By；PR 正文、评论和回复不加 Claude 署名。
4. **禁用词**：遵守 本机工作区上层协作约定 的禁用词规定，即英文"冒烟测试"的连写词，以及那个哈希算法名的小写连写形式。文档、代码注释、提交信息里都不能出现，内容哈希一律写作"内容指纹"。现有代码中对哈希库的调用不受此限。
5. **改动记录**：按 `AGENTS.md` 写 `docs/改动记录/YYYY-MM-DD/功能点.md`。
6. **Harness 评测**：改了提示词、工具合同、上下文、记忆注入或调用计量，交付前要跑 `make eval-harness`。
7. **凭据**：`.env` 由用户填写。agent 不修改 `.env`，也不在输出里打印密钥。缺凭据时停下来，说明缺哪个字段。
8. **如实报告**：测试失败、步骤跳过、指标未知，一律在 PR 里照实写。外部服务没跑通，不能写成"通过"。原始证据放在 `eval/verification/` 下，每次运行用一个新目录。
9. **修复原则**：只修根因；只在系统边界做校验；不吞错误，尽早失败；不写推测性的功能。
10. **保留兜底**：DESIGN.md 2.5 节列出的降级兜底一条都不能删、不能削弱。新增能力必须带开关，默认关闭；开关关闭时，现有测试必须原样通过。
11. **热点文件**：`app/composition.py`、`app/infrastructure/settings.py`、`app/application/prompts/voyacart.yml`、`uv.lock`、`frontend/package-lock.json` 这几个文件，并行任务都可能改到。合并前要先 rebase 到最新的 `main`，冲突自己解决，不覆盖别人的改动。

## 1. 固定参数

所有任务卡共用以下参数。要改参数，先改本节，再改代码。

**类目**（共 8 个，与 `product_search_tool.py` 的 `_KNOWN_CATEGORIES` 一致）：

> 旅行装备、户外运动、数码配件、家居生活、美妆个护、厨房餐饮、办公学习、母婴宠物

不属于以上 8 类的一律记为 `其他`。

**材质标签**（共 7 个，取自现有目录）：

> 合成聚合物、金属、天然纤维、玻璃、陶瓷、纸、天然材料

**站点映射**：

| ESCI 站点 | 币种 | 本地市场 | `origin_country` | `source_language` |
|---|---|---|---|---|
| us | USD | US | US | en |
| es | EUR | EU | ES | es |
| jp | JPY | JP | JP | ja |

**规模**：
- T1 预分类候选池：本轮按用户 2026-10-14 的最新要求使用 2,000 条查询，train、test 各 1,000 条，站点比例 2 : 1 : 1。原定每个划分 6,000 条保留为后续完整规模参数；已有缓存继续复用。
- 查询：本轮按用户确认的试运行规模，开发集 100 条，取自 ESCI 的 train 划分；留出集 300 条，取自 test 划分。站点比例 2 : 1 : 1。原定完整规模为开发 300、留出 900 条。
- 干扰查询：本轮 100 条，从 train 划分中未被选中的查询里取，只取 8 个类目的查询，站点比例同上。原定完整规模为 400 条。
- 中文派生集维持 200 条，取自本轮 300 条留出查询。T2 报告实际商品规模；原约 4 万件为完整规模参考值。

**随机种子**：全局种子为 `20261014`。
- 商品级的种子：`f"{全局种子}:{locale}:{asin}"`。
- 查询抽样：先按 `(locale, split, 类目)` 分层，每层内按 `query_id` 排序，再用全局种子打乱。

**价格**：
- 对数正态分布，参数以人民币中位数给出（见下表），σ = 0.6。
- 截断到 [10, 5000] 元，再用 `exchange_rate.py` 的汇率换算成站点币种。
- 日元取整；美元和欧元保留两位小数。

| 类目 | 中位数（元） |
|---|---|
| 旅行装备 | 260 |
| 户外运动 | 300 |
| 数码配件 | 180 |
| 家居生活 | 150 |
| 美妆个护 | 120 |
| 厨房餐饮 | 160 |
| 办公学习 | 80 |
| 母婴宠物 | 140 |
| 其他 | 200 |

**库存**：10% 的商品库存为 0；其余在 [5, 200] 区间内均匀取整数。

**可配送目的地**：
- 本地市场：必定包含；
- CN：以 85% 的概率包含；
- 其余目的地：各以 30% 的概率包含。

**文本截断**：
- 要点：最多 5 条，每条最多 200 字符，写入 `highlights`，标签为 `要点`；
- 颜色：单独一条，标签为 `颜色`；
- 描述：先去掉 HTML，再截到 1,000 字符。

**商品编号**：按 `(locale, asin)` 排序后，从 `P100000` 开始顺序编号。SKU 编号为 `P{n}-S1`。

**评测增益**：E、S、C、I 四档分别为 1.0、0.1、0.01、0。

**本地向量兜底**：

| 配置项 | 默认值 |
|---|---|
| `LOCAL_EMBEDDING_FALLBACK` | 0 |
| `QDRANT_LOCAL_COLLECTION` | `voyacart_products_local` |

**推送与队列**：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `AGUI_DELTA_COALESCE_MS` | 0 | 多进程压测配置中设为 50 |
| 过期恢复周期 | 5 秒 | |
| 有信号时的心跳注释间隔 | 15 秒 | |
| 无信号时的轮询间隔 | 150ms | 保持现状 |
| `AGUI_QUEUE_ENABLED` | 0 | |
| `AGUI_QUEUE_MAX_PENDING` | 200 | |

**假模型**：
- 首 token 延迟 800 毫秒，之后每个 token 间隔 30 毫秒；
- 每次回答约 300 token；
- 10% 的会话走"检索 → 下单"；
- 嵌入维度 1024。

## 2. 需要用户提供或参与的事项

| 事项 | 用在哪里 | 说明 |
|---|---|---|
| `.env` 里的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`EMBEDDING_*` | T1–T5，以及 Harness 评测 | 查询预分类约 1.2 万次短调用；商品增强约 4 万次调用，每次约 150 token 输入、30 token 输出；向量化约 1,200 万 token |
| `RERANKER_BASE_URL`、`RERANKER_MODEL`（可选） | T4 | 不配置时，带精排的策略标为"未执行" |
| 人工抽检 | T2 交付后 | 类目 200 条、中文译文 50 条。T2 导出 CSV，用户填写"正确／错误／修正值" |
| Docker 资源 | M3 起 | 压测需要 Redis、Qdrant、Nginx。建议给 Docker 至少 8 核、16GB，并在报告中记录机器配置 |
| PR 审核与合并 | 全部 | 每张任务卡一个 PR |

## 3. 依赖关系

```
T1 → T2 → T3 → T4 → T5
          T3 → F1
          T3 → L1 → L2 → C1 → C2 → R1 → R2
                      L2 → C3 ──────↗
```

可以并行的任务：
- F1 可以和 T4、L1 并行；
- C3 可以和 C1、C2 并行；
- T5 可以和 L 系列、C 系列并行。

## 4. 任务卡

### M1 数据与评测

#### T1 ESCI 原始数据与查询预分类

- **依赖**：无
- **改动范围**：
  - `scripts/esci/`（新建）
  - `pyproject.toml`：新增可选依赖组 `esci`（pyarrow）
  - `.gitignore`：忽略 `data/raw/`
  - `data/esci/source.json`、`data/esci/query-class-v1.jsonl`（入库）
- **步骤**：
  1. **下载原始数据**：从 `https://media.githubusercontent.com/media/amazon-science/esci-data/main/shopping_queries_dataset/<文件名>` 下载两个文件到 `data/raw/esci/`：
     - `shopping_queries_dataset_examples.parquet`，约 51MB；
     - `shopping_queries_dataset_products.parquet`，约 1.1GB。

     文件大小和内容指纹写入 `source.json`。下载失败或内容指纹不符时，报错退出。
  2. **建候选池**：读取 `small_version == 1` 的样本，按第 1 节本轮试运行规模，从 train、test 两个划分各抽 1,000 条查询。通过 `--queries-per-split 6000` 可显式恢复完整候选规模。
  3. **预分类**：用 `.env` 中的模型对候选池做预分类。
     - 温度 0，输出 `{"category": ...}`，提示词版本号 `query-class-v1`；
     - 缓存键为 `(locale, query_id, 提示词版本号)`；
     - 并发遵守 `LLM_MAX_CONCURRENCY`；
     - 请求超时沿用 `LLM_MAX_RETRIES` 的重试上限，退避 6、18 秒，每次尝试分别计数并保留失败证据；重试耗尽、HTTP 服务错误或其他连接错误时停止；
     - 输出不在枚举范围内的，记为失败，不做猜测。
- **完成标准**：
  - 第二次运行全部命中缓存，模型调用次数为 0；
  - 输出按 `(locale, split, 类目)` 统计的分布表；
  - 每个站点在 8 个类目内的可用查询数，都不低于目标数的 1.3 倍。不足时在 PR 中写明缺口。

#### T2 商品增强、抽样与目录生成

- **依赖**：T1
- **改动范围**：
  - `scripts/esci/build_catalog.py`
  - `data/esci/enrichment-v1.jsonl`、`data/esci/selection-v1.json`
  - `data/catalog-esci-v1.jsonl.gz`
  - `eval/esci/*.jsonl`
  - `data/esci/audit/*.csv`
  - `.gitignore`：放行以上数据文件
- **步骤**：
  1. 按 DESIGN.md 3.2 节的第 4–9 步执行。商品增强的提示词版本号为 `product-enrich-v1`，输出 `{"category": ..., "material_tags": [...]}`。
  2. 生成评测集：
     - `ranking-{dev,holdout}.jsonl`：每行包含 `query_id`、`query`、`locale` 和候选列表 `[{product_id, label}]`。
     - `retrieval-{dev,holdout}.jsonl`：每行包含 `query_id`、`query`、`locale`、`labels: {product_id: E|S|C|I}`。
     - `constraints-{dev,holdout}.jsonl`：
       - 目的地：随机选一个 `ship_to`；
       - 价格上限：取该查询所有 Exact 商品在目标币种下的价格中位数；
       - 要求有货；
       - 另有 10% 的用例把价格上限设在最低价以下，作为"应返回空结果"的用例。
     - `crosslingual-zh.jsonl`：从留出集抽 200 条查询翻译成中文，提示词版本号 `translate-zh-v1`，结果带缓存。
  3. 导出抽检文件 `data/esci/audit/category-sample-200.csv` 和 `translation-sample-50.csv`，留出供用户填写的列。
  4. 保证可复现：JSONL 按键排序、使用固定分隔符；gzip 文件头的时间戳设为 0。
- **完成标准**：
  - 在缓存齐全的情况下连跑两次，输出逐字节一致；
  - 输出规模报告：查询数、商品数、类目分布、缺货比例、各目的地的覆盖率。

#### T3 切换默认目录

- **依赖**：T2
- **改动范围**：
  - `seed_products.py`：路径改为常量，支持 gzip，默认指向 `catalog-esci-v1.jsonl.gz`；
  - 评测旧数据集的脚本和测试：显式指定各自的目录文件。需要检查的有：`run_product_recall.py`、`full_catalog_retrieval.py`、`retrieval_multilingual.py`、`run_manifest.py`、`build_product_media.py`、`test_catalog_media_manifest.py`、`test_multilingual_retrieval.py`、`test_full_catalog_eval.py`、`test_runtime_distribution.py`、`Dockerfile`；
  - `data_quality.py`、`validate_datasets.py`：新增 ESCI 门禁，旧门禁保留；
  - `voyacart.yml`：按 DESIGN.md 3.7 节修改提示词。
- **不改**：旧目录和旧评测集、商品图片接口、前端。
- **完成标准**：
  - `uv run python -m pytest -q` 全部通过；
  - `npm --prefix frontend test` 通过；
  - `uv run python -m scripts.eval.validate_datasets` 通过，新旧两套门禁都要检查；
  - 用现有的 `offline-fallback` 档在 v1 评测集上重跑，复现 `keyword_2gram` 的基线数值；
  - `make eval-harness` 跑完，且 `verify` 通过；
  - 启动后首次建索引的耗时写进 PR；
  - 在页面发送"预算 300 元以内，找一个寄到中国的轻便背包"，能返回 ESCI 商品，PR 附截图。

#### T4 ESCI 评测与基线

- **依赖**：T3
- **改动范围**：
  - `scripts/eval/metrics.py`：新增分级 nDCG 和 judged@k，附单元测试，用手算例子覆盖 ESCI 的增益；
  - `scripts/eval/run_esci.py`；
  - `eval/esci/baselines/approved-v1.json`（历史本地记录，不随公开快照发布）；
  - `eval/harness/esci-v1/`：新套件，包含 10 个场景；
  - `eval/verification/<日期>-esci-*/`。
- **步骤**：
  1. **排序评测**：直接调用检索组件，在每条查询的候选列表内打分：
     - 向量：用余弦相似度；
     - BM25：用全库的统计量；
     - 精排：调用精排接口；
     - 混合：对前两者的排名做 RRF 融合。
  2. **全库召回与硬约束评测**：调用 `CatalogSearchUseCase.execute`。全库召回时不传类目，`top_k=50`。
  3. **中文跨语种评测**：沿用全库召回的口径，单独出报告。
  4. **选定并冻结**：在开发集上跑全部策略，按全库 nDCG@10 选出默认策略，把参数写入清单冻结。然后对留出集只跑一次，结果写入 `approved-v1.json`。
  5. **新 Harness 套件**：按 DESIGN.md 3.6 节新建 `esci-v1` 套件，现有的 v1 套件不动，再跑一次开发档。
  6. **抽检结果**：读取用户填好的抽检 CSV，报告准确率。如果 CSV 还没填，标为"未完成"。
- **完成标准**：
  - 开发集、留出集各有一份报告，包含全部指标、失败样本和源码指纹；
  - 能从清单中证明留出集只跑过一次；
  - 类目抽检准确率 ≥ 90%（以用户填写的结果为准）。

#### T5 本地多语种向量兜底

- **依赖**：T4
- **改动范围**：
  - 新增 `app/infrastructure/embedding/local_multilingual.py`：从评测脚本中的实现提升而来，评测脚本改为直接引用它；
  - `catalog_search.py`：在纯向量这一级失败之后、关键词这一级之前，插入本地向量这一级，`recall_strategy` 记为 `local_embedding`；
  - 索引启动流程：开关打开时，同时为独立集合建立索引；
  - `settings.py`：新增开关和集合名两项配置。
- **步骤**：
  1. 在中文派生集的开发部分，比较 fastembed 支持的 2–3 个多语种模型，按 Recall@50 和 CPU 单次查询的 P95 延迟选定一个。
  2. 用 DESIGN.md 3.6 节"降级"那组评测，测第 3 级和第 4 级的效果。
- **完成标准**：
  - 开关关闭时，现有测试原样通过；
  - 新增测试：嵌入客户端抛错时，检索走本地向量，结果满足全部硬约束；
  - 降级评测报告给出第 1、3、4 级的对比，以及首次建索引的耗时和单次查询的 P95 延迟。

### M2 商品卡

#### F1 重做商品卡

- **依赖**：T3
- **改动范围**：DESIGN.md 第 4 章表格中列出的文件，以及它们的测试。
- **不改**：页面布局、导航、对话区、其他页面、CSS 体系、React 版本。
- **完成标准**：见 DESIGN.md 第 4 章。截图存到 `eval/verification/<日期>-product-cards/`。

### M3 压测底座与基线

#### L1 假模型服务

- **依赖**：T3
- **改动范围**：`scripts/loadtest/mock_llm.py`（用 FastAPI 实现）及其测试。
- **步骤**：
  1. 实现两个接口：`/v1/chat/completions` 和 `/v1/embeddings`。
  2. 支持流式输出，以及工具调用的增量输出；回答轨迹和默认参数见第 1 节。
  3. 以下参数可配置：延迟、回答长度、429 的注入比例。
  4. 嵌入向量以文本的哈希值为种子生成。
- **完成标准**：
  - 用 OpenAI 官方 SDK 测试流式输出和工具调用，全部通过；
  - 把应用的 `LLM_BASE_URL`、`EMBEDDING_BASE_URL` 指向假模型后，在页面上完整跑通一轮"检索 → 回答"。

#### L2 压测脚本与改造前基线

- **依赖**：L1
- **改动范围**：
  - `scripts/loadtest/locustfile.py`：新写。旧的 `scripts/locustfile.py` 和 `scripts/loadtest.py` 保留，它们压的是旧入口；
  - `scripts/loadtest/analyze.py`；
  - `eval/verification/<日期>-loadtest-baseline/`。
- **步骤**：
  1. 按 DESIGN.md 5.5 节实现虚拟用户的行为和断线重连校验。
  2. 实现热点库存场景：先为同一个 SKU 生成 500 张确认单，再并发批准。
  3. 在当前版本上使用默认配置（单进程、进程内执行），逐级加压，直到吞吐不再增长。
- **完成标准**：
  - 基线报告包含：饱和并发数；首事件、首 token 的 P50/P95/P99；错误率；热点 SKU 的结果；机器配置；
  - 原始 CSV 入库。

### M4 框架内并发改造

#### C1 推送路径优化

- **依赖**：L2（必须先测完基线）
- **改动范围**：`ag_ui_journal.py`、`agent_runtime.py`、`ag_ui.py`；新增一个进程内共享的信号订阅模块。
- **要点**：按 DESIGN.md 5.2 节实现，所有新行为都由开关和"Redis 是否可用"决定。
- **测试**：
  - 读事件时，并发写入不被阻塞；
  - 过期运行在"租约 + 5 秒"之内被标为中断；
  - 开启增量合并后，客户端拼出的文本与合并前逐字一致；
  - 信号丢失或 Redis 不可用时，事件仍能送达；
  - 不配置 Redis、且合并开关为 0 时，现有测试原样通过。
- **完成标准**：以上测试全部通过；在基线负载下，SSE 轮询不再产生写锁忙等（附写锁计数对比）。

#### C2 AG-UI 运行可选入队

- **依赖**：C1
- **改动范围**：`ag_ui.py`、`app/worker.py`、`redis_stream_queue.py`（新增任务类型）、`settings.py`。
- **要点**：按 DESIGN.md 5.3 节实现入队、等待运行出现、worker 执行、背压 503、入队失败退回进程内执行并记降级事件。
- **测试**：用现有测试基础设施启动真实的 `redis-server`，覆盖以下场景：
  - 入队模式下端到端跑通；
  - 按指纹去重；
  - worker 崩溃后重投递，不会重复执行；
  - 待处理任务数超过上限时返回 503；
  - 入队失败时退回进程内执行；
  - 跨进程取消；
  - 开关关闭时，现有测试原样通过。
- **完成标准**：以上测试全部通过；在 `docker-compose.scale.yaml` 下以入队模式完成一次完整选购。

#### C3 多进程正确性与部署

- **依赖**：L2
- **改动范围**：
  - `assertions.py`、`harness_middleware.py`、会话存储（新建表 `session_tool_history`）；
  - 上下文整理的并发测试；
  - `docker/docker-compose.scale.yaml`。
- **要点**：按 DESIGN.md 5.4 节实现。
- **测试**：
  - 同一会话的两轮落在两个进程上，顺序断言的结果正确；
  - 两个进程同时请求整理上下文，只执行一次。
- **完成标准**：测试通过；按 scale 配置启动后，经 Nginx 完成一次完整选购。

### M5 压测与演练

#### R1 容量矩阵与前后对比

- **依赖**：C2、C3
- **步骤**：按 DESIGN.md 5.5 节跑完 API × worker 矩阵，并对比两种执行模式。
- **完成标准**：
  - 报告包含：每个档位的饱和点、P50/P95/P99、扩展曲线和拐点的成因；
  - 与 L2 基线做前后对比。

#### R2 故障演练与收尾

- **依赖**：R1
- **步骤**：
  1. 按 DESIGN.md 5.5 节跑完 6 项故障演练，每项保存日志和结论。
  2. 在 `README.md` 补充以下章节：ESCI 数据与评测、本地向量兜底、入队模式、单机多进程部署。原有章节保留。
- **完成标准**：
  - 正确性指标全部满足；
  - 按 README 的新章节，能在一台干净的机器上启动多进程部署。

## 5. 交给 agent 的提示词模板

```
你负责 Voyacart 改造的任务卡 <编号>。
开工前先读：AGENTS.md、docs/DESIGN.md、docs/PLAN.md 第 0、1 节和任务卡 <编号>。
只改任务卡写明的范围；DESIGN.md 2.5 节的降级兜底不能删减。遇到任务卡没覆盖的决定，停下来说明选项和取舍。
完成后创建分支 feat/<编号>-<名称>，向 main 发 PR。PR 正文逐条对照完成标准，写明实际结果和证据路径。
```
