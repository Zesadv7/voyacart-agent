# ESCI JSONL 原文读取

ESCI 真实预选商品含 24 个 U+2028 与 1 个 U+0085 字符。原入口将 JSONL 全文传入 `splitlines()`，这些字符被当成记录分隔符，合法 JSON 字符串因此拆断。独立 Harness 计划的来源核验发现此问题后，本轮按用户已授权的数据读取范围修复。

## 具体改动

- `scripts/esci/finalize_catalog.py` 的 `load_rows` 使用 UTF-8 文件逐行读取，覆盖正式目录输入及增强/译文缓存的共享读取路径。
- `scripts/esci/prepare.py` 的查询缓存读取使用相同文件行边界，保留原有唯一键、状态和类目校验。
- 现有 ESCI 脚本测试增加 U+2028、U+2029、U+0085 三组数据，验证商品/查询/译文中的字符和无尾换行的末条记录完整保留。目录确定性测试覆盖带这些字符的原始标题，连续导出仍逐字节一致；测试内 JSONL 读取也采用文件行边界。

商品要点的文本分行规则保持原样。改动集中在 `scripts/esci/`，应用、默认 Harness 内核、`scripts/eval/` 与 `tests/` 源码保持原样。

## 验证与阶段证据

[修复前](../../../eval/verification/2026-10-14-esci-jsonl-boundary-before/)：新增 9 个用例全部失败，真实 `products.jsonl` 读取也复现 `JSONDecodeError`。失败日志、原始夹具、源码快照与输入内容指纹完整保留。各阶段原始夹具使用 `fixtures.tar.gz` 归档，并由 `fixtures-manifest.json` 核对每个文件字节；构造目录与评测输出均为测试夹具。

[首次复测](../../../eval/verification/2026-10-14-esci-jsonl-boundary-after/pytest.log)：ESCI 小套件 38 项通过。补齐目录输出标题原样保留和测试内文件行读取后，[最终回归](../../../eval/verification/2026-10-14-esci-jsonl-boundary-final/pytest.log) 同样 38 项通过。

最终验证命令：

```sh
.venv/bin/python -m pytest -q scripts/esci --basetemp eval/verification/2026-10-14-esci-jsonl-boundary-final/fixtures --junitxml=eval/verification/2026-10-14-esci-jsonl-boundary-final/tests.xml
```

[真实文件复核](../../../eval/verification/2026-10-14-esci-jsonl-boundary-after/real-input-verification.json)：15,860 件商品、15,977 条原始标注、15,860 条增强输入、2,784 条已有分类缓存均成功读取，与独立文件逐行读取逐字段一致；原始输入和缓存字节保持原样。模型及外部 HTTP 调用 0。

本次检查使用本地夹具和现有文件，完整 Harness 模型门禁保持未执行。默认 Harness 源码指纹范围的文件原样，静态 ESCI 场景计划的校验结果继续有效。

## 启用及剩余事项

既有 T1/T2 CLI 自动采用正确文件行读取，无配置或数据格式变化；`.env` 与已有缓存原样保留。对照旧版读取失败可使用修复前源码快照及同一真实商品输入。商品增强、最终目录、正式评测结果和人工抽检仍待后续数据阶段完成。

本轮起点为 `c6ee4cd84d7eefa3fe34aec80eb13ded4a9e8ac2`，新增修复为独立本地提交，实际代码版本以本记录所在提交为准。静态场景与首次发现过程见 [T4 记录](T4-ESCI评测入口准备.md)。
