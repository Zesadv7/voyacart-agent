# 前端无锁测试跨 Node 隔离

## 需求、计划与根因

- 日期：2026-10-16（Asia/Shanghai）。在公开快照完整前端回归中发现既有失败；按授权诊断并修复可恢复的测试环境隔离问题。
- 原始失败发生于 `prepareRequests.test.ts:64`：测试名要求模拟“无锁”，却默认使用当前运行时的 `globalThis.navigator?.locks`。当前 Node v24.19.0 已提供 `navigator.locks`，因此程序正确返回无警告，而测试期望“不支持跨标签页”。
- 从基线 `01db556` 导出独立 frontend 后可复现同一失败；移除 Node 全局 navigator 的诊断运行全套通过。原始失败、基线、环境与诊断原样保留在[前序媒体清理记录](公开快照图片来源收口.md)。

## 实现

- 只修改 `frontend/tests/prepareRequests.test.ts`，不修改生产 `PrepareRequests` 或浏览器默认锁选择。
- 无锁用例显式模拟两种环境：没有 navigator、navigator 没有 locks；继续断言刷新请求键复用、双标签风险警告、持久存储不可写时内存降级警告及旧键隔离。
- 新增正向用例：navigator 提供锁时，默认构造必须调用该锁且不得误报跨标签限制。
- 每个模拟均在 `finally` 中恢复全局变量，避免泄漏到后续用例。
- 同步更正前序记录的相对证据链接，补充最终复测入口；前序失败证据不覆盖。

## 回归与证据

使用默认 Node24 环境，不设置禁用 navigator 的运行参数：

- `npm test -- tests/prepareRequests.test.ts`：12/12 通过。[targeted.log](../../../eval/verification/2026-10-16-frontend-lock-isolation/targeted.log)
- `npm test`：27 文件、428/428 通过。[frontend-full.log](../../../eval/verification/2026-10-16-frontend-lock-isolation/frontend-full.log)
- `npm run build`：通过。[frontend-build.log](../../../eval/verification/2026-10-16-frontend-lock-isolation/frontend-build.log)
- 源码指纹与生产锁模块未改动核对：[source-manifest.json](../../../eval/verification/2026-10-16-frontend-lock-isolation/source-manifest.json)

## 未完成项、外部链路与效果验收

- 本次既有测试失败已收口；未改变生产告警、锁行为或交易语义。
- 未进行真实外部平台交易或真实模型运行；测试通过不代表生产效果或外部服务可用性。
- 本次是前端测试环境隔离，不涉及 Harness 策略、Prompt 或工具合同，无需真实模型评测。

## 启用、回滚与关联版本

- 无新增配置或数据迁移；正常 `npm test` 即可。
- 若需回滚，仅 revert 本次测试与文档提交，生产逻辑不受影响；旧测试在 Node24 上的失败可能复现。
- 前序媒体实现：`ade44b0`；本修复与本记录同一提交，文件级指纹见上述证据。
