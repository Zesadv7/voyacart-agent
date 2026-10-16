# ESCI 人工抽检规则

规则版本：`esci-human-qa-v1`。冻结日期：2026-10-16（Asia/Shanghai）。状态：规则与工作表准备完成，人工审核未开始。

## 范围与证据

抽样沿用历史定稿的 [200 件类目/材质表](../../eval/verification/2026-10-15-esci-finalize-v2/audit/category-sample-200.csv) 与 [50 条查询译文表](../../eval/verification/2026-10-15-esci-finalize-v2/audit/translation-sample-50.csv)，不重新挑选“容易通过”的样本。本轮读取到的原“正确/备注”栏均空白；完整指纹、行数、待审副本路径见 [审计记录](../../eval/verification/2026-10-16-m3c-provenance/source-audit.json)。

待审副本逐行保留全部原字段，并增加 rubric_version、reviewer、reviewed_at、judgment、error_types、correction、evidence、adjudicator、adjudicated_at、model_precheck。仅预填规则版本；审核人、时间、判断和结论全部留空。副本文件分别是 [category-review-200.csv](../../eval/verification/2026-10-16-m3c-provenance/audit/category-review-200.csv) 与 [translation-review-50.csv](../../eval/verification/2026-10-16-m3c-provenance/audit/translation-review-50.csv)。

## 填写与计数

1. 由真实审核者填写 reviewer（可用可追溯的审核 ID，不填个人秘密）、reviewed_at（带时区的 ISO 时间）与 judgment：`correct`、`incorrect` 或 `uncertain`。自动程序与模型不能填 reviewer=human 或冒用人名。
2. evidence 必须指出原始来源字段/可核对的文本片段或来源记录；不能以“模型这么说”作证据。需要补读商品原文时按 locale+asin 找原记录，不能仅凭跨版本 P 编号连接。
3. incorrect 填 error_types 和 correction；uncertain 写明缺证据/边界歧义，不能强行填正确。原“正确/修正/备注”栏作为历史字段保留，不自动从模型判断同步。
4. 可在 model_precheck 另放有版本标识的机器预审，必须明确模型来源，不计入人工覆盖率。当前全部为空；本轮没有执行机器预审。
5. 分歧由第二位真实审核者填写 adjudicator/adjudicated_at 及裁定依据，保留初审记录；不要删除错误行或覆盖为无争议样本。

有效人工样本 N 要同时满足真实 reviewer、reviewed_at、规则版本、上述三值判断与 evidence。覆盖率=N/原抽样总数；正确比例=correct/N；错误比例=incorrect/N；不确定比例=uncertain/N，不确定不能当作正确。N=0 时三个比例写“未知”，不能写 0% 准确率或 100% 通过。另报告缺字段、未审核和重复行数。需要报告“已裁定样本准确率”时必须另外列裁定分母与不确定数量，不能代替全体覆盖率。

## 类目与材质：200 件

以商品实际用途及原文证据判断，不能把查询的意图类别视为商家真实类目。八类为旅行装备、数码配件、户外运动、家居生活、厨房餐饮、美妆个护、母婴宠物、办公学习；不符合或证据不足时使用“其他/uncertain”并说明。

先固定图书边界：原文明确为教材、练习册、考试/语言学习材料，按“办公学习”；普通小说、休闲阅读、收藏/宗教读物等无明确学习用途证据的图书归“其他”；用途不清时 uncertain。不得为了与某一模型结果一致而逐行更换边界。其他跨类目商品按主要功能判断，若标题/描述无法确认，记录边界分歧。

类别错误代码：
- `category_boundary`：类别边界、图书归属或主要功能判断错误
- `insufficient_evidence`：原文不足以支持类别/属性，不等于相反事实成立
- `source_corrupt`：原文乱码、截断、清洗损失或 locale/asin 错绑
- `material_wrong`：材质与明确原文矛盾或把局部材质扩大到整件
- `functional_overclaim`：把推断升级为功效、适用、安全或认证承诺

材质仅允许现有枚举（合成聚合物、金属、天然纤维、玻璃、陶瓷、纸、天然材料）；有标签需证据支持，空标签表示未提供/未知，不能推出“不含”。含多部件时在 evidence 标明部件范围。对防过敏、食品接触、安全、无塑料等硬约束，不以模型标签独立确认。类目和材质均无错误且证据充分才能把整行判 correct；仅其中一项可确认时在备注说明，整体仍 uncertain。

## 查询译文：50 条

译文只改变语言，必须保留原查询购物意图、否定、数量、品牌、型号、单位和限制范围。不能加原文没有的用途、性能、适用对象、材质或确定性。品牌型号无须强行中文化；有歧义时允许保留原词并写 uncertain。

错误代码：
- `negation_lost`：漏译/颠倒否定，如“不含、不要、无”
- `identity_quantity_lost`：品牌、型号、尺寸或数量丢失/改动
- `unit_error`：单位、数量级或换算错误
- `meaning_expanded`：增加原文没有的需求或功能保证
- `ambiguous_meaning`：缩写、站点语言或歧义无可靠解释
- `omission_or_corruption`：遗漏关键片段、乱码或无依据重写

## 审核后处理

本表是数据质量诊断，不是新的独立检索效果实验。尤其 200 条中文派生查询来自已消费 holdout，不得修订原 holdout 后继续声称未见独立效果。修正类目、材质或译文如会改变检索输入，应新建版本、保留输入/来源指纹，重新准备开发与独立评价；不能原地覆盖冻结目录、原抽样、approved/frozen 基线。人工 QA、真实模型执行及发布许可各自独立记状态，不相互替代。
