"""T2 固定抽样边界与划分隔离。"""
from collections import Counter

import pytest

from scripts.esci.build_catalog import preselect
from scripts.esci.prepare import CATEGORIES, PROMPT_VERSION


def classified_pool():
    rows=[]; cache={}
    for locale in ('us','es','jp'):
        for split in ('train','test'):
            for i in range(320):
                row={'locale':locale,'query_id':i+(1000 if split=='test' else 0),'query':str(i),'split':split}
                rows.append(row)
                cache[locale,row['query_id'],PROMPT_VERSION]={**row,'prompt_version':PROMPT_VERSION,
                    'status':'ok','category':CATEGORIES[i%8]}
    return rows,cache


def test_preselection_is_reproducible_disjoint_and_covers_locales():
    rows,cache=classified_pool()
    result=preselect(rows,cache)
    assert result==preselect(list(reversed(rows)),cache)
    assert {name:len(group) for name,group in result.items()}=={'dev':131,'holdout':391,'distractors':131}
    keys={name:{(r['locale'],r['query_id']) for r in group} for name,group in result.items()}
    assert not keys['dev'] & keys['distractors']
    assert not keys['dev'] & keys['holdout']
    for name,group in result.items():
        assert {r['split'] for r in group}==({'test'} if name=='holdout' else {'train'})
        assert {r['category'] for r in group}==set(CATEGORIES)
        assert set(Counter(r['locale'] for r in group))=={'us','es','jp'}


def test_preselection_requires_complete_classification_and_enough_eligible_queries():
    rows,cache=classified_pool(); cache.pop(next(iter(cache)))
    with pytest.raises(ValueError,match='缺失分类'):preselect(rows,cache)
    rows,cache=classified_pool()
    for row in cache.values():row['category']='其他'
    with pytest.raises(ValueError,match='合格查询不足'):preselect(rows,cache)
