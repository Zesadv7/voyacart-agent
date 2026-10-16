"""T2 目录记录生成：原文字段保留，演示交易字段由固定种子生成。"""
from __future__ import annotations

from html.parser import HTMLParser
import math
import random

from app.domain.catalog.exchange_rate import ExchangeRateTable
from scripts.esci.prepare import CATEGORIES, SEED

LOCALES = {'us': ('USD', 'US', 'US', 'en'), 'es': ('EUR', 'EU', 'ES', 'es'), 'jp': ('JPY', 'JP', 'JP', 'ja')}
MEDIANS = dict(zip((*CATEGORIES, '其他'), (260, 300, 180, 150, 120, 160, 80, 140, 200)))
MATERIALS = ('合成聚合物', '金属', '天然纤维', '玻璃', '陶瓷', '纸', '天然材料')
DESTINATIONS = ('CN', 'US', 'EU', 'JP', 'SG')


class TextContent(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value: str | None) -> str:
    parser = TextContent()
    parser.feed(value or '')
    return ' '.join(' '.join(parser.parts).split())


def catalog_record(product: dict, enrichment: dict, number: int) -> dict:
    locale, asin = product['product_locale'], product['product_id']
    currency, market, origin, language = LOCALES[locale]
    category, tags = enrichment['category'], enrichment['material_tags']
    if category not in MEDIANS or not isinstance(tags, list) or any(t not in MATERIALS for t in tags) or len(tags) != len(set(tags)):
        raise ValueError(f'商品增强字段无效：{locale}/{asin}')
    title = (product['product_title'] or '').strip()
    if not title:
        raise ValueError(f'商品标题为空：{locale}/{asin}')
    rng = random.Random(f'{SEED}:{locale}:{asin}')
    price_cny = min(5000, max(10, rng.lognormvariate(math.log(MEDIANS[category]), 0.6)))
    price = round(price_cny * ExchangeRateTable().rate('CNY', currency), 0 if currency == 'JPY' else 2)
    stock = 0 if rng.random() < 0.1 else rng.randint(5, 200)
    ships_to = [place for place in DESTINATIONS if place == market or rng.random() < (0.85 if place == 'CN' else 0.3)]
    color = plain_text(product['product_color'])
    bullets = [plain_text(line)[:200] for line in (product['product_bullet_point'] or '').splitlines()]
    highlights = [{'label':'要点', 'detail':text} for text in bullets if text][:5]
    if color:
        highlights.append({'label':'颜色', 'detail':color})
    product_id = f'P{number}'
    return {'product_id':product_id, 'external_product_id':asin, 'canonical_product_id':asin,
        'source_platform':'amazon', 'source_locale':locale, 'source_language':language,
        'title':title, 'brand':(product['product_brand'] or '').strip(),
        'description':plain_text(product['product_description'])[:1000], 'highlights':highlights,
        'category':category, 'material_tags':tags, 'origin_country':origin, 'ships_to':ships_to,
        'skus':[{'sku_id':product_id+'-S1','spec':color or '默认','price_major':price,'currency':currency,'stock':stock}],
        'data_provenance':{'source':'esci','synthetic':['skus.price_major','skus.stock','ships_to'],
                           'derived':['category','material_tags','origin_country']}}
