"""T2 缓存齐全后生成正式目录、ESCI 评测集及人工抽检表。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import gzip
import json
from pathlib import Path
import random
from statistics import median

from app.domain.catalog.exchange_rate import ExchangeRateTable
from scripts.esci.build_catalog import TARGETS, stratified
from scripts.esci.catalog_records import CATEGORIES, DESTINATIONS, MATERIALS, SEED, catalog_record
from scripts.esci.prepare import json_line


# 商品级模型增强，或增强前以查询预分类众数代理的类目（材质为空）。
CATEGORY_SOURCES=('product-enrich-batch-v1','product-enrich-v1','query-class-proxy-v1')


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def selected_inputs(prepared: Path, enrichment_path: Path) -> tuple:
    """目录定稿与翻译输入共用 Exact 众数判定及最终查询抽样。"""
    plan=json.loads((prepared/'selection-plan.json').read_text())
    if plan['seed']!=SEED or plan['targets']!=TARGETS:
        raise ValueError('预选清单与当前固定参数不同')
    products={(p['product_locale'],p['product_id']):p for p in load_rows(prepared/'products.jsonl')}
    enriched={}
    for row in load_rows(enrichment_path):
        key=row['locale'],row['asin']
        tags=row.get('material_tags')
        if (row['prompt_version'] not in CATEGORY_SOURCES or key in enriched
            or row.get('status') not in (None,'ok') or row.get('category') not in (*CATEGORIES,'其他')
            or not isinstance(tags,list) or any(t not in MATERIALS for t in tags) or len(tags)!=len(set(tags))):
            raise ValueError('商品增强缓存版本、唯一性、状态或字段无效')
        enriched[key]=row
    examples=defaultdict(list)
    for row in load_rows(prepared/'examples.jsonl'):
        examples[row['product_locale'],row['query_id']].append(row)
    if set(products)-enriched.keys():
        raise ValueError('商品增强缓存仍有缺失')
    if len({row['prompt_version'] for row in enriched.values()})!=1:
        raise ValueError('同一目录只能使用一种类目来源')
    selection={}
    rejected=[]
    for dataset,preselected in plan['queries'].items():
        qualified=[]
        for query in preselected:
            linked=examples[query['locale'],query['query_id']]
            labels=[enriched[r['product_locale'],r['product_id']]['category'] for r in linked if r['esci_label']=='E']
            counts=Counter(labels)
            category=sorted(counts,key=lambda c:(-counts[c],c))[0] if counts else '其他'
            if category in CATEGORIES:
                qualified.append({**query,'category':category})
            else:
                rejected.append({'locale':query['locale'],'query_id':query['query_id'],'reason':'no_exact_or_other_category'})
        selected=[]
        for locale,ratio in (('us',2),('es',1),('jp',1)):
            selected.extend(stratified([r for r in qualified if r['locale']==locale],TARGETS[dataset]*ratio//4))
        selection[dataset]=selected
    return products,enriched,examples,selection,rejected


def chinese_selection(holdout: list[dict]) -> list[dict]:
    return [row for locale,quota in (('us',100),('es',50),('jp',50))
        for row in stratified([r for r in holdout if r['locale']==locale],quota)]


def translation_inputs(prepared: Path, enrichment_path: Path) -> list[dict]:
    _,_,_,selection,_=selected_inputs(prepared,enrichment_path)
    return [{key:row[key] for key in ('locale','query_id','query')}
        for row in chinese_selection(selection['holdout'])]


def finalize(prepared: Path, enrichment_path: Path, translations_path: Path, output: Path, version: str) -> dict:
    products,enriched,examples,selection,rejected=selected_inputs(prepared,enrichment_path)
    wanted={(r['product_locale'],r['product_id']) for qs in selection.values() for q in qs for r in examples[q['locale'],q['query_id']]}
    records=[catalog_record(products[key],enriched[key],100000+i) for i,key in enumerate(sorted(wanted))]
    for record in records:
        record['data_provenance']['category_source']=enriched[record['source_locale'],record['external_product_id']]['prompt_version']
    ids={(r['source_locale'],r['external_product_id']):r['product_id'] for r in records}
    by_id={r['product_id']:r for r in records}
    output.mkdir(parents=True,exist_ok=False)
    data=''.join(json_line(row) for row in records).encode()
    with (output/f'catalog-esci-{version}.jsonl.gz').open('wb') as stream:
        with gzip.GzipFile(filename='',mode='wb',fileobj=stream,mtime=0) as zipped:
            zipped.write(data)
    evaluation=output/'eval'; evaluation.mkdir()
    rankings={}; retrieval={}
    for dataset in ('dev','holdout'):
        rankings[dataset]=[]; retrieval[dataset]=[]; constraints=[]
        empty_queries={(q['locale'],q['query_id']) for q in random.Random(SEED).sample(selection[dataset],len(selection[dataset])//10)}
        for q in selection[dataset]:
            candidates=[{'product_id':ids[r['product_locale'],r['product_id']],'label':r['esci_label']}
                for r in examples[q['locale'],q['query_id']]]
            base={k:q[k] for k in ('query_id','query','locale','category')}
            rankings[dataset].append({**base,'candidates':candidates})
            retrieval[dataset].append({**base,'labels':{r['product_id']:r['label'] for r in candidates}})
            exact=[by_id[r['product_id']] for r in candidates if r['label']=='E']
            rng=random.Random(f'{SEED}:{dataset}:{q["locale"]}:{q["query_id"]}')
            destination=rng.choice(sorted({d for p in exact for d in p['ships_to']}))
            prices=[p['skus'][0]['price_major']*ExchangeRateTable().rate(p['skus'][0]['currency'],'CNY') for p in exact]
            empty_case=(q['locale'],q['query_id']) in empty_queries
            ceiling=0 if empty_case else round(median(prices),2)
            relevant=[p['product_id'] for p,price in zip(exact,prices) if destination in p['ships_to'] and p['skus'][0]['stock']>0 and price<=ceiling]
            constraints.append({**base,'ship_to':destination,'price_max_major':ceiling,'target_currency':'CNY',
                'in_stock':True,'expected_empty':empty_case,'relevant_product_ids':relevant})
        for kind,rows in (('ranking',rankings[dataset]),('retrieval',retrieval[dataset]),('constraints',constraints)):
            (evaluation/f'{kind}-{dataset}.jsonl').write_text(''.join(json_line(row) for row in rows))
    translations={}
    for row in load_rows(translations_path):
        key=row['locale'],row['query_id']
        if (row['prompt_version']!='translate-zh-v1' or key in translations or row.get('status') not in (None,'ok')
            or not isinstance(row.get('translation'),str) or not row['translation'].strip()):
            raise ValueError('中文译文缓存版本、唯一性或内容无效')
        translations[key]=row
    chinese=[]
    for row in chinese_selection(retrieval['holdout']):
        key=row['locale'],row['query_id']
        if key not in translations or translations[key]['query']!=row['query']:
            raise ValueError('中文译文缓存缺失或原文已变化')
        chinese.append({**row,'original_query':row['query'],'query':translations[key]['translation']})
    (evaluation/'crosslingual-zh.jsonl').write_text(''.join(json_line(row) for row in chinese))
    audit=output/'audit';audit.mkdir()
    rng=random.Random(SEED)
    with (audit/'category-sample-200.csv').open('w',newline='') as stream:
        fields=['product_id','locale','asin','title','category','material_tags','正确','修正类目','备注']
        writer=csv.DictWriter(stream,fields);writer.writeheader()
        for p in rng.sample(records,min(200,len(records))):
            writer.writerow({'product_id':p['product_id'],'locale':p['source_locale'],'asin':p['external_product_id'],
                'title':p['title'],'category':p['category'],'material_tags':'、'.join(p['material_tags'])})
    with (audit/'translation-sample-50.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,['locale','query_id','original_query','translation','正确','修正译文','备注']);writer.writeheader()
        for row in random.Random(SEED).sample(chinese,50):
            writer.writerow({'locale':row['locale'],'query_id':row['query_id'],'original_query':row['original_query'],'translation':row['query']})
    selected={'seed':SEED,'targets':TARGETS,'queries':selection,'rejected':rejected}
    (output/f'selection-{version}.json').write_text(json.dumps(selected,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    report={'queries':{k:len(v) for k,v in selection.items()},'products':len(records),
        'categories':dict(Counter(r['category'] for r in records)), 'locales':dict(Counter(r['source_locale'] for r in records)),
        'out_of_stock_ratio':sum(p['skus'][0]['stock']==0 for p in records)/len(records),
        'destination_coverage':{d:sum(d in p['ships_to'] for p in records)/len(records) for d in DESTINATIONS},
        'chinese_queries':len(chinese),'model_calls':0,'manual_audit':'pending',
        'category_source':records[0]['data_provenance']['category_source']}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared',type=Path,required=True)
    parser.add_argument('--enrichment',type=Path,required=True)
    parser.add_argument('--translations',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--version',required=True,help='目录与抽样文件的版本号，如 v2')
    args=parser.parse_args()
    print(json.dumps(finalize(args.prepared,args.enrichment,args.translations,args.output,args.version),ensure_ascii=False))


if __name__=='__main__':main()
