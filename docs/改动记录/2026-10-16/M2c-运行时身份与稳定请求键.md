# M2c：运行时身份与稳定请求键

## 需求与版本

按 M2 设计去除浏览器构建共享凭据，为私人演示提供运行时签名身份入口；前端 prepare/create 与 prepare/cancel 在响应未知、重复点击及刷新后复用请求键。日期按 Asia/Shanghai 为 2026-10-16。基于 M1 离线基线继续实施；本文件所属 Git 提交对应本批实现，最终源码指纹见下方验收目录。未改依赖、锁文件、真实 `.env`、商品目录或后端实现。

## 实现

- `runtimeIdentity.ts`、`IdentityGate.tsx`：先读取公开 `/commerce/identity/config`。demo 模式仅采用服务器 `default_buyer_id`；hmac 模式通过 `/commerce/identity` 校验用户运行时输入的短期凭证，身份和有效期只采用服务器结果，不通过浏览器解码 `sub` 授权。
- 凭证仅保存在内存及当前标签页 `sessionStorage`；刷新先重新验证，退出或 401 清空。旧版 `localStorage` 共享凭据只执行删除，不读取或迁移。没有公开签发入口，没有创建新持久访问。
- 身份未验证前不挂载买家页面；401/过期卸载旧页面并显示重新登录入口。重新登录不自动重发未确认的写动作。买家页面中的历史、收藏、表单与状态随身份切换卸载；本机缓存按 API、身份模式、已验证买家分隔，旧共享历史不作为授权来源。
- 凭证只经 Authorization Header 发往配置的 API origin 与 commerce 路径；拒绝路径越界，强制 `redirect: error`、`credentials: omit`，不把令牌加到 URL、日志或构建资源。`vite.config.ts` 对误配置 `VITE_API_TOKEN` 直接拒绝构建。
- `prepareRequests.ts`：作用域含 API、buyer、session、action、规范载荷及显式尝试锚点。规范化商品行排序/同商品 SKU 数量聚合、ID 与地址字段 trim；取消规范化 order_id/reason。缓存键为 SHA-256 摘要，值只含随机 request_id 和可选 confirmation_id，不新增明文收货地址副本。
- 请求键在发送前保存。Web Locks 覆盖共享存储读/建/写，两个标签页同一作用域首次并发使用同一键。网络错误、响应解析失败、成功后重复点击都不换键。prepare 与 resolve 分开，resolve 不携带 prepare request_id。
- 空会话在首次 prepare 前保存，同时记录 prepare 会话标记。响应丢失后刷新不会被“恢复最近的旧对话”切到不同 session 而生成新请求键。
- 过期/拒绝确认卡增加“重新核验并生成新确认单”；已批准创建卡增加“再买一次，重新核验”。以来源 confirmation_id 为新的稳定尝试锚点，同一旧卡再点或刷新重试仍复用本次新键；要继续再次购买，从新确认卡发起。原确认和批准不被延长或复用为新批准。
- 老会话的迟到响应、摘要计算或确认响应不能更新新会话。存储不可用时明确只保证本页重试；无 Web Locks 时仍保留刷新键，但明确要求只在同一标签页操作，不承诺跨标签并发一致性。

## 验证与证据

最终前端验收：[272 个测试通过](../../../eval/verification/2026-10-16-m2c-release/tests.log)，[TypeScript 与生产构建通过](../../../eval/verification/2026-10-16-m2c-release/build.log)。M1 为 250 个测试，本批增加运行时身份、登录界面和 prepare 键覆盖，共新增 22 项；没有删除旧功能断言。

