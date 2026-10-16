"""测试显式登记构造夹具，不在生产入口自动跳过配对。"""
import json
from pathlib import Path

from scripts.eval.esci_pairing import DATA_FILES, create_manifest


def write_fixture_pairing(directory, catalog, *, selection=None, ranking=None):
    directory = Path(directory)
    selection = selection or directory / 'selection.json'
    if not selection.exists():
        selection.write_text(json.dumps({'queries': {'dev': [], 'holdout': []}}) + '\n')
    paths = {'catalog': catalog, 'selection': selection}
    paths.update({name: directory / (name + '.jsonl') for name in DATA_FILES
                  if (directory / (name + '.jsonl')).exists()})
    if ranking is not None:
        paths['ranking-dev'] = ranking
    target = directory / 'fixture-pairing.json'
    content = create_manifest(paths, manifest_path=target, dataset_version='v2',
                              role='constructed_fixture', source_commit='constructed-offline-test')
    target.write_text(json.dumps(content, ensure_ascii=False, indent=2) + '\n')
    return target
