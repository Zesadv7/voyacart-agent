"""正式 ESCI 输入齐全时生成独立十场景开发套件，保留原准备证据。"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.eval.harness.contracts import ROOT, assert_output_path, write_json
from scripts.eval.harness.esci import build_suite, offline_resource_preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/catalog-esci-v2.jsonl.gz')
    parser.add_argument('--selection', type=Path, default=ROOT / 'data/esci/selection-v2.json')
    parser.add_argument('--ranking', type=Path, default=ROOT / 'eval/esci-v2/ranking-dev.jsonl')
    parser.add_argument('--pairing-manifest', type=Path, help='成套正式输入 manifest，默认位于 ranking 同目录')
    parser.add_argument('--translations', type=Path, required=True, help='独立 train 中文译文缓存')
    parser.add_argument('--index-dir', type=Path, required=True, help='独立 Harness 向量索引缓存')
    parser.add_argument('--local-model-cache', type=Path, required=True, help='预先准备的本地多语种模型缓存')
    parser.add_argument('--output', type=Path, required=True, help='新建套件目录')
    args = parser.parse_args()
    try:
        paths = {name: getattr(args, name) for name in ('catalog', 'selection', 'ranking', 'translations')}
        if args.pairing_manifest is not None:
            paths['pairing_manifest'] = args.pairing_manifest
        suite, cases = build_suite(paths,
            index_dir=args.index_dir, local_model_cache=args.local_model_cache)
        output = assert_output_path(args.output)
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / 'suite.json', suite)
        write_json(output / 'cases.json', cases)
        preflight = offline_resource_preflight(args.local_model_cache)
        write_json(output / 'preflight.json', preflight)
        print(json.dumps({'status': 'bound_development_suite', 'runnable': False, 'suite_bound': True,
                          'execution_resources': preflight, 'real_ten_scenarios': 'not_executed', 'case_count': len(cases),
                          'model_calls': 0, 'suite': str(output / 'suite.json')}, ensure_ascii=False))
        return 0
    except (ValueError, FileNotFoundError) as error:
        print(json.dumps({'status': 'prepared', 'runnable': False, 'model_calls': 0, 'reason': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
