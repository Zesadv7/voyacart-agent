# T1 ESCI 查询预分类

## 当前状态

用户已确认本轮使用 2,000 条预分类候选，开发/留出/干扰查询为 100/300/100，中文派生集维持 200。PLAN.md 与代码参数已同步，并发保持 2。[151101 试运行](../../../eval/verification/2026-10-14-151101-esci-t1-pilot-2000/manifest.json)在单次请求超时后停止，本轮候选中已有 462 条成功缓存。相同请求复核通过；已补上有上限的超时重试，专项测试 8 项通过。[154100 续跑](../../../eval/verification/2026-10-14-154100-esci-t1-pilot-2000-retry/manifest.json)正在后台运行。最近一轮 Harness 的 847 项契约通过，真实模型 11/12 场景通过，1 项超时，完整验收仍待复测。

本聊天已创建每 10 分钟检查一次的后台跟进 `voyacart-t1`。完成分类后执行缓存复跑、覆盖率验收和 T1 PR 交付；服务失败或 PR 合并等用户检查点保持人工决定。以下保留各阶段的真实失败、用户决定和复测记录。

## 需求与版本

按 PLAN.md 第 0、1 节与 T1 任务卡准备真实 ESCI 数据及查询预分类。
基线提交为 `7f29c08`，工作分支为 `feat/T1-esci-query-classification`。本阶段保存前置检查证据。

## 具体变更

建立本次证据目录、功能记录和日期索引。仓库 AGENTS.md 引用的改动记录规则文件在起点提交中缺失，本次按 AGENTS.md 已列明的字段建立记录。

## 验证与证据

- 仓库检查：本地 main 与 origin/main 同步，工作区初始干净。
- 配置检查：项目 `.env` 缺失；进程环境中的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 均为空。
- 依赖探测：`uv run --no-sync python` 创建本地虚拟环境后，导入 dotenv 失败。项目依赖安装待执行。
- 原始结果：[preflight.json](../../../eval/verification/2026-10-14-esci-t1-preflight-132223/preflight.json)。
- 探测日志：[preflight.log](../../../eval/verification/2026-10-14-esci-t1-preflight-132223/preflight.log)。
- 源码内容指纹：保存在上述 JSON 中，算法为 BLAKE2b。
- 下载、预分类、缓存复跑、分布统计、覆盖率验收和 Harness：未执行；模型调用为 0。

## 检查点与后续

用户任务要求缺少凭据时停止。等待用户在本机填写 `.env` 中的三项模型配置，然后安装依赖并执行 T1。
`LLM_MAX_CONCURRENCY` 可由用户配置，项目现有默认值为 2。

## 启用与回滚

本阶段仅增加证据和文档；应用配置与运行时保持基线状态。`.env` 由用户维护。
PR 尚未创建，T1 实现尚未开始。

## 配置恢复与实现阶段

用户随后明确授权使用 Kimi Code 的供应商 1 配置本项目模型。已从本机配置读取 `gemini-3.8-flash-high` 条目，将模型地址、密钥和模型名写入本机 `.env`，文件权限为 0600，Git 忽略规则生效。这项最新授权覆盖初始任务中由用户填写 `.env` 的分工；凭据始终留在本机。

