import type { CommerceSnapshot, ProductCard } from "../types";
import ProductCards from "./ProductCards";

/** 历史报价只读展示，不能用旧库存、旧金额直接发起交易。 */
export default function ProductHistory({batches = [], onSaveCandidate, onDetail, candidateBusy, candidateKeys}: {batches: CommerceSnapshot["productHistory"];
  onSaveCandidate?: (product: ProductCard, skuId: string, runId: string) => void;
  onDetail?: (product: ProductCard, runId: string) => void; candidateBusy?: boolean; candidateKeys?: Set<string>}) {
  if (!batches.length) return null;
  return <details className="historical-products">
    <summary>历史商品记录 · {batches.length} 轮</summary>
    <p className="results-footnote">以下为当时保存的商品信息。价格、库存和配送条件可能变化，继续购买前请重新核验。</p>
    {batches.map(batch => <section key={batch.runId} aria-label="历史商品批次">
      <h3>{new Date(batch.updatedAt).toLocaleString("zh-CN")} · {batch.products.length} 件</h3>
      <ProductCards products={batch.products} historical candidateBusy={candidateBusy} candidateKeys={candidateKeys}
        onSaveCandidate={onSaveCandidate ? (product, skuId) => onSaveCandidate(product, skuId, batch.runId) : undefined}
        onDetail={onDetail ? product => onDetail(product, batch.runId) : undefined} />
    </section>)}
  </details>;
}
