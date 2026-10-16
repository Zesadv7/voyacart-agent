# GLM-5.3-Flash 真实评测准备

## 需求与边界

- 使用用户指定的智谱 BigModel `glm-5.3-flash` 做真实评测；模型费用已获批准，但不自动购买套餐、不部署或启用生产策略。
- 凭据由仓库所有者在 GitHub 官方 Repository secrets 页面自行录入。工程与记录只保存配置名，不保存、读取或转发密钥值。
- 此提交只完成适配与离线 MockHTTP 验证；真实模型调用、账号额度、实际计费、服务协议兼容性及业务质量均未验证。

## 官方配置核验（2026-10-16）

- [模型文档](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)：准确模型 ID `glm-5.3-flash`，1M 上下文、128K 最大输出，支持工具调用，思考只能开启。
- [OpenAI 兼容接口](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)：按量 Chat Completion base URL 为 `https://open.bigmodel.cn/api/paas/v4`。最初准备阶段仅选择标准接口；后续用户选择 Coding Plan 的独立配置见下节。
- [思考模式](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)：交错工具调用必须传回 `reasoning_content`；启用保留式思考后应保留其顺序与原文。
- [价格](https://docs.bigmodel.cn/cn/guide/start/pricing)：标准输入/缓存输入/输出分别为人民币 0.8 / 0.23 / 2.8 元每百万 tokens。主价格页另列限时五折，截止日未知；预算参考标准价，不能冒充已结算账单。
- 官方兼容指南与 API schema 对 temperature=0 的说明不一致。本套件明确采用模型页推荐的 1，固定 top_p=0.95、reasoning_effort=max、thinking.enabled、clear_thinking=false，不依赖默认值。

## 具体变更

- `scripts/eval/harness/model_config.py`：仅隔离评测使用的显式模型/网关合同；将 SDK 的 max_completion_tokens 映射为官方 max_tokens，保留 reasoning_content，流式启用 tool_stream。其它模型的请求参数不变。
- `eval/harness/v1/glm-5.3-flash-suite.json`：复用原有冻结数据与 current/candidate，输出上限 8192、请求超时 120 秒、单场景超时 900 秒、并发 1、无瞬时重试/备用模型。为 GLM 固定计数上限，未改原套件运行参数与门禁。
- `scripts/model_preflight.py`：提供独立预检，非流式/流式分别检查 none、auto、指定函数，并以固定合成结果验证工具续答；最多 8 次物理模型请求，不执行业务工具。
- Harness context/agent/cache replay 共享冻结配置及物理请求计数；请求上限用尽后保留部分证据并停止，不重新跑整个套件。
- `.github/workflows/harness-eval.yml`：去掉缺失变量时隐式使用 Qwen 的默认值；增加独立 preflight 档。GLM 的完整评测前先预检。push/PR 不调用真实模型，仍要求 workflow_dispatch 和 approve_model_budget。

## 安全配置与执行顺序

1. Settings → Secrets and variables → Actions → Repository secrets：`HARNESS_LLM_API_KEY`、`HARNESS_LLM_BASE_URL`；Repository variable：`HARNESS_LLM_MODEL=glm-5.3-flash`。
2. workflow_dispatch 文件须先出现在默认分支；之后在 Actions 选择实际冻结的工作分支。不得以持续付费的 push 触发替代预算确认。
3. 先选择 preflight 并确认预算，只执行最多 8 个请求。失败时保留报告；遇到模型身份、工具协议或配置问题停止，不替换模型。
4. 预检通过后运行 smoke：6 场景 × 2 策略 × 1 次 = 12 个场景实例；物理调用上限 150。
5. 若需调参，仅用 dev：15 场景 × 2 策略 × 2 次 = 60 个实例；上限 600。修改参数后重新冻结，重跑完整 smoke。
6. 一次冻结留出 release：33 场景 × 2 策略 × 3 次 = 198 个实例；上限 3000。数量是场景实例，不是声称固定 API 请求数；工具、摘要和恢复均计入物理请求账本。
7. 原门禁仍检查质量、安全、完整用量、延迟与成本收益。金额或供应商计费字段缺失保持未知，不得为了绿色 CI 降低门槛；release 通过也不自动部署。

## 验证与原始证据

- 首次定向测试：116 通过、26 失败；失败来自宿主 SOCKS 代理配置但测试环境缺 socksio，尚未进入 MockHTTP 请求。保留 `eval/verification/2026-10-16-glm-adapter-offline/focused.log` 与 XML。
- 只移除测试子进程的代理变量后，142 项定向测试全部通过；加入 CI 配置契约后 161 项通过。记录位于同目录 `focused-no-proxy.*`、`final-focused.*`；增加独立预检档断言后最终 162 项通过（6.99 秒），见该目录 final-checked 日志。
- 实际 SDK/MockTransport 检查参数序列化、stream/nonstream reasoning 工具回传、8 请求预检上限、计数耗尽停止、错误模型/网关失败关闭、其它 provider 原有默认不变。
- 已有公开输出边界测试覆盖思考流、旧日志及历史回放。隔离 trace 会保留合成消息中的 reasoning_content，不能把它当作公共脱敏报告；生产 context_archive 回查可能把思考转为普通工具文本属于既有的另项风险，本提交未改变生产治理。
- 本机已无先前 Redis 二进制，未重装；完整 Redis 契约留给配置了官方 redis-server 的 GitHub Actions。离线定向通过不替代完整 CI 或真实模型预检。
- 已运行已知格式秘密扫描，0 findings；它不能证明所有任意格式秘密均不存在。

## 启用与回滚

仅在专用 suite 与显式 GLM 模型配置下生效，不改生产 `.env`、模型目录或默认模型。回滚此提交即可恢复旧评测实现；必须保留已生成的失败/成功报告，不覆盖旧证据。关联版本为包含本记录的 Git 提交；远端公开提交 SHA 在发布后由 Actions 记录。

## 后续：显式端点选择与快速诊断

- 用户明确选择 Coding Plan。按[官方接入地址](https://docs.bigmodel.cn/cn/coding-plan/tool/others)新增独立冻结套件 `glm-5.3-flash-coding-plan-suite.json`，端点为 `https://open.bigmodel.cn/api/coding/paas/v4`；模型、数据、功能参数和请求上限与标准套件一致，端点与实验指纹分别记录，不混用两类结果。客户端原生直连，不伪装工具身份、不添加伪装 headers、不代理转发，服务端拒绝即停止。
- 工作流新增 `endpoint_mode=standard/coding_plan` 显式选择；所选类型与用户注入的 URL 必须匹配，不覆盖 Secret、不自动回退标准地址。Coding Plan 不套用按量价格，实际 token、积分和金额缺失均保持未知；完整成本收益门禁未放宽。
- 新默认档 `config-check` 仅以标准库检查配置分类、模型是否匹配和 key 是否存在，不导入应用 `.env`、不拿 key 值、不创建模型客户端，也不等待安全契约。输出只包含固定枚举及布尔值，不输出原 URL、userinfo、query 或凭据。其它 provider 的既有评测参数不变。
- 功能预检仍最多 8 个请求、无业务工具执行、无重试和备用模型；模型身份与工具协议必须匹配。`usage_known=false` 不再直接使功能预检失败，`billing_verified=false` 始终明确；功能通过不等于成本收益通过。
- 原失败保留：[标准端点运行 38866364091](https://github.com/Zesadv7/voyacart-agent/actions/runs/38866364091) 实际尝试 1 次，返回 1113，金额未知；[新配置运行 38868744231](https://github.com/Zesadv7/voyacart-agent/actions/runs/38868744231) 在旧套件端点校验处停止，0 个模型请求。两次原始证据分别位于 `eval/verification/2026-10-16-glm-preflight-38866364091/`、`eval/verification/2026-10-16-glm-preflight-38868744231/`，不能用旧结果判断新 Secret。
- 当前补丁本地定向验证：首轮176 passed / 7.40 秒；补充供应商拒绝后的跨场景停止和错误码脱敏后，177 passed / 8.11 秒。原始日志/XML 位于 `eval/verification/2026-10-16-glm-endpoint-config/focused.*`、`final.*`。覆盖无网络分类/脱敏、显式双套件、未知 usage 的功能通过、401/403/429 单次停止、8 请求上限、其它 provider 不变。完整新 CI 与真实 Coding Plan 预检尚待执行，不宣称已通过。
- 启用：先选 `config-check` 与实际 `endpoint_mode`，无需模型预算批准；需要功能验证时新建 `preflight`，选择相同端点模式并批准预算。完整 smoke/release 必须另行显式启动。回滚本阶段补丁会恢复仅标准套件；不得删除已保存失败。


## Coding Plan 首次真实响应与 none 工具适配

- [运行 38872973913](https://github.com/Zesadv7/voyacart-agent/actions/runs/38872973913) 于 08:19:59 UTC 从 `370454a347ac5f24ed7a3b6bcfbf722a9f9ef24c` 启动；配置诊断确认 `endpoint_mode=coding_plan`、实际分类 coding_plan、`profile=preflight`，离线契约通过。
- 首个非流式请求实际返回 `glm-5.3-flash`：输入178、输出47 tokens，约16.45秒。传入 `tool_choice=none` 且存在工具定义时，响应仍含工具调用，协议保护以 `tools_forbidden` 拦截；物理请求1/8，实际工具执行0，后续请求未发起。费用和套餐扣量未知，功能预检未通过。原始 ZIP/JSON 保留于 `eval/verification/2026-10-16-glm-preflight-38872973913/`；ZIP SHA256 为 `64dd54f28c0e785feb782853d9c3e874f1548cab5e771a8f839e2e07ba9a825b`。
- 最小修复仅修改 GLM 评测 HTTP 适配：禁止工具时不发送 tools 和 tool_choice，避免向模型提供可调用定义。协议保护不放宽：实际请求的 allowed 集合为空，任何意外 tool_calls 仍在返回 AgentScope 前失败。auto、指定函数、思考历史和其它 provider 保持原样，不追加重试或替换模型。
- 两套 GLM 合同明确升级 adapter/experiment v2，冻结 `tool_choice_none=omit_tools`，源码指纹与旧失败分开。未来若复用于缓存专项中通过 none 禁止工具的 tools-schema 变化场景，适配后不再向供应商发送 schema，因此不能解释为工具前缀变化/缓存失效验证；当前 GLM 套件 cases.json 不含该专项场景，本次不声称缓存收益已验收。
- 验证范围为 GLM 实际 SDK MockTransport 的流式/非流式、正常文本及恶意意外工具响应、其它 provider 保持原 none 参数、八请求工具往返和既有协议/公开输出/CI安全契约。新真实预检须由用户启动新运行；不重跑旧失败，不以离线通过声称真实预检通过。
- 本地定向结果：132 passed / 5.34 秒；原始日志/XML 为 `eval/verification/2026-10-16-glm-none-tools/focused.*`，独立静态审查未发现权限放宽。完整后台 CI 和新真实预检尚待执行。
- 启用仍选 main / preflight / coding_plan 并批准预算。回滚本段相关提交恢复 v1 none 请求形状；保留本次真实失败证据，且禁止将旧失败当 v2 结果。


## smoke 启动前补齐独立 job 的 Redis 依赖

- v2 [真实预检 38874052873](https://github.com/Zesadv7/voyacart-agent/actions/runs/38874052873) 已通过：8次物理请求、流/非流共6种工具选择模式、两次合成工具结果续答；实际业务工具执行0，累计1034输入/713输出tokens，费用与套餐扣量未知。证据保留于 `eval/verification/2026-10-16-glm-preflight-38874052873/`。
- smoke 启动前发现既有 workflow 缺陷：runner 在模型 job 内再次执行完整契约，该 job 没有 redis-server；前置安全 job 的安装不能跨机器共享，队列可靠性测试会跳过，导致 `contract_checks_failed`。此前成功的 preflight 没有运行这些内部契约，不能证明 smoke 基础设施就绪。
- 仅为 `profile != preflight` 的模型 job 补官方 Ubuntu 包 redis-server 安装，并置于任何模型请求之前；安装失败即停，不降低 skipped==0、不伪造复用契约。暂保留重复契约，入口去重另行优化，不把功能修复扩成新框架。
- 工作流/GLM/CI环境定向测试56 passed / 4.02秒，日志/XML见 `eval/verification/2026-10-16-glm-smoke-redis/focused.*`；覆盖安装先于 preflight/完整评测、preflight 不安装、预算批准和前置契约保留、禁止跳过内部契约。不会触发模型或改用户配置；新版本完整后台CI与smoke待执行。
- 启用：main / smoke / coding_plan / 批准预算；6场景×2策略×1次为12实例，smoke本体最多150请求，前置preflight另8次，一次dispatch合计最多158。仍不自动release。回滚此修复会恢复模型job缺Redis的风险。


## 同进程离线契约隔离真实模型环境

- [旧版本 smoke 38875399271](https://github.com/Zesadv7/voyacart-agent/actions/runs/38875399271) 在 Redis 修复发布前按 `9f86cb3` 启动，08:48:21 UTC 失败结束。前置预检8次通过；smoke本体模型请求0，内层契约1483项中1381通过、99跳过、3失败。99跳过来自缺Redis；3失败来自测试继承 `LLM_MODEL=glm-5.3-flash` 后误用默认假模型套件，触发显式配置校验。不能将整个失败归因于API或余额。原始产物保留在 `eval/verification/2026-10-16-glm-smoke-38875399271/`。
- `contract_checks` 复用已有 `scripts.ci.offline_env.isolated_env`，对子进程启用临时数据目录、假模型、回环不可用地址、占位密钥与禁用.env，并固定隔离Redis测试使用回环TCP；不继承真实供应商凭据、代理、追踪和模型设置。父进程os.environ不改变，真实评测仍按用户冻结的模型/网关运行。
- 修复前用合成GLM环境复现相同3个失败，保留 `eval/verification/2026-10-16-glm-contract-env/reproduced.*`；修复后定向121项通过，含真实子进程执行54项runner测试、父环境不变和凭据canary不出现在日志。日志/XML保留在同目录 focused/final 文件；这不等于真实smoke通过。
- 此阶段没有模型调用、没有新模型参数或放宽契约。与已发布的Redis安装修复配套，仍要求完整零跳过安全契约及独立真实smoke；不导入或伪造旧job结果。

- 完整同Coding Plan套件离线验证：1484 passed，0 failures / 0 errors / 0 skipped，121.84秒；使用官方Redis7.4.11临时编译版，父配置保持不变，详见 `eval/verification/2026-10-16-glm-contract-env/full-suite/verification.json`、contracts.log/xml。不使用真实API凭据，未执行模型；新的 `scripts/ci/offline_env.py` 依赖也纳入源码指纹、源码归档及Harness影响判定。


## 一次手动启动完整功能矩阵（full-functional）

- 需求：用户明确继续 Coding Plan，先不计算 token/金额收益，希望一次启动完成全部功能验证。已有 [smoke 38877836503](https://github.com/Zesadv7/voyacart-agent/actions/runs/38877836503) 在 `92724db` 上12/12通过，smoke29次+预检8次=37个真实请求；内部1484安全合同0失败/0跳过。本次不重复smoke、不声称正式收益发布通过。
- 新增独立 `full-functional` 工作流选项：先执行一次8请求预检，再完整 dev（15场景×2策略×2次=60实例，上限600请求），成功且证据校验通过后由独立job完整执行留出 release 数据（33场景×2策略×3次=198实例，上限3000请求）。合计48独立场景、258实例，单次工作流尝试物理调用硬上限3608；不并行、不自动重跑，供应商拒绝/预算耗尽/阶段失败即停止后续。
- 两个模型job均安装Redis，内部完整契约使用隔离子进程；不存在跳过或伪造前置契约。后续job只下载当前run与attempt固定名称产物，并在任何模型调用前核对GitHub SHA、源码指纹/归档、冻结suite/数据/模型/网关哈希、dev矩阵与请求上限。preflight补充同样的非凭据指纹；原始URL或key不进入阶段汇总。
- 功能通过要求完整矩阵、全部场景与安全合同通过、身份/工具协议无未知或违规、原有普通轮P95阈值不放宽。用量缺失或账单未知不伪造；最终独立汇总明确 `cost_benefit_status=not_evaluated`、`production_deploy_authorized=false`。原 `release` 档仍执行 `--require-benefit`，其发布收益门禁完全保留；full-functional使用留出数据不等于正式release收益验收。
- 付费手动工作流跨分支共用串行组；push/PR离线任务不再占其槽，零调用config-check也单独分组。保持原有预算勾选、显式端点、无fallback，不更改任何Secrets或权限。
- 规模：本次smoke12实例业务执行约6分08秒，线性外推258实例约2小时12分，仅用于说明量级；长上下文和供应商响应可能更慢，不能保证。dev和holdout各自180分钟超时；失败/取消的已调用用量仍保留。
- 启用：Actions新建运行，main / full-functional / coding_plan / 勾批准预算，只点击一次。报告分别保留第一阶段与最终全阶段artifact；不会部署或切换生产默认。回滚本阶段补丁恢复原分档入口，既有完整smoke与失败证据必须保留。
- 实装离线验收：阶段证据/配置/工作流/runner 联测166 passed（12.33秒）；同Coding Plan套件内部完整安全合同1538 passed、0失败/0错误/0跳过（122.52秒，官方Redis7.4.11，隔离子进程）。阶段校验53项覆盖未知token允许但未知身份/协议拒绝、跨SHA/网关/数据混用拒绝、预算和矩阵缺失拒绝、P95未达标拒绝、错误固定枚举不泄密。原始日志/XML与验证JSON保留于 `eval/verification/2026-10-16-full-functional/`。已跟踪文件及新增模块离线高置信度凭据扫描0发现；真实full-functional尚待用户一次手动启动，后台全量CI不作为此次入口发布等待条件。
