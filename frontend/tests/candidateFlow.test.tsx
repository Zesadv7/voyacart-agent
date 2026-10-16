// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../src/App";
import type { ProductCard, TradeConfirmation } from "../src/types";
import type { CandidateWorkspace } from "../src/lib/candidateWorkspace";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const product: ProductCard = {
  product_id: "P1", title: "合成旅行背包", brand: "合成品牌", category: "旅行装备", origin_country: "CN", price_major: 20, currency: "CNY",
  score: 1, highlights: ["测试目录"], default_sku_id: "S1", ships_to: ["CN", "US"],
  data_provenance: {source: "synthetic_catalog", synthetic: ["price", "stock"], derived: []},
  skus: [{sku_id: "S1", spec: "蓝色", price_major: 20, currency: "CNY", stock: 0}, {sku_id: "S2", spec: "绿色", price_major: 25, currency: "CNY", stock: 7}],
};
let root: Root, host: HTMLDivElement;
let workspaces: Map<string, CandidateWorkspace>, runs: Map<string, any>, requests: {path: string; method: string; body: any}[];
let verificationReply: Promise<Response> | null;
let confirmations: TradeConfirmation[], verificationStatus: string, lostResponse: boolean, revisionConflict: boolean;
const copy = <T,>(v: T): T => JSON.parse(JSON.stringify(v));
const json = (v: unknown, status = 200) => Response.json(copy(v), {status});
function workspace(session: string): CandidateWorkspace {
  if (!workspaces.has(session)) workspaces.set(session, {schema_version: 1, buyer_id: "buyer-test", session_id: session, revision: 0, items: [], comparison_ids: [], limits: {candidates: 30, comparison: 3}});
  return workspaces.get(session)!;
}
const replays = new Map<string, CandidateWorkspace>();
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); workspaces = new Map(); runs = new Map(); requests = []; confirmations = []; replays.clear();
  verificationReply = null; verificationStatus = "available"; lostResponse = false; revisionConflict = false;
  vi.stubGlobal("scrollTo", vi.fn()); Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(String(url), "http://localhost").pathname, method = init?.method ?? "GET", body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({path, method, body});
    if (path.endsWith("/identity/config")) return json({mode: "demo", default_buyer_id: "buyer-test"});
    if (path.endsWith("/ag-ui/sessions")) return json({sessions: [...runs.values()].map(run => ({id: run.threadId, title: "合成选购", updatedAt: 1}))});
    if (path.endsWith("/ag-ui/run") && method === "POST") {
      const products = body.messages.at(-1).content.includes("零结果") ? [] : [product];
      const run = {runId: body.runId, threadId: body.threadId, status: "completed", messages: body.messages, state: {products, searchCompleted: true}};
      runs.set(body.threadId, run);
      const events = [{type: "RUN_STARTED", threadId: body.threadId, runId: body.runId}, {type: "STATE_SNAPSHOT", snapshot: run.state}, {type: "RUN_FINISHED", threadId: body.threadId, runId: body.runId}];
      return new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""), {headers: {"Content-Type": "text/event-stream"}});
    }
    const match = path.match(/\/ag-ui\/sessions\/([^/]+)\/candidates(?:\/(.*))?$/);
    if (match) {
      const state = workspace(decodeURIComponent(match[1])), suffix = match[2];
      if (suffix?.endsWith("/verification")) {
        if (verificationReply) return verificationReply;
        const candidate = state.items.find(item => item.candidate_id === suffix.split("/")[0])!;
        const fresh = copy(candidate.snapshot); fresh.skus.find(s => s.sku_id === candidate.sku_id)!.stock = 9;
        fresh.skus.find(s => s.sku_id === candidate.sku_id)!.price_major += 2;
        return json({candidate_id: candidate.candidate_id, session_id: state.session_id, workspace_revision: state.revision, checked_at: "2026-10-15T20:00:00Z", source: "local_catalog", status: verificationStatus,
          reason: verificationStatus === "available" ? "目录现有库存，生成确认时会再次核对。" : "当前目录缺货，候选保留。", ...(verificationStatus === "available" ? {product: fresh} : {})});
      }
      if (method === "GET") return json(state);
      if (revisionConflict) { revisionConflict = false; ++state.revision; return json({detail: {code: "CANDIDATE_REVISION_CONFLICT", message: "changed", current_revision: state.revision}}, 409); }
      if (replays.has(body.request_id)) return json(replays.get(body.request_id));
      if (body.expected_revision !== state.revision) return json({detail: {code: "CANDIDATE_REVISION_CONFLICT", message: "changed", current_revision: state.revision}}, 409);
      if (method === "POST") {
        const snapshot = copy(product), sku = snapshot.skus.find(s => s.sku_id === body.source.sku_id)!;
        snapshot.default_sku_id = sku.sku_id; snapshot.price_major = sku.price_major;
        state.items.push({candidate_id: `candidate-${body.source.sku_id}`, product_id: "P1", sku_id: sku.sku_id, saved_at: "2026-10-15T19:00:00Z", source: {kind: "run", run_id: body.source.run_id, captured_cursor: 5, captured_at: "2026-10-15T18:00:00Z"}, identity_status: "bound", quote_status: "historical", snapshot});
      } else if (method === "PUT") state.comparison_ids = body.candidate_ids;
      else if (method === "DELETE") { state.items = state.items.filter(item => item.candidate_id !== suffix); state.comparison_ids = state.comparison_ids.filter(id => id !== suffix); }
      ++state.revision; replays.set(body.request_id, copy(state));
      if (lostResponse) { lostResponse = false; throw new Error("synthetic response lost after commit"); }
      return json(state);
    }
    const session = path.match(/\/ag-ui\/sessions\/([^/]+)$/);
    if (session) { const run = runs.get(session[1]); return json({run, messages: run?.messages ?? [], productHistory: []}); }
    if (path.endsWith("/confirmations") && method === "GET") return json({confirmations});
    if (path.endsWith("/confirmations/orders")) {
      const c: TradeConfirmation = {confirmation_id: "confirm-1", operation_id: "op-1", action: "create", buyer_id: body.buyer_id, session_id: body.session_id, snapshot_hash: "a".repeat(64), expires_at: "2099-01-01T00:00:00Z", expired: false, status: "pending", result: null,
        payload: {items: body.items.map((item: any) => ({...item, title: product.title, unit_price_minor: 2200, currency: "CNY"})), shipping_address: body.shipping_address, total_amount_minor: 2200, currency: "CNY", amount_scope: "merchandise_only", order_kind: "purchase_intent"}};
      confirmations = [c]; return json({confirmation: c});
    }
    if (path.endsWith("/confirmations/confirm-1/resolve")) {
      confirmations[0] = {...confirmations[0], status: body.approved ? "approved" : "rejected", result: body.approved ? {order_id: "intent-1", status: "CONFIRMED", total_amount_minor: 2200, total_amount_major: 22, currency: "CNY", cancel_reason: null} : null};
      return json({confirmation: confirmations[0]});
    }
    if (path.endsWith("/skills")) return json({skills: []});
    if (path.endsWith("/favorites")) return json({products: []});
    return json({});
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function mount() { await act(async () => { root.render(<App />); }); }
const button = (text: string, within: ParentNode = document) => {
  const result = [...within.querySelectorAll<HTMLButtonElement>("button")].find(b => b.textContent?.trim() === text);
  expect(result, `button ${text}`).toBeDefined(); return result!;
};
async function click(element: HTMLElement) { await act(async () => element.click()); }
async function fill(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  await act(async () => {
    const prototype = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : element.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event(element.tagName === "SELECT" ? "change" : "input", {bubbles: true}));
  });
}
async function search(text: string) {
  const input = host.querySelector<HTMLTextAreaElement>("#query")!; await fill(input, text);
  await act(async () => input.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true})));
}
async function saveBothSkus() {
  await search("查找合成背包"); await click(button("保留候选", host.querySelector('.search-results')!));
  await click(button("查看详情", host.querySelector('.search-results')!));
  await click(document.querySelector<HTMLInputElement>('input[name="product-sku"][value="S2"]')!);
  await click(button("保留当前规格候选")); await click(document.querySelector<HTMLButtonElement>('[role="dialog"] [aria-label="关闭"]')!);
}

