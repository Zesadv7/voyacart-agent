import { useEffect, useState } from "react";
import { ProductSummary, type ProductPreview } from "./ProductCards";
import { readProducts } from "../lib/commerceClient";
import { readProductProvenance } from "../lib/productProvenance";

export type LoadToolProducts = (runId: string, toolCallId: string) => Promise<unknown>;
type MiniProduct = ProductPreview;
type Result = { products: MiniProduct[]; status: string; totalCount: number | null; truncated: boolean };

function readResult(value: unknown, runId: string, toolCallId: string): Result {
  const data = value as Record<string, unknown> | null;
  if (!data || data.runId !== runId || data.toolCallId !== toolCallId || !Array.isArray(data.products)) throw new Error("结果关联不匹配");
  const products = data.products.flatMap((item): MiniProduct[] => {
    const complete = readProducts([item])[0];
    if (complete) return [complete];
    if (!item || typeof item !== "object" || typeof item.product_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(item.product_id) || typeof item.title !== "string" || !item.title.trim() ||
      typeof item.price_major !== "number" || !Number.isFinite(item.price_major) || item.price_major < 0 || !/^[A-Z]{3}$/.test(item.currency)) return [];
    return [{ product_id: item.product_id, title: item.title.slice(0, 240), price_major: item.price_major, currency: item.currency,
      data_provenance: readProductProvenance(item.data_provenance),
      source_locale: typeof item.source_locale === "string" ? item.source_locale.slice(0, 16) : undefined,
      category: typeof item.category === "string" ? item.category.slice(0, 80) : undefined,
      spec: typeof item.spec === "string" ? item.spec.slice(0, 120) : undefined,
      default_sku_id: typeof item.default_sku_id === "string" ? item.default_sku_id : undefined }];
  }).slice(0, 50);
  return { products, status: String(data.status), totalCount: typeof data.totalCount === "number" ? data.totalCount : null, truncated: data.truncated === true };
}

/** 展开时按调用 ID 读取已保存的工具结果，绝不用最后一批商品替代历史调用。 */
export default function ToolProductResult({ runId, toolCallId, load }: { runId: string; toolCallId: string; load: LoadToolProducts }) {
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [showAll, setShowAll] = useState(false);
  useEffect(() => {
    let disposed = false;
    setError(false); setResult(null);
    void load(runId, toolCallId).then(data => {
      if (!disposed) setResult(readResult(data, runId, toolCallId));
    }).catch(() => { if (!disposed) setError(true); });
    return () => { disposed = true; };
  }, [runId, toolCallId, load, attempt]);
  if (error) return <p className="tool-products-note" role="status">这次调用的商品记录暂时无法读取。<button onClick={() => setAttempt(value => value + 1)}>重试</button></p>;
  if (!result) return <p className="tool-products-note" role="status">正在读取本次调用的商品…</p>;
  if (result.status === "pending") return <p className="tool-products-note">商品结果尚未保存。<button onClick={() => setAttempt(value => value + 1)}>重新读取</button></p>;
  if (!result.products.length) return <p className="tool-products-note">{result.truncated || (result.totalCount ?? 0) > 0
    ? "已保存分页或不完整的商品片段，暂时无法组成完整小卡。"
    : result.status === "ready" ? "本次检索没有返回商品。" : "这次历史调用没有可展示的商品卡。"}</p>;
  const visible = showAll ? result.products : result.products.slice(0, 6);
  return <div className="tool-product-result">
    <div className="product-grid compact-product-grid">{visible.map((product, index) => <article className="product-card text-product-card" key={`${product.product_id}:${product.default_sku_id ?? index}`}>
      <ProductSummary product={product} historical />
    </article>)}</div>
    <div className="tool-products-footer"><span>历史查询快照，交易前请重新核验。</span>
      {result.products.length > 6 && <button onClick={() => setShowAll(value => !value)}>{showAll ? "收起" : `查看全部 ${result.products.length} 件`}</button>}
    </div>
    {result.truncated && <p className="tool-products-note">此处仅展示可读取的商品{result.totalCount !== null ? `（该批共 ${result.totalCount} 件）` : ""}，分页或不完整的记录未展示。</p>}
  </div>;
}
