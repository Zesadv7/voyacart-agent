import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import type { ProductCard } from "../types";
import { hasDemoCommerce } from "../lib/productProvenance";

type Asset = { url: string; cell?: number; columns?: number; rows?: number };
type Manifest = { assets: Record<string, Asset>; products: Record<string, string>; skus: Record<string, string> };
export type MediaProduct = { product_id?: string; sku_id?: string; title: string; brand?: string; category?: string; source_locale?: string; data_provenance?: ProductCard["data_provenance"]; image_url?: string | null; image_kind?: "illustration" | "placeholder"; image_alt?: string };
const empty: Manifest = { assets: {}, products: {}, skus: {} };
const MediaContext = createContext(empty);

export function CatalogMediaProvider({ request, children }: {
  request: (path: string) => Promise<Record<string, unknown>>; children: ReactNode;
}) {
  const [manifest, setManifest] = useState(empty);
  useEffect(() => {
    let disposed = false;
    void request("/product-media").then(data => {
      if (!disposed) setManifest(parseMediaManifest(data));
    }).catch(() => { /* 媒体不可用时保留目录原图或占位，不能阻止购物。 */ });
    return () => { disposed = true; };
  }, [request]);
  return <MediaContext.Provider value={manifest}>{children}</MediaContext.Provider>;
}

export function parseMediaManifest(value: unknown): Manifest {
  if (!value || typeof value !== "object") return empty;
  const data = value as Record<string, unknown>;
  if (data.kind !== "illustration" || !data.assets || !data.products || !data.skus) return empty;
  const assets: Manifest["assets"] = {}, products: Manifest["products"] = {}, skus: Manifest["skus"] = {};
  for (const [key, value] of Object.entries(data.assets)) {
    if (!value || typeof value !== "object") continue;
    const asset = value as Asset;
    // 展示注册表只引用随工程发布的静态图，不接受任意 URL 或无效裁切。
    if (typeof asset.url !== "string" || !/^\/products\/[a-zA-Z0-9_-]+\.png$/.test(asset.url)) continue;
    if (asset.cell !== undefined && (!Number.isInteger(asset.cell) || asset.columns !== 4 || asset.rows !== 4 || asset.cell < 0 || asset.cell >= 16)) continue;
    assets[key] = asset;
  }
  for (const [id, key] of Object.entries(data.products)) if (typeof key === "string" && Object.prototype.hasOwnProperty.call(assets, key)) products[id] = key;
  for (const [id, product] of Object.entries(data.skus)) if (typeof product === "string" && Object.prototype.hasOwnProperty.call(products, product)) skus[id] = product;
  return { assets, products, skus };
}

export function useProductMedia(product: MediaProduct) {
  const manifest = useContext(MediaContext);
  const id = product.product_id ?? (product.sku_id ? manifest.skus[product.sku_id] : undefined);
  const asset = id && Object.prototype.hasOwnProperty.call(manifest.products, id) ? manifest.assets[manifest.products[id]] : undefined;
  // 有真实目录图片时优先保留；注册表只补缺失媒体，不更改商品事实。
  return product.image_url ? { url: product.image_url, kind: product.image_kind, alt: product.image_alt || product.title, cell: undefined, columns: undefined, rows: undefined }
    : asset ? { ...asset, kind: "illustration" as const, alt: `${product.title}：同类商品 AI 示意图，非商品实拍，具体规格以文字为准` }
    : { url: null, kind: "placeholder" as const, alt: `${product.title}：暂无商品图片`, cell: undefined, columns: undefined, rows: undefined };
}
export function ProductImageCaption({ product }: { product: MediaProduct }) {
  return <>{isDemoProduct(product) ? "价格、库存与配送为演示数据" : "商品信息以文字为准"}</>;
}

export function isDemoProduct(product: MediaProduct) {
  return hasDemoCommerce(product);
}

export function ProductBadges({ product }: { product: MediaProduct }) {
  const site = product.source_locale?.split(/[-_]/).pop()?.toUpperCase();
  return <span className="product-labels">
    {site && <span className="product-site" aria-label={`来源站点 ${site}`}>{site}</span>}
    {isDemoProduct(product) && <span className="product-demo">演示数据</span>}
  </span>;
}

export function ProductImage({ product, className = "" }: { product: MediaProduct; className?: string }) {
  return <div className={`product-identity ${className}`} role="img" aria-label={`${product.brand || product.title} 商品标识`}>
    <strong className="product-monogram">{Array.from(product.brand || product.category || "商品")[0].toUpperCase()}</strong>
    <span className="product-identity-copy"><ProductBadges product={product} />
      {product.category && <span>{product.category}</span>}
    </span>
  </div>;
}