it("真实 App + SDK HTTP mock：保留同商品双规格、零结果仍比较、核验旧缺货、prepare与显式批准、刷新恢复", async () => {
  // 强制经过真实异步摘要阶段，防止点击返回偶然等同于完整 prepare 完成。
  const nativeDigest = crypto.subtle.digest.bind(crypto.subtle);
  vi.spyOn(crypto.subtle, "digest").mockImplementationOnce(async (...args) => {
    await new Promise(resolve => setTimeout(resolve, 60));
    return nativeDigest(...args);
  });
  await mount(); await saveBothSkus();
  const saveRequests = requests.filter(r => r.method === "POST" && r.path.endsWith("/candidates"));
  expect(saveRequests).toHaveLength(2); expect(saveRequests[0].body.source.run_id).toBe([...runs.values()][0].runId);
  expect(saveRequests[1].body.source.sku_id).toBe("S2"); expect(saveRequests[0].body).not.toHaveProperty("snapshot");
  for (const box of host.querySelectorAll<HTMLInputElement>('.candidate-workspace input[type="checkbox"]')) await click(box);
  await search("零结果"); expect(host.querySelector('.search-results')).toBeNull(); expect(host.querySelectorAll('.candidate-card')).toHaveLength(2);
  await click(button("开始比较")); expect(document.querySelectorAll('.compare-table thead th')).toHaveLength(3);
  expect(document.querySelector('.compare-table')!.textContent).toContain("保存时库存");
  await click(document.querySelector<HTMLButtonElement>('[role="dialog"] [aria-label="关闭"]')!);
  await click(host.querySelector<HTMLButtonElement>('[aria-label="核验候选 合成旅行背包 S1"]')!);
  expect(document.querySelector('[role="dialog"]')!.textContent).toContain("库存 0");
  await click(button("重新核验")); expect(document.querySelector('[role="dialog"]')!.textContent).toContain("当前目录：¥22.00 / 库存 9");
  expect(requests.some(r => r.path.endsWith("/confirmations/orders"))).toBe(false);
  await click(button("填写下单意向"));
  await fill(document.querySelector<HTMLInputElement>('[name="recipient_name"]')!, "合成测试人");
  await fill(document.querySelector<HTMLInputElement>('[name="city"]')!, "合成城市");
  await fill(document.querySelector<HTMLTextAreaElement>('[name="address_line"]')!, "合成街道");
  await act(async () => {
    button("生成确认单").click();
    // 等待异步摘要、HTTP 及确认映射完成，随后由 act 刷新真实界面。
    await vi.waitFor(() => {
      const saved = Object.keys(localStorage).filter(key => key.includes("voyacart.prepare.v1:"))
        .map(key => JSON.parse(localStorage.getItem(key)!));
      expect(saved.some(record => record.confirmationId === "confirm-1")).toBe(true);
    }, {timeout: 3000});
  });
  expect(requests.filter(r => r.path.endsWith("/confirmations/orders"))).toHaveLength(1);
  expect(requests.some(r => r.path.endsWith("/resolve"))).toBe(false);
  expect(host.textContent).toContain("尚未支付，也未安排发货");
  await click(button("确认创建意向单")); expect(requests.filter(r => r.path.endsWith("/resolve"))).toHaveLength(1);
  expect(confirmations[0].status).toBe("approved");
  await act(async () => root.unmount()); root = createRoot(host); await mount();
  expect(host.querySelectorAll('.candidate-card')).toHaveLength(2);
  expect([...host.querySelectorAll<HTMLInputElement>('.candidate-workspace input[type="checkbox"]')].every(box => box.checked)).toBe(true);
  expect(host.textContent).toContain("intent-1");
  expect([...workspaces.values()].find(state => state.items.length)!.items[0].snapshot.skus[0].stock).toBe(0);
});