- [源码指纹](../../../eval/verification/2026-10-16-m2c-release/source-fingerprint.json)、[构建资源扫描](../../../eval/verification/2026-10-16-m2c-release/asset-audit.json)、[Node/npm 版本](../../../eval/verification/2026-10-16-m2c-release/environment.log)。Node 22.23.3，未安装或升级依赖。
- [构建共享 token 拒绝证据](../../../eval/verification/2026-10-16-m2c-frozen/forbidden-build-token.log)：仅合成值，预期退出 1。最终资源不含该值或 `VITE_API_TOKEN`；旧 storage key 字面量保留仅为删除旧凭据。
- 测试涵盖：验证前页面隔离、服务端身份而非客户端 claims、两买家缓存、sessionStorage 刷新复验、401 清除及不自动重发、403 不误当过期、迟到响应、目标 origin/路径/重定向限制、不可用存储。
- prepare 测试涵盖：规范等价、不同买家/会话/action/载荷隔离、同锁双客户端并发、reload、提交后丢响应、JSON 解析失败、确认后重复点击、新尝试、旧确认回写防护、重新核验失败的跨刷新重试、取消键、resolve 分离、切会话前尚在计算摘要时不再发送旧写请求。

## 阶段失败与复测

1. 首次 prepare 编辑脚本工作目录多了一层 frontend，路径查找失败，未应用 prepare 修改；更正目录后继续。记录见 [初始诊断](../../../eval/verification/2026-10-16-m2c-initial/edit-path-failure.log)。该次身份代码构建已通过。
2. [首次全前端回归](../../../eval/verification/2026-10-16-m2c-first/tests.log)：229 通过、21 失败。原页面夹具尚未提供新的 identity/config 入口；prepare 断言尚未包含 request_id，异步摘要导致原“立即已发出”测试假设不再成立。新增合成身份夹具、明确 request_id 与等待网络发出后，原页面行为断言全部保留并通过。
3. [第二轮](../../../eval/verification/2026-10-16-m2c-second/tests.log) 258 通过；[第三轮](../../../eval/verification/2026-10-16-m2c-third/tests.log) 268 通过；[登录界面补测](../../../eval/verification/2026-10-16-m2c-final/tests.log) 272 通过；[冻结复测](../../../eval/verification/2026-10-16-m2c-frozen/tests.log) 272 通过。最后修正登录页/身份条布局后，单独 release 目录重跑全部 272 项及构建，未覆盖前述日志。
4. 原始测试日志保留 SDK 503 合成错误场景输出与工具环境代理实验性警告；它们不代表真实 API 或付费模型调用。执行器日志时钟与对话时钟有偏差，未改写原始时间。

## 启用与回滚

- 必须与 M2 服务端身份端点和 prepare request_id 同批启用。hmac 返回 `{mode: "hmac", buyer_id, expires_at}`，expires_at 为带时区 ISO 字符串；config 返回 mode，demo 时返回 default_buyer_id。沿用服务端现有 CLI 在可信环境签发短期身份，经可信渠道给演示者；这里不签发，不提供匿名取 token 接口。
- 浏览器构建只设置公开 API base；移除任何构建 token 配置。联网私有部署仍需 HTTPS、明确 CORS 和服务端 HMAC/owner-binding 等部署边界。摘要及 Web Locks 依赖安全浏览器上下文；无 SubtleCrypto 时 prepare 明确阻止发送。
- 回滚前停止新 prepare 写入并核对已有确认。旧前端不理解请求键，因此不能继续承诺刷新/重复 prepare 幂等。此批没有数据库迁移；不要删除后端映射表或重置历史数据。回滚不得重新引入长期共享或构建 token。

## 边界与未完成

- 本文件的通过结论限单测、jsdom 页面交互、TypeScript 与构建。真实 Chromium 登录/刷新/双标签与后端 HTTP 的联合验证由 M2 集成任务另记；这里不把 mock 当作浏览器或真实外部链路完成。
- 没有调用真实模型、embedding/rerank，没有公开部署或推送。前端变更不涉及模型 Harness 策略，不能据此宣称完整模型评测或生产安全验收通过。
- localStorage 不可写或用户自行清除浏览器存储后，无法保证重开页面恢复请求键；提示明确要求先核对服务端记录。无 Web Locks 不保证不同标签页同时首次创建同键；双标签强保证只在具备锁且共享存储可写的浏览器上成立。
