# ESCI 输入配对与十场景离线绑定

日期：2026-10-16 Asia/Shanghai（执行时间 2026-10-15 UTC）。
基线：`41666a3`。这是 M3 输入安全工作；不切换应用 demo 5,700 目录，不修改既有 v1/v2 冻结数据及 approved/frozen 历史。

## 需求与原始失败

旧 `run_esci` 默认 v2 catalog、v1 dataset、v2 baseline。单靠内部编号存在检查会接受错配：v1 开发 ranking 的 2,439 条标注引用均存在于 v2，但 2,435 条指向不同外部商品。没有执行错误组合的模型评测；只读复现旧 `validate_cases` 通过的事实。

证据：[原始错配复现](../../../eval/verification/2026-10-16-m3-esci-pairing/original-mismatch.json)。

## 实现

- `scripts/eval/esci_pairing.py`：共享 fail-closed 边界。正式/夹具角色区分、未知 schema 拒绝、正式清单自身固定指纹、catalog/selection/当档数据文件指纹、站点敏感外部身份及 SKU 映射指纹。
- `eval/esci/input-pairing.json`、`eval/esci-v2/input-pairing.json`：仅追加伴随清单，记录全部 dev/holdout/crosslingual 输入；开发验证不打开留出正文或计算留出 hash。制备清单时读取历史文件字节计算 hash，不进行留出样本分析、选策或重跑。
- `scripts/eval/run_esci.py`：修复默认 v2 配套，任何 output、设置读取、模型/索引客户端或 holdout-attempt 创建之前校验。开发报告与冻结参数纳入配对证明。合法运行后的失败仍保留一次留出 attempt；预检拒绝不消耗 attempt。
- `scripts/eval/harness/esci.py`、`eval/harness/esci-v1/build_suite.py`：使用同一 validator，保存配对/训练译文来源，默认绑定 v2。旧合同类型 `esci-dev-v1` 保持兼容，数据版本独立写 `esci-v2-dev`。
- `eval/harness/esci-v2/translations-train-zh-v1.jsonl`：四条独立 AI train 中文译文，附源 selection hash、AI 作者/生成方式、未暴露具体模型版本的事实、人工未审核。没有把 AI 译文标为人工完成。
- 新静态 preflight：不读凭据、不启动模型、不发送服务探测，明确 `suite_bound=true`、`runnable=false`、真实十场景未执行。

启用命令、配套 v1/v2 参数与安全回滚说明见 [ESCI v2 绑定说明](../../../eval/harness/esci-v2/README.md)。

## 分阶段验证

所有 pytest 使用现有 `.venv/bin/python` 与 `scripts.ci.offline_env.isolated_env`；不安装依赖、不继承真实凭据、不调用付费/外部服务、不 push。父任务中其他 M3 工作并行修改工程，以下是本功能聚焦回归，不能替代最终工程全量验收。

1. [旧代码基线](../../../eval/verification/2026-10-16-m3-esci-pairing/baseline-tests.log)：28 passed。
2. [首次候选](../../../eval/verification/2026-10-16-m3-esci-pairing/candidate-1-tests.log)：49 passed、2 failed。身份错配已正确拒绝，失败原因是新增断言正则未覆盖实际错误文案；完整原始失败保留。
3. [第二次复测](../../../eval/verification/2026-10-16-m3-esci-pairing/candidate-2-tests.log)：51 passed，补正式清单 pin。
4. [第三次复测](../../../eval/verification/2026-10-16-m3-esci-pairing/candidate-3-tests.log)：53 passed，补显式 fixture 防补签与资源预检合同。
5. [补充一次尝试合同复测](../../../eval/verification/2026-10-16-m3-esci-pairing/candidate-4-tests.log)：54 passed；增加“合法开始后失败仍消耗一次 attempt”及“approved 存在而 attempt 丢失仍拒绝”的回归。

6. [集成模板后的最终复测](../../../eval/verification/2026-10-16-m3-esci-pairing/candidate-5-integrated-tests.log)：54 passed；合同列表同步父任务新增 M3 测试并按顺序去重。

命令：`.venv/bin/python -m pytest -q -ra tests/test_esci_evaluation.py tests/test_esci_harness.py tests/test_esci_pairing.py`。

[集成模板后的正式 v2 离线绑定输出](../../../eval/verification/2026-10-16-m3-esci-pairing/bound-suite-integrated.log)：十场景已绑定，零模型调用，没有执行场景。[套件](../../../eval/verification/2026-10-16-m3-esci-pairing/bound-v2-integrated/suite.json)、[场景](../../../eval/verification/2026-10-16-m3-esci-pairing/bound-v2-integrated/cases.json)、[静态预检](../../../eval/verification/2026-10-16-m3-esci-pairing/bound-v2-integrated/preflight.json)。当前 fastembed 未安装、缓存不存在，远端配置未检查。

模板变更前的 `bound-v2` 仍作为阶段证据保留，当前应使用 `bound-v2-integrated`。

[历史冻结字节复核](../../../eval/verification/2026-10-16-m3-esci-pairing/historical-artifacts.json)：25 个原有目录/selection/评测/批准/准备文件与基线逐字节相同。[本功能源码指纹及最终命令](../../../eval/verification/2026-10-16-m3-esci-pairing/verification.json)。

[历史准备态复核](../../../eval/verification/2026-10-16-m3-esci-pairing/prepared-history.log)：`verified_preparation`、10 场景、`runnable=false`。保留原始准备历史，不把历史预选改写为 v2 正式绑定。

## 未完成与验收边界

- 没有真实模型前后基线，不能宣称质量、成本、延迟收益。
- 本功能只完成计划实现、离线配对/合同测试、来源绑定；人工 200/50 审核、真实十场景、Harness smoke/release 均未执行或仍阻塞。
- 最终 M3 全工程源码稳定后，父任务统一运行 Harness contracts/compare/verify、完整后端/前端/HTTP 回归。M2 accepted contracts 可作历史离线基线，不是当前源码或模型效果验收。
- 旧 v1/v2 检索指标仍是历史指标；不能因数据 hash 仍匹配就声称 M2/M3 最终源码与历史评测的源码指纹相同。
