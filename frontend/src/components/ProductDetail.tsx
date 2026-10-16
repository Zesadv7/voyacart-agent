import { useState } from "react";
import type { ProductCard } from "../types";
import Icon from "./Icon";
import { ProductImageCaption } from "./CatalogMedia";
import Modal from "./Modal";
import { money, ProductImage } from "./ProductCards";
import { categoryProvenanceLabel, hasDemoCommerce, materialProvenanceLabel, originProvenance, sourceProcessingLabel } from "../lib/productProvenance";
import "./productDetail.css";

export default function ProductDetail({
  product,
  busy,
  onClose,
  onCompare,
  onAsk,
  onPrepare,
  onSaveCandidate,
  candidateBusy = false,
  historical = false,
}: {
  product: ProductCard;
  busy: boolean;
  onClose: () => void;
  onCompare?: (product: ProductCard) => void;
  onSaveCandidate?: (product: ProductCard, skuId: string) => void;
  candidateBusy?: boolean;
  historical?: boolean;
  onAsk: (query: string) => void;
  onPrepare: (product: ProductCard, skuId: string) => void;
}) {
  const [skuId, setSkuId] = useState(
    product.default_sku_id || "",
  );
  const sku = product.skus.find((item) => item.sku_id === skuId),
    landed = product.landed_price;
  const canShowLanded =
    skuId === product.default_sku_id &&
    landed &&
    !landed.unavailable_reason &&
    Number.isFinite(landed.landed_total_major);
  const origin = originProvenance(product);
  const sourceProcessing = sourceProcessingLabel(product);
  return (
    <Modal title={`${product.title} 商品详情`} drawer onClose={onClose}>
      <section className="product-detail" aria-label="商品信息与规格">
        <div className="detail-heading">商品详情</div>
        <div className="detail-summary">
          <figure className="detail-image">
            <div className="detail-thumbnail">
              <ProductImage product={product} />
            </div>
            <figcaption>
              <ProductImageCaption product={product} />
            </figcaption>
          </figure>
          <div className="detail-summary-copy">
            <div className="detail-brand">{product.brand || product.category}</div>
            <h2>{product.title}</h2>
            <div className="detail-price-row">
              <strong className="detail-price">
                {money(
                  sku?.price_major ?? product.price_major,
                  sku?.currency ?? product.currency,
                )}
              </strong>
              <span className="detail-price-kind">{historical ? "保存时规格商品价" : "当前规格商品价"}</span>
            </div>
          </div>
        </div>
        {product.description && <p className="detail-description">{product.description}</p>}
        {!!product.highlights.length && <ul className="detail-highlights">{product.highlights.map((text, index) => <li key={index}>{text}</li>)}</ul>}
        {product.skus.length > 0 && (
          <fieldset className="sku-picker">
            <legend>选择规格</legend>
            {product.skus.map((item) => (
              <label
                key={item.sku_id}
                className={skuId === item.sku_id ? "chosen" : ""}
              >
                <input
                  type="radio"
                  name="product-sku"
                  value={item.sku_id}
                  checked={skuId === item.sku_id}
                  onChange={() => setSkuId(item.sku_id)}
                />
                <span>{item.spec}</span>
                <small>
                  {money(item.price_major, item.currency)} / {item.stock > 0 ? `库存 ${item.stock}` : "暂无库存"}
                </small>
              </label>
            ))}
          </fieldset>
        )}
        <dl className="detail-specs">
          <div>
            <dt>商品编号</dt>
            <dd>{product.product_id}</dd>
          </div>
          {sku && (
            <div>
              <dt>规格编号</dt>
              <dd>{sku.sku_id}</dd>
            </div>
          )}
          <div>
            <dt>商品分类</dt>
            <dd>{product.category}<small className="detail-provenance">{categoryProvenanceLabel(product)}</small></dd>
          </div>
          <div>
            <dt>{origin.label}</dt>
            <dd>{origin.value}<small className="detail-provenance">{origin.note}</small></dd>
          </div>
          <div>
            <dt>材质</dt>
            <dd>{product.material_tags?.join(" / ") || "未提供"}<small className="detail-provenance">{materialProvenanceLabel(product)}</small></dd>
          </div>
          {(product.ships_to?.length ?? 0) > 0 && (
            <div>
              <dt>配送地区</dt>
              <dd>{product.ships_to?.join(" / ")}</dd>
            </div>
          )}
          {product.external_product_id && <div><dt>平台商品编号</dt><dd>{product.external_product_id}</dd></div>}
          {product.weight_kg !== undefined && product.weight_kg > 0 && <div><dt>重量</dt><dd>{product.weight_kg} kg</dd></div>}
          {product.dimensions_cm && Object.keys(product.dimensions_cm).length > 0 && <div><dt>尺寸</dt><dd>
            {Object.entries(product.dimensions_cm).map(([key, value]) => `${({length: "长", width: "宽", height: "高"} as Record<string, string>)[key]} ${value} cm`).join(" / ")}
          </dd></div>}
          {product.rating_summary && (
            <div>
              <dt>{product.rating_is_live ? "商品评分" : "评分样例"}</dt>
              <dd>
                ★ {product.rating_summary.average} ·{" "}
                {product.rating_summary.review_count} 条
              </dd>
            </div>
          )}
        </dl>
        {canShowLanded ? (
          <div className="landed-detail-panel">
            <strong>
              到手价 {money(landed.landed_total_major, landed.currency)}
            </strong>
            <span>
              小计 {money(landed.subtotal_major, landed.currency)} + 运费{" "}
              {money(landed.freight_major, landed.currency)} + 关税{" "}
              {money(landed.tariff_major, landed.currency)}
            </span>
            <small>配送至 {landed.ship_to} · 对应当前默认规格报价</small>
          </div>
        ) : (
          <p className="detail-quote-note" role="status">
            {skuId !== product.default_sku_id
              ? "已更换规格，到手价需重新查询。"
              : landed?.unavailable_reason || "到手价待目的地与规格确认。"}
          </p>
        )}
        {sourceProcessing && <p className="detail-note">{sourceProcessing}</p>}
        <p className="detail-note">材质标签不能用于过敏、安全用途或无塑料保证；请核验原始商品说明。</p>
        <p className="detail-note">
          {hasDemoCommerce(product)
            ? "价格、库存与配送为演示数据；到手价使用模拟交易规则，不构成真实报价、商家库存或履约保证。"
            : "价格、库存与配送信息来自目录快照，来源与时效待核验。"}
        </p>
        <div className="detail-actions">
          {onSaveCandidate && <button className="detail-compare" disabled={candidateBusy || !sku} onClick={() => sku && onSaveCandidate(product, sku.sku_id)}>保留当前规格候选</button> }
          <button
            className="detail-verify"
            disabled={busy}
            onClick={() => {
              onAsk(
                `请进一步核对「${product.title}」（product_id=${product.product_id}${sku ? `，sku_id=${sku.sku_id}，规格=${sku.spec}` : ""}）的当前库存与到手价。`,
              );
              onClose();
            }}
          >
            <Icon name="chat" />
            {busy ? "正在处理中" : "核验库存与报价"}
          </button>
          <button
            className="detail-prepare"
            disabled={busy || historical || !sku || sku.stock <= 0}
            onClick={() => sku && onPrepare(product, sku.sku_id)}
          >
            准备下单意向
          </button>
          {onCompare && <button
            className="detail-compare"
            aria-label="用当前规格比较"
            onClick={() =>
              onCompare(
                sku
                  ? {
                      ...product,
                      default_sku_id: sku.sku_id,
                      price_major: sku.price_major,
                      currency: sku.currency,
                      landed_price: canShowLanded ? landed : undefined,
                    }
                  : product,
              )
            }
          >
            <Icon name="compare" />
            比较
          </button>}
        </div>
      </section>
    </Modal>
  );
}
