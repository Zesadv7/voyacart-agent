"""固定种子、真实文本与交易字段边界。"""
import pytest

from scripts.esci.catalog_records import catalog_record


def product(locale='us', asin='B-EXAMPLE'):
    return {'product_locale':locale,'product_id':asin,'product_title':'Travel Backpack',
            'product_brand':'Original Brand','product_description':'<p>First &amp; second.</p><p>中文原文。</p>',
            'product_bullet_point':'\n'.join(str(i)+'原文'*150 for i in range(8)), 'product_color':'Blue'}


@pytest.mark.parametrize(('locale','currency','market','language'), [('us','USD','US','en'),('es','EUR','EU','es'),('jp','JPY','JP','ja')])
def test_original_text_and_explicit_synthetic_fields(locale,currency,market,language):
    source=product(locale)
    record=catalog_record(source, {'category':'旅行装备','material_tags':['天然纤维']},100000)
    assert record==catalog_record(source, {'category':'旅行装备','material_tags':['天然纤维']},100000)
    assert record['title']==source['product_title']
    assert record['description']=='First & second. 中文原文。'
    assert record['source_language']==language and record['skus'][0]['currency']==currency
    assert market in record['ships_to']
    assert len(record['highlights'])==6
    assert all(len(h['detail'])<=200 for h in record['highlights'][:5])
    assert record['highlights'][-1]=={'label':'颜色','detail':'Blue'}
    assert record['data_provenance']['source']=='esci'
    assert 'skus.stock' in record['data_provenance']['synthetic']
    assert not {'rating_summary','weight_kg','dimensions_cm'} & record.keys()
    if locale=='jp': assert record['skus'][0]['price_major'].is_integer()


def test_fixed_seed_population_stock_shipping_and_optional_text():
    rows=[catalog_record(product(asin=str(i)),{'category':'旅行装备','material_tags':[]},100000+i) for i in range(2000)]
    empty=sum(r['skus'][0]['stock']==0 for r in rows)
    cn=sum('CN' in r['ships_to'] for r in rows)
    assert 160<=empty<=240
    assert 1620<=cn<=1780
    assert all(r['skus'][0]['stock']==0 or 5<=r['skus'][0]['stock']<=200 for r in rows)
    source=product(); source.update(product_color=None,product_description=None,product_brand=None,product_bullet_point=None)
    record=catalog_record(source,{'category':'其他','material_tags':[]},100000)
    assert record['skus'][0]['spec']=='默认' and record['highlights']==[]


def test_invalid_enrichment_fails_without_guessing():
    with pytest.raises(ValueError):
        catalog_record(product(), {'category':'推测类目','material_tags':[]},100000)
    with pytest.raises(ValueError):
        catalog_record(product(), {'category':'旅行装备','material_tags':['未知材质']},100000)