- 项目依赖安装完成。ESCI 使用可选依赖组 `esci`，锁定 pyarrow 23.0.1。
- 首次连接返回 HTTP 200，输出 JSON 解析失败：[连接记录](../../../eval/verification/2026-10-14-esci-t1-model-config-134309/connection.json)。
- 增加明确的 JSON 输出约束后，模型返回有效类目：[原始请求](../../../eval/verification/2026-10-14-esci-t1-json-probe-134411/request.json)、[原始响应](../../../eval/verification/2026-10-14-esci-t1-json-probe-134411/response.json)。
- 两个原始文件完成下载与上游内容指纹校验：[下载日志](../../../eval/verification/2026-10-14-esci-t1-download-134630/run.log)、[数据清单](../../../eval/verification/2026-10-14-esci-t1-download-134630/source.json)。原始 parquet 保存在 Git 忽略的 `data/raw/esci/`。
- 新增 `scripts/esci/download.py` 与 `scripts/esci/prepare.py`。通过 `--download` 和 `--classify` 显式启用一次性数据准备，按固定种子抽样，逐条分类并保存成功或失败缓存，复跑复用已有结果。网络或 HTTP 错误保存证据后结束运行。
- 缓存键为 `(locale, query_id, query-class-v1)`；并发上限读取 `LLM_MAX_CONCURRENCY`，同时沿用现有请求间隔配置。
- 专项测试 6 项通过：[测试输出](../../../eval/verification/2026-10-14-esci-t1-unit-135049/pytest.log)。
- 实际候选池 12,000 条；train、test 各 6,000 条，每个划分 us/es/jp 分别为 3,000/1,500/1,500。

## 执行口径

T1 的候选池按任务卡要求先按站点和原始划分抽样，再做预分类；PLAN.md 第 1 节的类目分层用于取得类目后的 T2 定稿抽样。覆盖率报告同时计入 train 的 300 条开发查询与 400 条干扰查询需求，test 按 900 条留出查询核验 1.3 倍余量。

## 启用命令

`uv run --extra esci python -m scripts.esci.prepare --download --classify --output eval/verification/<新的运行目录>`

两个动作由命令行开关控制。停止调用数据准备命令即可保持现有应用路径。

完整预分类、零调用复跑、完整回归与 Harness 的结果待实际执行后补充。

### 全量回归首次失败

首次以空模型凭据执行完整 pytest，`server` 模块在测试收集阶段触发必填凭据校验，退出码为 2；应用源码保持原状。随后使用测试专用凭据与测试地址复测。原始日志：[首次回归](../../../eval/verification/2026-10-14-esci-t1-regression-1351/pytest.log)。

### Harness 验收完成

`make eval-harness` 完成：847 项安全契约通过，真实模型 6 个场景各运行 current/candidate 两种策略，共 12 次全部通过，`verify` 返回通过。模型为 `gemini-3.8-flash-high`。证据：[运行日志](../../../eval/verification/2026-10-14-esci-t1-harness-1350/run.log)、[交互报告](../../../eval/verification/2026-10-14-esci-t1-harness-1350/report.html)。

### 完整后端回归完成

使用测试专用凭据与测试地址运行 pytest：1940 项通过，1 项跳过，耗时 84.18 秒。日志与跳过原因均保存在[回归证据目录](../../../eval/verification/2026-10-14-esci-t1-regression-1354/manifest.json)。

### 用户授权提高并发

用户选择将并发上限提高至 8。已修改本机 `LLM_MAX_CONCURRENCY=8`，请求起点间隔沿用 1 秒。原预分类进程主动中断，完整保留已完成的缓存；在途请求的实际消耗记为未知。续跑使用新的证据目录。原始记录：[中断记录](../../../eval/verification/2026-10-14-esci-t1-classify-1356/interruption.json)、[运行日志](../../../eval/verification/2026-10-14-esci-t1-classify-1356/run.log)。

### 并发 8 的模型请求超时检查点

本轮出现一次 `ReadTimeout`，单次请求超时设置为 120 秒；程序按模型服务故障检查点结束，退出码为 1。累计缓存 645 条，缓存中分类格式失败为 0 条。本轮计数为 {"cache_hits": 276, "candidate_count": 12000, "completed_calls": 369, "failed_outputs": 0, "model_calls": 378}。未完成请求的实际消耗未知，已保存的缓存可以续用。

证据：[检查点](../../../eval/verification/2026-10-14-141010-esci-t1-classify-c8/checkpoint.json)、[原始失败](../../../eval/verification/2026-10-14-141010-esci-t1-classify-c8/failures.jsonl)、[完整运行日志](../../../eval/verification/2026-10-14-141010-esci-t1-classify-c8/run.log)。等待用户决定续跑方式；T1 的完整分布和零调用复跑验收仍待完成。