it("App 写入丢响应按同键恢复，409保持可见且只有显式核对重试才换版本", async () => {
  await mount(); await search("测试背包"); lostResponse = true;
  await click(button("保留候选", host.querySelector('.search-results')!));
  expect(host.querySelector('.candidate-error')!.textContent).toContain("操作未确认");
  const writes = () => requests.filter(r => r.method === "POST" && r.path.endsWith("/candidates"));
  const firstId = writes()[0].body.request_id;
  await click(button("使用同一请求重试")); expect(writes()[1].body.request_id).toBe(firstId);
  expect(host.querySelectorAll('.candidate-card')).toHaveLength(1);
  revisionConflict = true; await click(host.querySelector<HTMLInputElement>('.candidate-workspace input[type="checkbox"]')!);
  expect(host.querySelector('.candidate-error')!.textContent).toContain("另一页面已修改");
  expect(requests.filter(r => r.method === "PUT" && r.path.endsWith("/comparison"))).toHaveLength(1);
  await click(button("按最新版本重试"));
  const comparisons = requests.filter(r => r.method === "PUT" && r.path.endsWith("/comparison"));
  expect(comparisons).toHaveLength(2); expect(comparisons[1].body.request_id).not.toBe(comparisons[0].body.request_id);
  expect(comparisons[1].body.expected_revision).toBe(comparisons[0].body.expected_revision + 1);
});

