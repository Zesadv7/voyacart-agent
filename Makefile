HARNESS_RUN_DIR ?= eval/verification/harness-local-$(shell date +%Y%m%d-%H%M%S)
HARNESS_SUITE ?= eval/harness/v1/suite.json
HARNESS_STRATEGIES ?= current,candidate
HARNESS_PYTHON ?= uv run python

.PHONY: eval-harness eval-harness-contracts eval-harness-dev eval-harness-release

# 每次 Harness 修改：安全契约 + 完整真实模型冒烟 + 同源码证据检查。
eval-harness:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile smoke --strategies $(HARNESS_STRATEGIES) --output $(HARNESS_RUN_DIR)
	$(HARNESS_PYTHON) -m scripts.eval.harness verify $(HARNESS_RUN_DIR)

eval-harness-contracts:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile contracts --output $(HARNESS_RUN_DIR)

eval-harness-dev:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile dev --strategies $(HARNESS_STRATEGIES) --output $(HARNESS_RUN_DIR)

eval-harness-release:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile release --strategies $(HARNESS_STRATEGIES) --output $(HARNESS_RUN_DIR) --require-benefit
	$(HARNESS_PYTHON) -m scripts.eval.harness verify $(HARNESS_RUN_DIR) --minimum release

.PHONY: test-backend test-loadtest test-frontend smoke-mock check-compose
# 首次使用：uv sync --frozen --extra esci --extra optimization；安装 redis-server。
test-backend:
	uv run --frozen --extra esci --extra optimization python -m scripts.ci.backend

# gevent/Locust 与普通异步测试分开解释器，依赖均来自 uv.lock。
test-loadtest:
	uv run --frozen --isolated --group loadtest python -m pytest -q -ra tests/test_agui_loadtest.py tests/test_hotspot_client.py

test-frontend:
	npm --prefix frontend test
	npm --prefix frontend run build

# 仅启动回环 mock；数据写入一次性目录，报告目录不能复用。
smoke-mock:
	uv run --frozen python -m scripts.ci.mock_smoke --output $(HARNESS_RUN_DIR)-http

check-compose:
	LLM_BASE_URL=http://mock-llm:8081/v1 LLM_API_KEY=ci-local-placeholder docker compose -f docker/docker-compose.yaml config --quiet
	LLM_BASE_URL=http://mock-llm:8081/v1 LLM_API_KEY=ci-local-placeholder docker compose -f docker/docker-compose.scale.yaml --profile mock config --quiet
