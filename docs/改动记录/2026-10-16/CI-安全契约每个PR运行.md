# CI：安全契约对每个 PR 运行

## 需求

main 分支规则集“main 分支保护”把 `Offline safety contracts` 设为必需检查。原工作流在 `pull_request` 和 `push` 上带路径过滤，只改文档、数据或评测证据的 PR 不会触发该检查，导致 PR 一直等待必需检查，无法合并（例如 ESCI v2 数据 PR）。

## 具体变更

- `.github/workflows/harness-eval.yml` 去掉 `pull_request` 与 `push` 的路径过滤，`Offline safety contracts` 对每个 PR 和 main 推送都运行。
- 新增 `changes` 任务，用 `git diff --name-only` 比较 PR 基线（推送时为推送前提交）与当前提交；只有原路径过滤覆盖的路径变化时，才运行 `Real model evaluation`。路径集合与原过滤一致：`app/`、`scripts/eval/`、`eval/harness/v1/`、`eval/learning/`、`tests/`、`data/catalog-v1.jsonl`、`Makefile`、`pyproject.toml`、`uv.lock` 与本工作流文件。手动触发总是运行真实模型评测。
- `Real model evaluation` 依赖 `changes` 与 `safety-contracts`，缺少评测凭据时仍明确失败，不静默跳过。

## 验证

- 本地解析工作流 YAML：三个任务、依赖与条件符合预期。
- 路径判定抽样：`README.md`、`docs/`、`eval/esci-v2/`、`data/catalog-esci-v2.jsonl.gz` 判为跳过；`app/`、`tests/`、工作流文件判为运行；`data/catalog-v1.jsonl.bak` 判为跳过。
- GitHub Actions 实际运行结果以本 PR 的检查为准。

## 启用与回滚

合并后立即生效。回滚时恢复原 `paths` 过滤并删除 `changes` 任务；回滚后需同时从规则集移除必需检查，否则文档类 PR 会再次被阻塞。
