import { useEffect, useRef, useState } from "react";
import type { ProductCard } from "../types";
import type { Candidate, CandidateState, CandidateVerification, CandidateWorkspaceController } from "../lib/candidateWorkspace";
import { money, ProductSummary } from "./ProductCards";
import Modal from "./Modal";
import "./candidateWorkspace.css";

type Props = CandidateState & {
  controller: CandidateWorkspaceController;
  onPrepare: (product: ProductCard, skuId: string, country: string) => void;
  onResearch: (query: string) => void;
};
export default function CandidateWorkspace({controller, workspace, loading, busy, error, warning, pending, conflict, retryable, onPrepare, onResearch}: Props) {
  const [checking, setChecking] = useState<Candidate | null>(null);
  const selected = workspace?.comparison_ids ?? [];
  const locked = busy || !!pending || !workspace;
  return <section className="candidate-workspace" aria-label="已保留候选" aria-busy={loading || busy}>
    <div className="candidate-heading"><div><h2>已保留候选 <span>{workspace ? `${workspace.items.length} / ${workspace.limits.candidates}` : ""}</span></h2>
      <p>按商品与规格保存，换搜索或刷新后仍保留。比较最多 3 项。</p></div>
      <button type="button" disabled={loading || busy} onClick={() => void controller.refresh()}>{loading ? "正在读取…" : "刷新候选"}</button></div>
    <p className="candidate-historical-note">以下都是保存时信息。核验当前目录后才能从候选准备意向单；生成确认时会再次核对，尚未支付或安排发货。</p>
    {warning && <p role="status" className="identity-warning">{warning}</p>}
    {error && <div className="candidate-error" role="alert"><p>{error}</p>
      {pending && <div className="candidate-actions">
        {retryable && <button disabled={busy || loading} onClick={() => void controller.retry()}>{conflict ? "按最新版本重试" : "使用同一请求重试"}</button>}
        <button disabled={busy} onClick={controller.dismiss}>放弃待处理操作并刷新</button>
      </div>}</div>}
    {workspace && !workspace.items.length && <p className="candidate-empty">在这次结果或历史商品中选择明确规格，点击“保留候选”。</p>}
    <div className="candidate-grid">{workspace?.items.map(candidate => <article className="candidate-card" key={candidate.candidate_id}>
      <ProductSummary product={candidate.snapshot} historical />
      <div className="candidate-source">保存于 {new Date(candidate.saved_at).toLocaleString("zh-CN")}<br />
        来源记录 {candidate.source.run_id} · {candidate.sku_id}<br />
        {candidate.identity_status === "bound" ? "目录身份已绑定；价格与库存待重新核验" : "历史来源身份未知，请重新查询后购买"}</div>
      <div className="candidate-actions"><label><input type="checkbox" checked={selected.includes(candidate.candidate_id)} disabled={locked}
        aria-label={`比较 ${candidate.snapshot.title} ${candidate.sku_id}`} onChange={() => void controller.mutate({kind: "compare", candidate_ids: selected.includes(candidate.candidate_id)
          ? selected.filter(id => id !== candidate.candidate_id) : [...selected, candidate.candidate_id]})} />加入比较</label>
        <button disabled={busy} onClick={() => setChecking(candidate)} aria-label={`核验候选 ${candidate.snapshot.title} ${candidate.sku_id}`}>核验当前目录</button>
        <button disabled={locked} onClick={() => void controller.mutate({kind: "remove", candidate_id: candidate.candidate_id})} aria-label={`移除候选 ${candidate.snapshot.title} ${candidate.sku_id}`}>移除</button></div>
    </article>)}</div>
    {checking && workspace?.items.some(c => c.candidate_id === checking.candidate_id) && <CandidateVerificationDialog
      key={`${checking.candidate_id}:${workspace.revision}`} candidate={checking} controller={controller}
      onClose={() => setChecking(null)} onPrepare={(product, skuId, country) => { setChecking(null); onPrepare(product, skuId, country); }} onResearch={onResearch} />}
  </section>;
}

function CandidateVerificationDialog({candidate, controller, onClose, onPrepare, onResearch}: {
  candidate: Candidate; controller: CandidateWorkspaceController; onClose: () => void;
  onPrepare: Props["onPrepare"]; onResearch: Props["onResearch"];
}) {
  const [country, setCountry] = useState("CN"), [currency, setCurrency] = useState("CNY");
  const [result, setResult] = useState<CandidateVerification | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => () => { ++generation.current; }, []);
  const change = (field: "country" | "currency", value: string) => {
    ++generation.current; setBusy(false); setResult(null); setError("");
    if (field === "country") setCountry(value.toUpperCase()); else setCurrency(value);
  };
  const check = async () => {
    if (busy || !/^[A-Z]{2}$/.test(country)) return;
    const current = ++generation.current; setBusy(true); setResult(null); setError("");
    try {
      const next = await controller.verify(candidate, currency, country);
      if (generation.current !== current) return;
      if (next) setResult(next); else setError("候选集合已变化，请关闭并重新核验。");
    } catch (e) { if (generation.current === current) setError(e instanceof Error ? e.message : "核验失败，请重试。"); }
    finally { if (generation.current === current) setBusy(false); }
  };
  const historical = candidate.snapshot.skus.find(s => s.sku_id === candidate.sku_id)!;
  const fresh = result?.product?.skus.find(s => s.sku_id === candidate.sku_id);
  return <Modal title="核验候选当前目录" onClose={onClose}>
    <h2>核验当前目录</h2><p>{candidate.snapshot.title} · {historical.spec} · {candidate.sku_id}</p>
    <p>保存时：{money(historical.price_major, historical.currency)} / 库存 {historical.stock}。保存记录不会被本次核验覆盖。</p>
    <div className="candidate-verification-inputs"><label>配送国家或地区代码<input aria-label="核验配送地区" value={country} maxLength={2}
      onChange={e => change("country", e.target.value)} placeholder="CN" /></label>
      <label>显示币种<select aria-label="核验显示币种" value={currency} onChange={e => change("currency", e.target.value)}>
        {["CNY", "USD", "EUR", "JPY", "GBP"].map(code => <option key={code}>{code}</option>)}</select></label></div>
    <button disabled={busy || !/^[A-Z]{2}$/.test(country)} onClick={() => void check()}>{busy ? "正在核验…" : "重新核验"}</button>
    {error && <p role="alert">{error}</p>}
    {result && <div role="status" className="candidate-verification-result"><p>本地目录核验于 {new Date(result.checked_at).toLocaleString("zh-CN")}</p>
      <p>{result.reason}</p>{fresh && <p>当前目录：{money(fresh.price_major, fresh.currency)} / 库存 {fresh.stock}。最终金额以确认单为准。</p>}
      <p>本次核验不预留库存，不代表实时商家报价。生成确认时会再次核对。</p>
      {result.status === "available" && result.product && <button onClick={() => onPrepare(result.product!, candidate.sku_id, country)}>填写下单意向</button>}
    </div>}
    {(candidate.identity_status === "unknown" || (result && ["identity_mismatch", "unknown_identity", "terms_changed", "unavailable"].includes(result.status))) &&
      <button onClick={() => { onResearch(`请重新查询商品 product_id=${candidate.product_id}，sku_id=${candidate.sku_id}，核对当前商品身份与规格。`); onClose(); }}>重新查询该商品</button>}
  </Modal>;
}
