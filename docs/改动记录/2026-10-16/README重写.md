# README 重写

## 需求

项目改名为 Voyacart 并迁到新仓库后，按项目当前状态重写 README，不沿用原课程工程的 README。

## 具体变更

- 新 README 按"能做什么 → 后端设计要点 → 检索与评测 → 架构 → 技术栈 → 快速开始 → 主要配置 → 测试与评测 → 目录结构 → 设计文档 → 当前局限 → 项目来源"组织。
- 后端设计要点逐项链接 `eval/verification/` 与 `docs/改动记录/` 中的原始证据：API 停机续传、worker 崩溃重投、推送路径锁等待、热点库存、共享熔断、模型 429/5xx 回退。
- 检索评测使用 ESCI 正式评测 v2 的留出结果，并保留 v1 报告链接；注明类目来自模型批量增强、人工抽检未执行。
- 启动、Compose、假模型、压测与评测命令与 `pyproject.toml`、`Makefile`、`docker/` 下的 Compose 文件和 `scripts/` 入口逐项核对。
- 原 README 中的界面截图来自旧名称的界面，本次不再引用。

## 验证

- README 中引用的证据文件与文档路径均已核对存在于合并后的 main。
- 配置变量名已在 `app/infrastructure/settings.py` 或 `docker/` 中核对。
- 文档改动，不涉及代码、模型调用或 Harness。

## 未完成

- 新界面截图尚未补充。
