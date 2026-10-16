# M3c：目录来源展示、数据卡与人工审核准备

日期：2026-10-16（Asia/Shanghai）。实施起点 `41666a3`，沿用 M2 买家身份、事件公开审核、金额/币种/SKU 合同。未更换默认 5,700 件 demo 目录，未改冻结 ESCI 数据、历史评测或共享日期索引。关联本地提交可用 `git log --all --grep='M3c'` 定位；本轮源码指纹见 [manifest](../../../eval/verification/2026-10-16-m3c-provenance/manifest.json)。

## 需求与实现

1. 前端 `ProductProvenance` 与 [来源 parser](../../../frontend/src/lib/productProvenance.ts) 复制白名单字段和枚举，保留 category_source/category_method/category_status、material_status、origin_country_method、source_text_processing。拒绝未知嵌套元数据、自称 human_verified 的状态及未知来源值；不渲染原始 JSON。
2. 商品详情不再把 origin_country 标为“发货地”。ESCI（包括旧 v1/v2 无 method 字段的记录）显示“来源站点，站点代理，不代表发货地或产地”。显式 synthetic 显示模拟国家/地区；缺来源显示“来源未说明”，不按编号猜来源。
3. 分类旁区分查询代理、模型推断、合成样例和未知来源。商品卡、历史批次、比较与工具迷你卡沿用同一来源说明。材质明确未知或未人工核验；空标签不能用于“无某材质”的保证。
4. 价格、库存、配送及到手价规则明确为演示；原文处理说明区分 HTML 清洗、截断、颜色附加与查询翻译，不声称商品已翻译。保留原有商品标题、示意/演示标记与下单意向交互。
5. 后端 [`public_output.py`](../../../app/infrastructure/security/public_output.py) 新增来源专用白名单，之后仍经过 M2 `public_value` 审核；[`tool_product_view.py`](../../../app/infrastructure/tool_product_view.py) 透传已审核来源、站点和分类。新增文本字段先完整审核再截断，避免展示长度限制把凭据模式切断。Product/SearchHit 的来源类型改为 `str | dict[str, object]`，运行行为不变。
6. 新增 [数据卡](../../数据卡/ESCI目录数据卡.md)、[固定人工审核规则](../../数据卡/ESCI人工抽检规则.md) 和 200/50 待审副本。原表未改、人工列未填；固定版许可仍待核对。审计发现 demo 的 500 条旧合成样例缺来源字段，明确记录该历史缺口，不伪造单条来源。

## 验证与原始失败

证据目录：[`2026-10-16-m3c-provenance`](../../../eval/verification/2026-10-16-m3c-provenance/)。沿用已安装 Node 22 与 `.venv`，未安装、升级、外部模型调用或推送。Python 检查用 `scripts.ci.offline_env.isolated_env`，不继承真实凭据/.env/远程 Redis/Qdrant；数据库仅测试临时目录。执行器日志时钟比会话 UTC 落后约 7 小时，保留原始日志时间，不改写。

| 阶段 | 实际结果 | 证据 |
|---|---|---|
| 新前端合同先复现 | 6 failed：来源丢失、发货地误标、缺少推断标记 | [frontend-red.log](../../../eval/verification/2026-10-16-m3c-provenance/frontend-red.log) |
| 新后端合同先复现 | 10 failed：来源丢失、任意元数据透传 | [backend-red.log](../../../eval/verification/2026-10-16-m3c-provenance/backend-red.log) |
| 前端定向 | 50 passed | [frontend-focused.log](../../../eval/verification/2026-10-16-m3c-provenance/frontend-focused.log) |
| 后端初次定向回归 | 162 passed | [backend-focused.log](../../../eval/verification/2026-10-16-m3c-provenance/backend-focused.log) |
| 前端首轮全量/构建 | 278 passed；TypeScript + Vite 通过 | [frontend-all.log](../../../eval/verification/2026-10-16-m3c-provenance/frontend-all.log)、[build](../../../eval/verification/2026-10-16-m3c-provenance/frontend-build.log) |
| 追加完整审核后截断测试 | 162 passed / 1 failed：测试把 canary 无分隔符拼到长字母串，未满足既有 M2 凭据模式的词边界 | [backend-final.log](../../../eval/verification/2026-10-16-m3c-provenance/backend-final.log) |
| 修正测试输入并复测 | 163 passed；保留完整审核后截断实现，未放宽检测或白名单 | [backend-retest.log](../../../eval/verification/2026-10-16-m3c-provenance/backend-retest.log) |
| 最终前端全量/构建 | 23 files / 279 passed；TypeScript + Vite 通过 | [frontend-final.log](../../../eval/verification/2026-10-16-m3c-provenance/frontend-final.log)、[final build](../../../eval/verification/2026-10-16-m3c-provenance/frontend-build-final.log) |
| 数据来源与待审表 | 数量/指纹核对；200/50 行，人工有效数均为 0 | [source-audit.json](../../../eval/verification/2026-10-16-m3c-provenance/source-audit.json) |
| Harness impact | public_output 触发 contracts + smoke | [harness-impact.log](../../../eval/verification/2026-10-16-m3c-provenance/harness-impact.log) |

专项测试为 [`tests/test_catalog_provenance_public.py`](../../../tests/test_catalog_provenance_public.py)（11 项）和 [`frontend/tests/productProvenance.test.tsx`](../../../frontend/tests/productProvenance.test.tsx)（7 项）。后端回归同时包含 M2 public_output_boundary、public_output_review 的敏感串/opaque/公开合同，以及 ag_ui_tool_products 和 catalog_fixture。来源白名单与真实 SQLite 日志/回放、按调用 mini-card 的来源透传均有覆盖；原金额/币种/SKU 测试保留。

## 未完成与验收口径

- 真正人工类目/材质 200、译文 50 审核均未执行，不能把待审表准备写成“人工审核完成”。
- 上游固定版本 LICENSE/NOTICE 未外部核验，不能写“已确认可自由分发”。
- 本分工只跑相关后端测试及前端全量；全仓后端/HTTP/合并后 Harness contracts、compare/verify 由 M3 集成验收另记，不能据此声称全量后端通过。
- `make eval-harness` 的真实模型 smoke 受本轮禁止外部/模型调用限制，未执行，状态 BLOCKED；真实十场景与完整效果门禁未通过。旧 v2 检索结果继续属于历史源码/数据，不是当前源码验收。
- UI 为 jsdom 组件/交互合同与生产构建验收，未在本分工执行实际浏览器像素检查。

## 启用与回滚

来源展示随前后端代码生效，不新增开关、不改 `.env`、不改数据库，不需要重建数据卷或目录迁移。回滚应按本功能提交回退对应源码与测试，保留原始失败、数据卡及待审记录作为证据；不能回退 M2 安全边界或覆盖其他并行 M3 改动。冻结目录与历史 QA 原表无需回滚，因为从未改写。新增审核副本在正式人工填写后应另存版本，不以本轮脚本覆盖审核结论。