### 用户确认并发 2 续跑

用户明确选择降到并发 2 从缓存续跑。已确认原进程退出，将本机 `LLM_MAX_CONCURRENCY` 设为 2，保留 1 秒请求起点间隔，使用新的证据目录继续预分类。开始时已有 645 条成功缓存，模型只处理尚缺的查询。

续跑证据：[运行清单](../../../eval/verification/2026-10-14-144757-esci-t1-classify-c2/manifest.json)。进程运行期间逐请求落盘；全量完成后验证 12,000 条分类缓存与零调用复跑。

### 用户调整为 2,000 条候选试运行

用户要求先做 2,000 条数据试运行，然后继续其他任务。本轮将该要求落实到正在执行的 T1 查询预分类：train/test 各 1,000 条，两个划分内 us/es/jp 各 500/250/250 条。PLAN.md 第 1 节及 T1 任务卡已先同步参数，再新增命令行 `--queries-per-split`；默认 1,000，显式传 6,000 可恢复原完整规模。

旧规模运行主动中断，退出码为 130；保存 946 条成功缓存。固定种子的新候选池与已有缓存重合 162 条，其余缓存保留以备后续扩量。原始中断证据：[中断记录](../../../eval/verification/2026-10-14-144757-esci-t1-classify-c2/interruption.json)。

缩小规模后重新验证抽样比例、固定种子、子集包含关系与非法数量输入，6 项专项测试通过：[测试输出](../../../eval/verification/2026-10-14-esci-t1-pilot-tests-1500/pytest.log)、[抽样结果与源码指纹](../../../eval/verification/2026-10-14-esci-t1-pilot-tests-1500/selection.json)。

本轮分类证据：[运行清单](../../../eval/verification/2026-10-14-150526-esci-t1-pilot-2000/manifest.json)。并发为 2，请求起点间隔为 1 秒。完整分布、失败样本和零调用复跑仍以本轮完成时的结果为准。

2,000 条候选池中 test 只有 1,000 条，原留出目标 900 条的 1.3 倍要求为 1,170 条；此项余量在当前规模下达不到。已集中询问用户是否将试运行的开发/留出/干扰规模设为 100/300/100、中文派生集维持 200，等待其决定。完整规模的验收状态分别记录。

### 用户确认试运行评测规模

用户选择开发 100、留出 300、干扰 100，中文派生集维持 200。PLAN.md 第 1 节和 T1 报告计算均已更新。train 的 1.3 倍余量要求按开发与干扰合计 200 条计算，test 按 300 条留出查询计算；分站点取上整后依次为 us train/test 130/195、es 65/98、jp 65/98。

6 项专项测试通过，新增断言检查上述余量计算：[本轮测试证据](../../../eval/verification/2026-10-14-esci-t1-confirmed-pilot-tests/manifest.json)。参数冻结前的运行主动中断并保存原始记录，随后采用新目录继续处理尚缺的查询。

与初始目标的差异由用户最新要求覆盖：候选总数从 12,000 改为 2,000；开发/留出/干扰从 300/900/400 改为 100/300/100。原约 4 万件商品属于完整规模估计，本轮 T2 按实际构建结果报告商品数。其余任务目标、证据标准和人工检查点继续执行。

### 单次超时复核与有限重试

151101 运行在 es/train 查询 93174 上出现 120 秒 `ReadTimeout`，退出码 1。本轮开始复用 239 条缓存，新增 223 条成功缓存，合计覆盖 462/2,000 条候选；已发起 226 次请求，未完成请求的实际消耗未知。缓存中格式失败为 0。证据：[检查点](../../../eval/verification/2026-10-14-151101-esci-t1-pilot-2000/checkpoint.json)、[失败记录](../../../eval/verification/2026-10-14-151101-esci-t1-pilot-2000/failures.jsonl)、[运行日志](../../../eval/verification/2026-10-14-151101-esci-t1-pilot-2000/run.log)。

