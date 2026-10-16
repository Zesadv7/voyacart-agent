"""ESCI 成套输入的离线边界：先验指纹与身份映射，后运行/写索引。"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'esci-input-pairing-v1'
MANIFEST_NAME = 'input-pairing.json'
DATA_FILES = ('ranking-dev', 'retrieval-dev', 'constraints-dev',
              'ranking-holdout', 'retrieval-holdout', 'constraints-holdout', 'crosslingual-zh')
SOURCE_COMMIT = '7916cdf6ab75a462e77f20ab40428a10923998d5'
# 正式冻结清单自身也固定；外部临时输入只能显式标作 constructed_fixture。
FROZEN_MANIFESTS = {'v1': '62425aec8c2fa618a4d53612e955484a531d25bb167db979c7c391d99c233c9fb7e7f0a85dc601f8bbb7c46ebb107c869f53fc6aefe7490b7adf51c7388f7624', 'v2': '9917c2df31367e985a6585a77d36625524b9f5040452112091432016bca3c42bc45019e1fcdbaa815f13e87d3a14847f5d5be06ca63392518344c84e7071d743'}


def fingerprint(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'blake2b').hexdigest()


def catalog_identity(path: Path) -> dict:
    """站点属于身份；排序后的内部商品/SKU→外部身份映射单独留证。"""
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    products, skus, external, mapping = set(), set(), set(), []
    for row in rows:
        product_id = row['product_id']
        identity = [row['source_platform'], row['source_locale'], row['external_product_id']]
        if (product_id in products or tuple(identity) in external or not all(identity)
                or not row['skus']):
            raise ValueError('ESCI 配对目录商品/外部身份重复或为空')
        products.add(product_id)
        external.add(tuple(identity))
        items = []
        for sku in row['skus']:
            if sku['sku_id'] in skus:
                raise ValueError('ESCI 配对目录 SKU 重复')
            skus.add(sku['sku_id'])
            items.append({'sku_id': sku['sku_id'], 'spec': sku['spec']})
        mapping.append({'product_id': product_id, 'external_identity': identity,
                        'skus': sorted(items, key=lambda item: item['sku_id'])})
    if not rows:
        raise ValueError('ESCI 配对目录为空')
    encoded = json.dumps(sorted(mapping, key=lambda row: row['product_id']),
                         ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return {'method': 'blake2b-canonical-json-product-external-identity-sku-v1',
            'blake2b': hashlib.blake2b(encoded).hexdigest(),
            'product_count': len(products), 'sku_count': len(skus)}


def create_manifest(paths, *, manifest_path, dataset_version, role, source_commit) -> dict:
    """显式制备工具；运行校验绝不自动给未登记的输入补签。"""
    paths = {name: Path(path).resolve() for name, path in paths.items()}
    return {'schema_version': SCHEMA, 'dataset_version': dataset_version,
            'baseline_version': dataset_version, 'role': role, 'source_commit': source_commit,
            'fingerprint_method': 'blake2b',
            'identity_mapping': catalog_identity(paths['catalog']),
            'artifacts': {name: {'path': os.path.relpath(path, Path(manifest_path).resolve().parent),
                                 'bytes': path.stat().st_size, 'blake2b': fingerprint(path)}
                          for name, path in paths.items()}}


def validate_pairing(*, catalog, manifest=None, dataset_dir=None, selection=None, ranking=None,
                     profile='dev', baseline_version=None, allow_fixture=False) -> dict:
    """开发档只核验开发字节，不打开留出；留出档在消耗尝试前核验其字节。"""
    if profile not in ('dev', 'holdout', 'harness'):
        raise ValueError('未知 ESCI 配对校验档位')
    if manifest is None:
        directory = Path(dataset_dir) if dataset_dir is not None else Path(ranking).parent
        manifest = directory / MANIFEST_NAME
    manifest = Path(manifest).resolve()
    try:
        content = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'ESCI 配对 manifest 缺失或不可读：{manifest}') from error
    if content.get('schema_version') != SCHEMA or content.get('fingerprint_method') != 'blake2b':
        raise ValueError('未知 ESCI 配对 manifest schema/指纹算法')
    role = content.get('role')
    if role not in ('frozen_esci', 'constructed_fixture'):
        raise ValueError('未知 ESCI 配对 manifest 角色')
    if role == 'constructed_fixture' and not allow_fixture:
        raise ValueError('自定义夹具须显式启用 fixture manifest，不能冒充正式 ESCI')
    if allow_fixture and role != 'constructed_fixture':
        raise ValueError('fixture manifest 选项只接受明确标记的构造夹具')
    version = content.get('dataset_version')
    if not isinstance(version, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', version):
        raise ValueError('ESCI 数据集版本无效')
    if content.get('baseline_version') != version or (baseline_version is not None and baseline_version != version):
        raise ValueError('ESCI baseline-version 与输入配对 manifest 不同')
    if role == 'frozen_esci' and fingerprint(manifest) != FROZEN_MANIFESTS.get(version):
        raise ValueError('ESCI 正式配对 manifest 指纹与冻结版本不同')
    artifacts = content.get('artifacts')
    required = {'catalog', 'selection', *DATA_FILES} if role == 'frozen_esci' else {'catalog', 'selection', 'ranking-dev'}
    if not isinstance(artifacts, dict) or not required <= artifacts.keys() or set(artifacts) - {'catalog', 'selection', *DATA_FILES}:
        raise ValueError('ESCI 配对 manifest 输入缺失或未知')
    if role == 'frozen_esci' and (version not in ('v1', 'v2') or content.get('source_commit') != SOURCE_COMMIT):
        raise ValueError('ESCI 正式输入来源版本无效')
    resolved = {}
    for name, item in artifacts.items():
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str) or not item['path']
                or type(item.get('bytes')) is not int or item['bytes'] <= 0
                or not isinstance(item.get('blake2b'), str) or not re.fullmatch('[0-9a-f]{128}', item['blake2b'])):
            raise ValueError(f'ESCI 配对 manifest 文件描述无效：{name}')
        resolved[name] = (manifest.parent / item['path']).resolve()
    expected = {'catalog': Path(catalog).resolve()}
    if selection is not None:
        expected['selection'] = Path(selection).resolve()
    if ranking is not None:
        expected['ranking-dev'] = Path(ranking).resolve()
    if dataset_dir is not None:
        kinds = ('ranking', 'retrieval', 'constraints')
        expected.update({f'{kind}-{profile}': (Path(dataset_dir) / f'{kind}-{profile}.jsonl').resolve() for kind in kinds})
        if profile == 'holdout':
            expected['crosslingual-zh'] = (Path(dataset_dir) / 'crosslingual-zh.jsonl').resolve()
    checked = {'catalog', 'selection', *expected.keys()}
    if role == 'constructed_fixture':
        protected = {(ROOT / f'data/catalog-esci-{v}.jsonl.gz').resolve() for v in ('v1', 'v2')}
        protected.update((ROOT / f'data/esci/selection-{v}.json').resolve() for v in ('v1', 'v2'))
        protected.update((ROOT / folder / (name + '.jsonl')).resolve()
                         for folder in ('eval/esci', 'eval/esci-v2') for name in DATA_FILES)
        if any(path in protected for path in resolved.values()):
            raise ValueError('构造夹具不能给正式冻结目录或评测输入补签')
    for name in checked:
        if name not in artifacts or (name in expected and expected[name] != resolved[name]):
            raise ValueError(f'ESCI 输入不属于同一配对 manifest：{name}')
        path, item = resolved[name], artifacts[name]
        try:
            valid = path.stat().st_size == item['bytes'] and fingerprint(path) == item['blake2b']
        except OSError as error:
            raise ValueError(f'ESCI 配对输入缺失：{name}') from error
        if not valid:
            raise ValueError(f'ESCI 配对输入指纹不符：{name}')
    if catalog_identity(resolved['catalog']) != content.get('identity_mapping'):
        raise ValueError('ESCI 配对内部编号→外部身份/SKU 映射指纹不符')
    return {'schema_version': SCHEMA, 'dataset_version': version, 'role': role,
            'source_commit': content.get('source_commit'), 'manifest': str(manifest),
            'manifest_blake2b': fingerprint(manifest), 'identity_mapping': content['identity_mapping'],
            'verified_artifacts': {name: {'path': str(resolved[name]), 'blake2b': artifacts[name]['blake2b']}
                                   for name in sorted(checked)}}
