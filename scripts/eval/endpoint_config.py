"""零网络配置诊断：只报告白名单分类，绝不输出 URL、模型原文或密钥。"""
import argparse
import json
import os
from pathlib import Path


GLM_MODEL = 'glm-5.3-flash'
GLM_BASE_URL = 'https://open.bigmodel.cn/api/paas/v4'
GLM_CODING_URL = 'https://open.bigmodel.cn/api/coding/paas/v4'
ENDPOINT_MODES = ('standard', 'coding_plan')
PROFILES = ('config-check', 'preflight', 'smoke', 'dev', 'release')


def classify_endpoint(value):
    # 完整白名单匹配，不解析/回显 userinfo、query 或任意供应商返回值。
    return {GLM_BASE_URL: 'standard', GLM_CODING_URL: 'coding_plan'}.get(
        value.rstrip('/') if isinstance(value, str) else '', 'unknown')


def inspect_configuration(environ, endpoint_mode='standard', profile='config-check'):
    actual = classify_endpoint(environ.get('LLM_BASE_URL', ''))
    model = environ.get('LLM_MODEL', '')
    model_status = 'expected' if model == GLM_MODEL else ('other' if model else 'missing')
    key_present = environ.get('HARNESS_KEY_CONFIGURED', '').lower() == 'true'
    issues = []
    if endpoint_mode not in ENDPOINT_MODES:
        issues.append('invalid_endpoint_mode')
    if profile not in PROFILES:
        issues.append('invalid_profile')
    # 原有其它 provider 的标准评测配置保持不变；本诊断不声称验证其服务能力。
    other_provider = (model_status == 'other' and profile != 'config-check' and
                      endpoint_mode == 'standard' and actual != 'coding_plan')
    if not key_present:
        issues.append('api_key_not_configured')
    if not environ.get('LLM_BASE_URL'):
        issues.append('endpoint_missing')
    if not other_provider and model_status != 'expected':
        issues.append('model_not_glm_5_3_flash')
    if not other_provider and actual != endpoint_mode:
        issues.append('endpoint_mode_mismatch')
    configuration_matches = not issues
    return {
        'scope': 'configuration_only_no_model_request',
        'endpoint_mode': endpoint_mode if endpoint_mode in ENDPOINT_MODES else 'unknown',
        'endpoint_class': actual,
        'profile': profile if profile in PROFILES else 'unknown',
        'model_status': model_status,
        'api_key_configured': key_present,
        'configuration_matches': configuration_matches,
        'other_provider_unchanged': other_provider,
        'model_calls': 0,
        'issues': issues,
        'next_step': ('请检查所选 endpoint_mode、Repository secrets 的 HARNESS_LLM_BASE_URL、HARNESS_LLM_API_KEY 和变量 HARNESS_LLM_MODEL；不会覆盖配置。'
                      if issues else
                      '仅确认配置匹配，未验证账户余额、密钥有效性或模型可用性；需要单独批准 preflight。'),
    }


def main(argv=None, environ=None):
    parser = argparse.ArgumentParser(description=__doc__)
    # 不用 argparse choices 回显非法输入；诊断结果只用固定枚举。
    parser.add_argument('--endpoint-mode', default='standard')
    parser.add_argument('--profile', default='config-check')
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    report = inspect_configuration(env, args.endpoint_mode, args.profile)
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    print(text, end='')
    if env.get('GITHUB_STEP_SUMMARY'):
        with Path(env['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as output:
            output.write(text)
    return 0 if report['configuration_matches'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