模型列表接口返回 HTTP 200；随后对同一查询单独调用完成接口，4.325 秒返回 HTTP 200，类目为数码配件。[复核请求与结果](../../../eval/verification/2026-10-14-153152-esci-timeout-diagnostic/result.json)独立保留；这次复核产生一次额外模型调用。此证据支持请求超时后服务已恢复可调用。

分类脚本读取已有 `LLM_MAX_RETRIES` 配置（本机为 2），仅对请求超时执行最多两次重试，间隔 6、18 秒；每次尝试计入调用数，并保存失败类型、耗时、尝试序号和重试决定。HTTP 服务错误与其他连接错误保持立即停止；超时重试耗尽后停止。固定数量的工作协程保证每条查询的重试及时执行，每次请求继续遵守原并发和起点间隔约束。已成功缓存的分类继续复用。

专项测试覆盖超时恢复、三次均超时后的终止、失败证据与调用计数，以及原有抽样和缓存行为，8 项通过：[测试证据](../../../eval/verification/2026-10-14-153457-esci-t1-timeout-tests/manifest.json)。

### 候选数量解释

原完整规模需要选出 300+900+400=1,600 条查询，整体 1.3 倍余量约需 2,080 条合格候选；其中 test 单独需要 900×1.3=1,170 条，超出本轮 test 的 1,000 条。按各站点向上取整后的门槛会略高。

本轮已确认只选 100+300+100=500 条，按站点分别取整后的总余量门槛为 651 条。2,000 条原始候选具有数量空间；是否满足最终余量，待各站点和划分中属于 8 个类目的数量统计完成后核验。中文派生 200 条复用留出查询。

### 超时处理修改后的 Harness 与分类续跑

`make eval-harness` 在 [153457 运行](../../../eval/verification/2026-10-14-153457-esci-t1-timeout-harness/report.html)中完成全部场景尝试，退出码为 2。847 项契约测试通过；真实模型共 12 个场景/策略组合，11 项通过，`h1-dev-cost/current` 因 `APITimeoutError` 失败。随后独立执行 `verify`，结果为未通过，原因为“场景缺失或失败”。[原始日志](../../../eval/verification/2026-10-14-153457-esci-t1-timeout-harness/run.log)、[验证结果](../../../eval/verification/2026-10-14-153457-esci-t1-timeout-harness/verify.json)均保留；此次失败与先前通过的运行分别记录。

已确认 Harness 进程退出后，于 15:41 使用新证据目录 [154100](../../../eval/verification/2026-10-14-154100-esci-t1-pilot-2000-retry/manifest.json)恢复分类，会话句柄为 88384，原始日志位于 `/tmp/2026-10-14-154100-esci-t1-pilot-2000-retry.log`，退出时复制到证据目录。启动时复用 462 条候选缓存，保持并发 2、请求起点间隔 1 秒、最多 2 次超时重试。后台跟进已同步准确句柄，当前活进程保持唯一。分类结束后执行零调用复跑与完整 Harness 复测，再判断 T1 是否达到 PR 交付条件。

### 2,000 条预分类完成与零调用复跑

运行 88384 已确认退出码 0，原始 manifest 为 completed、source_stable=true。候选 2,000 条中成功 1,994 条，格式失败 6 条保留失败状态；本轮复用 462 条缓存，新增 1,538 次调用。候选中六个站点/划分组合均满足当前试运行的 1.3 倍余量要求，缺口为 0。

[零调用复跑](../../../eval/verification/2026-10-14-t1-cache-replay-complete/verification.json)确认缓存命中 2,000 条、模型调用 0、缓存内容逐字节保持一致、退出码 0。失败分类也作为缓存保留，未改成推测类目。T1 的试运行数据与缓存验收完成；原完整规模由用户确认的 2,000 条试运行覆盖。

后台索引任务也已停止，T1 原生跟进已暂停，避免任务完成后重复唤醒。此次暂停只针对该后台跟进，整个改造 goal 继续进行。
