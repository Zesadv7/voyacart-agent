"""隔离评测的显式供应商合同；不改变生产模型或其它网关的默认值。"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

from agentscope.formatter import DeepSeekChatFormatter
from scripts.eval.endpoint_config import GLM_MODEL, GLM_BASE_URL, GLM_CODING_URL, classify_endpoint


GLM_CONFIG = {
    'adapter': 'bigmodel-glm-5.3-flash-v1',
    'model': GLM_MODEL,
    'base_url': GLM_BASE_URL,
    'endpoint_mode': 'standard',
    'temperature': 1,
    'top_p': 0.95,
    'reasoning_effort': 'max',
    'thinking': {'type': 'enabled', 'clear_thinking': False},
    'max_tokens_parameter': 'max_tokens',
    'tool_stream': True,
}
GLM_CODING_CONFIG = {**GLM_CONFIG, 'adapter': 'bigmodel-glm-5.3-flash-coding-plan-v1',
                     'base_url': GLM_CODING_URL, 'endpoint_mode': 'coding_plan'}


class ModelCallLimitExceeded(RuntimeError):
    """达到预先冻结的物理请求上限；不再提交新 HTTP 请求。"""


class ProviderRejected(RuntimeError):
    """供应商已拒绝请求；整个本次评测禁止继续发起请求。"""


@dataclass
class ModelCallBudget:
    limit: int
    started: int = 0
    exhausted: bool = False
    provider_rejected: bool = False

    def reserve(self):
        if self.provider_rejected:
            raise ProviderRejected('供应商已拒绝请求；本次评测停止，不重试或切换端点')
        if self.started >= self.limit:
            self.exhausted = True
            raise ModelCallLimitExceeded('评测已达到冻结的物理模型请求上限')
        self.started += 1


model_call_budget = ContextVar('evaluation_model_call_budget', default=None)


def validate_model_config(settings, runtime):
    actual_endpoint = classify_endpoint(settings.llm_base_url)
    config = runtime.get('model_config')
    if not config:
        if settings.llm_model == GLM_MODEL or actual_endpoint == 'coding_plan':
            raise ValueError('glm-5.3-flash 必须显式选择已冻结的 GLM 评测 suite')
        return
    if config not in (GLM_CONFIG, GLM_CODING_CONFIG):
        raise ValueError('未知或被修改的供应商评测参数合同')
    if settings.llm_model != config['model']:
        raise ValueError('模型与冻结 GLM suite 不一致；请检查 HARNESS_LLM_MODEL，不会替换模型')
    if settings.llm_base_url.rstrip('/') != config['base_url']:
        raise ValueError(f"端点不匹配：期望={config['endpoint_mode']}，实际={actual_endpoint}；先运行 config-check 并核对 endpoint_mode 和 Repository secret HARNESS_LLM_BASE_URL，不会覆盖地址")
    if settings.llm_enable_thinking is not None:
        raise ValueError('GLM suite 不接受 LLM_ENABLE_THINKING；思考参数由冻结合同指定')
    if settings.prompt_cache_mode != 'passthrough':
        raise ValueError('GLM suite 暂只验证原生缓存，不支持未核验的显式缓存标记')


def configure_evaluation_model(model, runtime):
    """在真实 SDK HTTP 边界映射参数，摘要/工具/续写共用同一计数器。"""
    model.parameters.max_tokens = runtime['output_limit']
    model.parameters.temperature = runtime.get('model_config', {}).get('temperature', 0)
    model.client.timeout = runtime['request_timeout_seconds']
    config = runtime.get('model_config')
    if config:
        # SDK 原生此 formatter 实现标准 reasoning_content/tool 配对；本套件仅文本。
        # OpenAIChatFormatter 会主动丢弃 ThinkingBlock，不能用于 GLM 交错思考。
        model.formatter = DeepSeekChatFormatter()
        model.parameters.top_p = config['top_p']
    if getattr(model, '_evaluation_http_configured', False):
        raise ValueError('评测模型不能重复安装 HTTP 合同')
    model._evaluation_http_configured = True
    original = model.client.chat.completions.create

    async def bounded_create(*args, **request):
        if config:
            if request.get('model') != config['model']:
                raise ValueError('实际模型与冻结 GLM suite 不一致')
            request = deepcopy(request)
            # AgentScope 2.0.8 默认写 max_completion_tokens；BigModel 文档只声明 max_tokens。
            maximum = request.pop('max_completion_tokens', request.get('max_tokens', runtime['output_limit']))
            request['max_tokens'] = min(maximum, runtime['output_limit'])
            request['temperature'] = config['temperature']
            request['top_p'] = config['top_p']
            request.pop('reasoning_effort', None)
            extra = dict(request.get('extra_body') or {})
            extra.pop('enable_thinking', None)
            extra.update(thinking=deepcopy(config['thinking']), reasoning_effort=config['reasoning_effort'])
            if request.get('stream'):
                extra['tool_stream'] = config['tool_stream']
            request['extra_body'] = extra
        budget = model_call_budget.get()
        if budget is not None:
            budget.reserve()
        try:
            return await original(*args, **request)
        except Exception as error:
            status = getattr(error, 'status_code', None)
            if config and budget is not None and type(status) is int and 400 <= status <= 599:
                budget.provider_rejected = True
            raise

    model.client.chat.completions.create = bounded_create
