import type { ProductCard } from "../types";
import { readProducts } from "./commerceClient";

export interface Candidate {
  candidate_id: string;
  product_id: string;
  sku_id: string;
  saved_at: string;
  source: { kind: "run"; run_id: string; captured_cursor: number; captured_at: string };
  identity_status: "bound" | "unknown";
  quote_status: "historical";
  snapshot: ProductCard;
}
export interface CandidateWorkspace {
  schema_version: 1;
  buyer_id: string;
  session_id: string;
  revision: number;
  items: Candidate[];
  comparison_ids: string[];
  limits: { candidates: number; comparison: number };
}
export type CandidateIntent =
  | { kind: "add"; source: { kind: "run"; run_id: string; product_id: string; sku_id: string } }
  | { kind: "remove"; candidate_id: string }
  | { kind: "compare"; candidate_ids: string[] };
export type CandidateAction = CandidateIntent & { request_id: string; expected_revision: number };
export interface CandidateVerification {
  candidate_id: string;
  session_id: string;
  workspace_revision: number;
  checked_at: string;
  source: "local_catalog";
  status: "available" | "out_of_stock" | "unavailable" | "identity_mismatch" | "unknown_identity" | "terms_changed";
  reason: string;
  product?: ProductCard;
}
export type WorkspaceRequest = (path: string, method?: string, body?: Record<string, unknown>) => Promise<Record<string, unknown>>;
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const id = (v: unknown): v is string => typeof v === "string" && !!v.trim() && v.length <= 256;
const count = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0;
const date = (v: unknown): v is string => typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v) && Number.isFinite(Date.parse(v));
const invalid = () => new Error("候选响应不完整或身份不匹配，请刷新后重试；已有候选不会被清空。");
export const selectionKey = (productId: string, skuId: string) => JSON.stringify([productId, skuId]);
export function selectedSku(product: ProductCard): string | null {
  return product.default_sku_id && product.skus.some(s => s.sku_id === product.default_sku_id) ? product.default_sku_id : null;
}
export function readCandidateWorkspace(value: unknown, buyerId: string, sessionId: string): CandidateWorkspace {
  if (!record(value) || value.schema_version !== 1 || value.buyer_id !== buyerId || value.session_id !== sessionId
    || !count(value.revision) || !Array.isArray(value.items) || !Array.isArray(value.comparison_ids)
    || !record(value.limits) || value.limits.candidates !== 30 || value.limits.comparison !== 3
    || value.items.length > 30 || value.comparison_ids.length > 3) throw invalid();
  const ids = new Set<string>(), selections = new Set<string>();
  const items = value.items.map((raw): Candidate => {
    if (!record(raw) || !id(raw.candidate_id) || !id(raw.product_id) || !id(raw.sku_id) || !date(raw.saved_at)
      || !record(raw.source) || raw.source.kind !== "run" || !id(raw.source.run_id) || !count(raw.source.captured_cursor)
      || !date(raw.source.captured_at) || typeof raw.identity_status !== "string" || !["bound", "unknown"].includes(raw.identity_status)
      || raw.quote_status !== "historical") throw invalid();
    const snapshot = readProducts([raw.snapshot])[0];
    if (!snapshot || snapshot.product_id !== raw.product_id || selectedSku(snapshot) !== raw.sku_id
      || new Set(snapshot.skus.map(s => s.sku_id)).size !== snapshot.skus.length
      || ids.has(raw.candidate_id) || selections.has(selectionKey(raw.product_id, raw.sku_id))) throw invalid();
    ids.add(raw.candidate_id); selections.add(selectionKey(raw.product_id, raw.sku_id));
    return { candidate_id: raw.candidate_id, product_id: raw.product_id, sku_id: raw.sku_id, saved_at: raw.saved_at,
      source: {kind: "run", run_id: raw.source.run_id, captured_cursor: raw.source.captured_cursor, captured_at: raw.source.captured_at},
      identity_status: raw.identity_status as Candidate["identity_status"], quote_status: "historical", snapshot };
  });
  if (!value.comparison_ids.every(v => id(v) && ids.has(v)) || new Set(value.comparison_ids).size !== value.comparison_ids.length) throw invalid();
  return { schema_version: 1, buyer_id: buyerId, session_id: sessionId, revision: value.revision, items,
    comparison_ids: value.comparison_ids as string[], limits: {candidates: 30, comparison: 3} };
}
/** 同版本也不覆盖：一次提交的旧重放不能复活后来删除的候选。 */
export function mergeCandidateWorkspace(current: CandidateWorkspace | null, next: CandidateWorkspace): CandidateWorkspace {
  if (current && (current.buyer_id !== next.buyer_id || current.session_id !== next.session_id)) throw invalid();
  return current && current.revision >= next.revision ? current : next;
}
export function readCandidateVerification(value: unknown, candidate: Candidate, sessionId: string): CandidateVerification {
  if (!record(value) || value.candidate_id !== candidate.candidate_id || value.session_id !== sessionId
    || !count(value.workspace_revision) || !date(value.checked_at) || value.source !== "local_catalog"
    || typeof value.status !== "string" || !["available", "out_of_stock", "unavailable", "identity_mismatch", "unknown_identity", "terms_changed"].includes(value.status)
    || typeof value.reason !== "string") throw invalid();
  const product = value.product === undefined ? undefined : readProducts([value.product])[0];
  if (value.status === "available" && (candidate.identity_status !== "bound" || !product || product.product_id !== candidate.product_id
    || selectedSku(product) !== candidate.sku_id || new Set(product.skus.map(s => s.sku_id)).size !== product.skus.length || !product.skus.some(s => s.sku_id === candidate.sku_id && s.stock > 0))) throw invalid();
  return { candidate_id: candidate.candidate_id, session_id: sessionId, workspace_revision: value.workspace_revision,
    checked_at: value.checked_at, source: "local_catalog", status: value.status as CandidateVerification["status"], reason: value.reason,
    // 失败核验绝不向执行入口传递商品。
    ...(value.status === "available" ? {product} : {}) };
}
export function candidateActionRequest(base: string, action: CandidateAction) {
  const body = {request_id: action.request_id, expected_revision: action.expected_revision};
  if (action.kind === "add") return {path: base, method: "POST", body: {...body, source: {kind: "run", run_id: action.source.run_id, product_id: action.source.product_id, sku_id: action.source.sku_id}}};
  if (action.kind === "remove") return {path: `${base}/${encodeURIComponent(action.candidate_id)}`, method: "DELETE", body};
  return {path: `${base}/comparison`, method: "PUT", body: {...body, candidate_ids: action.candidate_ids}};
}
export interface CandidateState {
  workspace: CandidateWorkspace | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  warning: string | null;
  pending: CandidateAction | null;
  conflict: boolean;
  retryable: boolean;
}
interface PendingStorage { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void }
function readPending(raw: string | null): CandidateAction | null {
  if (!raw) return null;
  const v: unknown = JSON.parse(raw);
  if (!record(v) || !id(v.request_id) || !count(v.expected_revision)) throw invalid();
  const common = {request_id: v.request_id, expected_revision: v.expected_revision};
  if (v.kind === "remove" && id(v.candidate_id)) return {...common, kind: "remove", candidate_id: v.candidate_id};
  if (v.kind === "compare" && Array.isArray(v.candidate_ids) && v.candidate_ids.length <= 3 && v.candidate_ids.every(id)
    && new Set(v.candidate_ids).size === v.candidate_ids.length) return {...common, kind: "compare", candidate_ids: v.candidate_ids};
  if (v.kind === "add" && record(v.source) && v.source.kind === "run" && id(v.source.run_id) && id(v.source.product_id) && id(v.source.sku_id))
    return {...common, kind: "add", source: {kind: "run", run_id: v.source.run_id, product_id: v.source.product_id, sku_id: v.source.sku_id}};
  throw invalid();
}
/** 每个挂载实例有独立代次；同一 session 再次进入也不能接收旧实例的回包。 */
export class CandidateWorkspaceController {
  private state: CandidateState = {workspace: null, loading: false, busy: false, error: null, warning: null, pending: null, conflict: false, retryable: true};
  private listeners = new Set<() => void>();
  private generation = 0;
  private readGeneration = 0;
  private active = false;
  readonly base: string;
  private storageKey: string;
  constructor(readonly buyerId: string, readonly sessionId: string, private request: WorkspaceRequest,
    private storage?: PendingStorage, private timeoutMs = 15000) {
    this.base = `/ag-ui/sessions/${encodeURIComponent(sessionId)}/candidates`;
    this.storageKey = `voyacart.candidate.pending.v1:${encodeURIComponent(buyerId)}:${encodeURIComponent(sessionId)}`;
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(patch: Partial<CandidateState>) { this.state = {...this.state, ...patch}; this.listeners.forEach(fn => fn()); }
  private current(generation: number) { return this.active && generation === this.generation; }
  private async bounded<T>(operation: Promise<T>): Promise<T> {
    let timer: ReturnType<typeof setTimeout>;
    try { return await Promise.race([operation, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("连接超时，操作结果待核对。请使用同一请求重试。")), this.timeoutMs); })]); }
    finally { clearTimeout(timer!); }
  }
  private persist(pending: CandidateAction | null) {
    try {
      if (!this.storage) throw new Error("storage unavailable");
      if (pending) this.storage.setItem(this.storageKey, JSON.stringify(pending)); else this.storage.removeItem(this.storageKey);
    } catch { this.update({warning: "浏览器无法保存待核对操作。刷新后请先核对服务器候选，避免重复操作。"}); }
  }
  start = () => {
    this.active = true; ++this.generation;
    try {
      const pending = readPending(this.storage?.getItem(this.storageKey) ?? null);
      if (pending) this.update({pending, error: "有上次结果待核对的操作。请先刷新候选，再使用同一请求重试。"});
    } catch { this.update({warning: "上次操作记录无法读取，请先核对服务器候选。"}); }
    void this.refresh();
  };
  dispose = () => { this.active = false; ++this.generation; ++this.readGeneration; };
  refresh = async (): Promise<void> => {
    if (!this.active) return;
    const generation = this.generation, read = ++this.readGeneration;
    this.update({loading: true});
    try {
      const value = readCandidateWorkspace(await this.bounded(this.request(this.base)), this.buyerId, this.sessionId);
      if (!this.current(generation) || read !== this.readGeneration) return;
      this.update({workspace: mergeCandidateWorkspace(this.state.workspace, value), ...(this.state.conflict ? {retryable: true} : {}), ...(!this.state.pending ? {error: null} : {})});
    } catch (error) {
      if (!this.current(generation) || read !== this.readGeneration) return;
      this.update({error: this.state.pending ? (this.state.conflict ? "另一页面已修改候选，最新状态读取失败。请先刷新后再重试。" : this.state.error) : (error as {status?: number}).status === 404
        ? "会话尚未建立。完成一次查询后可保留候选；已有记录请刷新核对。"
        : `候选暂未同步：${error instanceof Error ? error.message : "请稍后刷新"}`});
    } finally { if (this.current(generation) && read === this.readGeneration) this.update({loading: false}); }
  };
  mutate = async (intent: CandidateIntent): Promise<boolean> => {
    if (!this.active || this.state.busy || this.state.pending || !this.state.workspace) return false;
    if (intent.kind === "compare" && intent.candidate_ids.length > 3) { this.update({error: "一次最多比较 3 个规格，请先移出一项。"}); return false; }
    return this.perform({...intent, request_id: crypto.randomUUID(), expected_revision: this.state.workspace.revision});
  };
  private async perform(action: CandidateAction): Promise<boolean> {
    const generation = this.generation;
    this.update({busy: true, pending: action, error: null, conflict: false, retryable: true}); this.persist(action);
    try {
      const {path, method, body} = candidateActionRequest(this.base, action);
      const next = readCandidateWorkspace(await this.bounded(this.request(path, method, body)), this.buyerId, this.sessionId);
      if (!this.current(generation)) return false;
      const stale = this.state.workspace && next.revision < this.state.workspace.revision;
      this.update({workspace: mergeCandidateWorkspace(this.state.workspace, next), pending: null, error: null}); this.persist(null);
      if (stale) void this.refresh();
      return true;
    } catch (error) {
      if (!this.current(generation)) return false;
      const code = (error as {code?: string}).code;
      const conflict = code === "CANDIDATE_REVISION_CONFLICT";
      this.update({conflict, retryable: !conflict && code !== "CANDIDATE_REQUEST_CONFLICT", error: conflict
        ? "另一页面已修改候选。请先刷新核对，再点击“按最新版本重试”；不会自动覆盖。"
        : `操作未确认：${error instanceof Error ? error.message : "请使用同一请求重试"}`});
      if (conflict) await this.refresh();
      return false;
    } finally { if (this.current(generation)) this.update({busy: false}); }
  }
  retry = async (): Promise<boolean> => {
    if (!this.active || this.state.busy || this.state.loading || !this.state.pending || !this.state.retryable) return false;
    const pending = this.state.pending;
    if (this.state.conflict) {
      if (!this.state.workspace) return false;
      return this.perform({...pending, request_id: crypto.randomUUID(), expected_revision: this.state.workspace.revision});
    }
    return this.perform(pending);
  };
  dismiss = () => { if (!this.state.busy) { this.update({pending: null, conflict: false, error: null}); this.persist(null); void this.refresh(); } };
  verify = async (candidate: Candidate, currency: string, shipTo: string): Promise<CandidateVerification | null> => {
    const generation = this.generation;
    const value = readCandidateVerification(await this.bounded(this.request(`${this.base}/${encodeURIComponent(candidate.candidate_id)}/verification?target_currency=${encodeURIComponent(currency)}&ship_to=${encodeURIComponent(shipTo)}`)), candidate, this.sessionId);
    if (!this.current(generation) || !this.state.workspace?.items.some(c => c.candidate_id === candidate.candidate_id)) return null;
    if (value.workspace_revision !== this.state.workspace.revision) { void this.refresh(); return null; }
    return value;
  };
}
