// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it } from 'vitest';
import { CatalogMediaProvider, parseMediaManifest, ProductImageCaption, useProductMedia } from '../src/components/CatalogMedia';
import { ProductImage } from '../src/components/ProductCards';
import publishedManifest from '../../app/presentation/assets/product-media-v1.json';

const manifest = {kind:'illustration',assets:{bag:{url:'/products/catalog-atlas-a-v1.png',cell:0,columns:4,rows:4}},products:{P10000:'bag'},skus:{'P10000-S1':'P10000'}};
it('拒绝外链、越界裁切和未知商品，不能按相似标题猜图',()=>{
 expect(parseMediaManifest({...manifest,assets:{bag:{url:'https://example.com/test.png'}}}).products).toEqual({});
 expect(parseMediaManifest({...manifest,assets:{bag:{...manifest.assets.bag,cell:16}}}).products).toEqual({});
 expect(parseMediaManifest({})).toEqual({assets:{},products:{},skus:{}});
});
it('商品与订单共用文字标识，展示品牌首字母、来源站点和演示标记',async()=>{
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const product={product_id:'P10000',title:'旅行背包',price_major:100,brand:'Trail',category:'旅行装备',source_locale:'en-US',data_provenance:'synthetic'};
 const frozen=JSON.stringify(product);
 const request=async()=>manifest;
 const render=async(p:any)=>act(async()=>root.render(<CatalogMediaProvider request={request}><ProductImage product={p}/><ProductImageCaption product={p}/></CatalogMediaProvider>));
 try {
  await render(product);
  expect(host.querySelector('.product-monogram')?.textContent).toBe('T');
  expect(host.textContent).toContain('US'); expect(host.textContent).toContain('演示数据'); expect(JSON.stringify(product)).toBe(frozen);
  await render({title:product.title,sku_id:'P10000-S1'});
  expect(host.querySelector('.product-identity')).not.toBeNull();
  await render({...product,image_url:'/products/original.png'});
  expect(host.querySelector('img')).toBeNull();
  await render({...product,product_id:'UNKNOWN'});
  expect(host.querySelector('image')).toBeNull();
 } finally { await act(async()=>root.unmount()); host.remove(); }
});

it('公开快照的三件旧商品通过精确 ID 或 SKU 读取背包图集单元',async()=>{
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const request=async()=>publishedManifest;
 function MediaProbe({product}: {product: Parameters<typeof useProductMedia>[0]}) {
  return <output>{JSON.stringify(useProductMedia(product))}</output>;
 }
 try {
  for (const product_id of ['P1003','P1018','P1049']) {
   for (const identity of [{product_id},{sku_id:`${product_id}-S1`}]) {
    await act(async()=>root.render(<CatalogMediaProvider request={request}><MediaProbe product={{...identity,title:'旅行背包',image_url:null,image_kind:'placeholder'}}/></CatalogMediaProvider>));
    expect(JSON.parse(host.textContent || '{}')).toMatchObject({url:'/products/catalog-atlas-a-v1.png',cell:0,columns:4,rows:4,kind:'illustration'});
    expect(host.textContent).toContain('非商品实拍');
   }
  }
 } finally { await act(async()=>root.unmount()); host.remove(); }
});
