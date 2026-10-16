# GLM-5.3-Flash 真实评测准备

## 需求与边界

- 使用用户指定的智谱 BigModel `glm-5.3-flash` 做真实评测；模型费用已获批准，但不自动购买套餐、不部署或启用生产策略。
- 凭据由仓库所有者在 GitHub 官方 Repository secrets 页面自行录入。工程与记录只保存配置名，不保存、读取或转发密钥值。
- 此提交只完成适配与离线 MockHTTP 验证；真实模型调用、账号额度、实际计费、服务协议兼容性及业务质量均未验证。

## 官方配置核验（2026-10-16）

- [模型文档](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)：准确模型 ID `glm-5.3-flash`，1M 上下文、128K 最大输出，支持工具调用，思考只能开启。
- [OpenAI 兼容接口](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)：按量 Chat Completion base URL 为 `https://open.bigmodel.cn/api/paas/v4`。不使用 Coding Plan 专用端点。
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