it("核验缺货保留历史且无准备入口；目的地变动使成功结果失效，关闭恢复焦点", async () => {
  await mount(); await search("测试背包"); await click(button("保留候选", host.querySelector('.search-results')!));
  const opener = host.querySelector<HTMLButtonElement>('[aria-label="核验候选 合成旅行背包 S1"]')!; opener.focus(); await click(opener);
  verificationStatus = "out_of_stock"; await click(button("重新核验"));
  expect(document.querySelector('[role="dialog"]')!.textContent).toContain("当前目录缺货");
  expect([...document.querySelectorAll('button')].some(b => b.textContent === "填写下单意向")).toBe(false);
  expect(host.querySelectorAll('.candidate-card')).toHaveLength(1);
  verificationStatus = "available"; await click(button("重新核验")); expect(button("填写下单意向")).toBeDefined();
  await fill(document.querySelector<HTMLInputElement>('[aria-label="核验配送地区"]')!, "US");
  expect([...document.querySelectorAll('button')].some(b => b.textContent === "填写下单意向")).toBe(false);
  await click(document.querySelector<HTMLButtonElement>('[role="dialog"] [aria-label="关闭"]')!); expect(document.activeElement).toBe(opener);
});

it("核验未返回时切换会话，迟到的available不能打开旧商品订单表或污染新会话", async () => {
  await mount(); await search("测试背包"); await click(button("保留候选", host.querySelector('.search-results')!));
  const old = [...workspaces.values()].find(state => state.items.length)!;
  let resolve!: (value: Response) => void;
  verificationReply = new Promise<Response>(done => { resolve = done; });
  await click(host.querySelector<HTMLButtonElement>('[aria-label="核验候选 合成旅行背包 S1"]')!);
  await click(button("重新核验")); expect(button("正在核验…").disabled).toBe(true);
  await click(button("新的选购")); expect(document.querySelector('[role="dialog"]')).toBeNull();
  const fresh = copy(product); fresh.skus[0].stock = 8;
  await act(async () => resolve(json({candidate_id: old.items[0].candidate_id, session_id: old.session_id, workspace_revision: old.revision,
    checked_at: "2026-10-15T20:00:00Z", source: "local_catalog", status: "available", reason: "late", product: fresh})));
  expect(document.querySelector('[role="dialog"]')).toBeNull(); expect(host.querySelectorAll('.candidate-card')).toHaveLength(0);
  expect(requests.some(r => r.path.endsWith("/confirmations/orders"))).toBe(false);
  const oldHistory = host.querySelector<HTMLButtonElement>('.history-short')!; await click(oldHistory);
  expect(host.querySelectorAll('.candidate-card')).toHaveLength(1); expect(document.querySelector('[role="dialog"]')).toBeNull();
});
