"""隔离评测的显式供应商合同；不改变生产模型或其它网关的默认值。"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

from agentscope.formatter import DeepSeekChatFormatter


GLM_MODEL = 'glm-5.3-flash'
GLM_BASE_URL = 'https://open.bigmodel.cn/api/paas/v4'
GLM_CONFIG = {
    'adapter': 'bigmodel-glm-5.3-flash-v1',
    'model': GLM_MODEL,
    'base_url': GLM_BASE_URL,
    'temperature': 1,
    'top_p': 0.95,
    'reasoning_effort': 'max',
    'thinking': {'type': 'enabled', 'clear_thinking': False},
    'max_tokens_parameter': 'max_tokens',
    'tool_stream': True,
}


class ModelCallLimitExceeded(RuntimeError):
    """达到预先冻结的物理请求上限；不再提交新 HTTP 请求。"""


@dataclass
class ModelCallBudget:
    limit: int
    started: int = 0
    exhausted: bool = False

    def reserve(self):
        if self.started >= self.limit:
            self.exhausted = True
            raise ModelCallLimitExceeded('评测已达到冻结的物理模型请求上限')
        self.started += 1


model_call_budget = ContextVar('evaluation_model_call_budget', default=None)


def validate_model_config(settings, runtime):
    config = runtime.get('model_config')
    if not config:
        if settings.llm_model == GLM_MODEL:
            raise ValueError('glm-5.3-flash 必须显式选择已冻结的 GLM 评测 suite')
        return
    if config != GLM_CONFIG:
        raise ValueError('未知或被修改的供应商评测参数合同')
    if settings.llm_model != config['model'] or settings.llm_base_url.rstrip('/') != config['base_url']:
        raise ValueError('模型或网关与冻结 GLM suite 不一致；禁止静默替换')
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
        return await original(*args, **request)

    model.client.chat.completions.create = bounded_create
