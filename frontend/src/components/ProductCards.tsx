import { memo } from "react";
import type { ProductCard } from "../types";
import Icon from "./Icon";
import { selectedSku, selectionKey } from "../lib/candidateWorkspace";
import { ProductBadges } from "./CatalogMedia";
import { categoryProvenanceLabel } from "../lib/productProvenance";
import "./compactProductCards.css";

export { ProductImage } from "./CatalogMedia";

export function money(value: number, currency: string) {
  if (!Number.isFinite(value)) return "待确认";
  try {
    return new Intl.NumberFormat("zh-CN", { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${value} ${currency}`;
  }
}

export type ProductPreview = Pick<ProductCard, "product_id" | "title" | "price_major" | "currency"> & Partial<ProductCard> & { spec?: string };

/** 当前查询、历史批次与工具结果共用同一信息顺序；缺失字段保持未知。 */
export function ProductSummary({ product, onDetail, historical = false }: {
  product: ProductPreview; onDetail?: () => void; historical?: boolean;
}) {
  const primary = product.skus?.find(sku => sku.sku_id === product.default_sku_id) ?? product.skus?.[0];
  const price = primary?.price_major ?? product.source_price_major ?? product.price_major;
  const currency = primary?.currency ?? product.source_currency ?? product.currency;
  const spec = primary?.spec || product.spec;
  const landed = product.landed_price;
  return <div className="product-body">
    <div className="product-topline"><span>{product.brand || product.category || "商品信息"}</span><ProductBadges product={product} /></div>
    {onDetail ? <button className="product-title" onClick={onDetail} title={product.title}>{product.title}</button>
      : <h4 className="product-title" title={product.title}>{product.title}</h4>}
    {product.category && <div className="product-provenance">分类：{categoryProvenanceLabel(product)}</div>}
    {spec && <div className="product-subtitle">{spec}</div>}
    {!!product.highlights?.length && <ul className="product-highlights">{product.highlights.slice(0, 2).map((text, index) => <li key={index}>{text}</li>)}</ul>}
    <div className="product-price-row"><strong className="price">{money(price, currency)}</strong>
      <span className="price-kind">{historical ? "当时单价" : "原币单价"} {currency}</span></div>
    {landed && !landed.unavailable_reason && Number.isFinite(landed.landed_total_major)
      ? <div className="card-landed"><strong>{historical ? "当时到手价" : "到手价"} {money(landed.landed_total_major, landed.currency)}</strong>
        <span>寄往 {landed.ship_to}，含运费与税费</span></div>
      : <div className="card-landed pending">{landed?.unavailable_reason || (historical ? "当时未保存到手价" : "选择目的地后确认到手价")}</div>}
    <div className={`product-stock ${primary?.stock === 0 ? "empty" : ""}`}>
      {primary ? primary.stock > 0 ? `${historical ? "当时库存" : "库存"} ${primary.stock}` : historical ? "当时缺货" : "暂时缺货" : historical ? "当时库存未记录" : "库存待核验"}
    </div>
  </div>;
}

interface ProductCardsProps {
  products: ProductCard[];
  favoriteIds?: Set<string>;
  comparedIds?: Set<string>;
  onFavorite?: (product: ProductCard) => void;
  onCompare?: (product: ProductCard) => void;
  onDetail?: (product: ProductCard) => void;
  historical?: boolean;
  onSaveCandidate?: (product: ProductCard, skuId: string) => void;
  candidateBusy?: boolean;
  candidateKeys?: Set<string>;
}

function ProductCards({ products, favoriteIds, comparedIds, onFavorite, onCompare, onDetail, historical, onSaveCandidate, candidateBusy, candidateKeys }: ProductCardsProps) {
  return <div className="product-grid compact-product-grid">
    {products.map(product => {
      const skuId = selectedSku(product);
      const retained = !!skuId && !!candidateKeys?.has(selectionKey(product.product_id, skuId));
      const saved = favoriteIds?.has(product.product_id) ?? false;
      const selected = comparedIds?.has(product.product_id) ?? false;
      return <article className={`product-card text-product-card ${selected ? "selected" : ""}`} key={selectionKey(product.product_id, product.default_sku_id ?? "")}>
        <div className="product-card-heading"><span>{product.category}</span>
          {onFavorite && <button className={`heart ${saved ? "saved" : ""}`} onClick={() => onFavorite(product)}
            aria-label={`${saved ? "取消收藏" : "收藏"} ${product.title}`} aria-pressed={saved}><Icon name="heart" /></button>}
        </div>
        <ProductSummary product={product} onDetail={onDetail ? () => onDetail(product) : undefined} historical={historical} />
        {(onCompare || onDetail || onSaveCandidate) && <div className="product-footer">
          {onSaveCandidate && <button className="detail-button" disabled={candidateBusy || !skuId || retained}
            onClick={() => skuId && onSaveCandidate(product, skuId)} title={!skuId ? "请在详情中选择明确规格" : undefined}>{retained ? "已保留" : "保留候选"}</button>}
          {onCompare && <label className="compare-check"><input type="checkbox" checked={selected}
            onChange={() => onCompare(product)} aria-label={`将 ${product.title} 加入比较`} />加入比较</label>}
          {onDetail && <button className="detail-button" onClick={() => onDetail(product)}>查看详情<Icon name="arrow" /></button>}
        </div>}
      </article>;
    })}
  </div>;
}

export default memo(ProductCards);
