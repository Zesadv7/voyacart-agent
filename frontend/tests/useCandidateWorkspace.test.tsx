// @vitest-environment jsdom
import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCandidateWorkspace } from "../src/hooks/useCandidateWorkspace";
import type { Candidate, CandidateWorkspace, WorkspaceRequest } from "../src/lib/candidateWorkspace";

const STAMP = "2026-10-16T04:00:00.000Z";
const pendingKey = (buyer = "buyer-a", session = "session-a") => `voyacart.candidate.pending.v1:${encodeURIComponent(buyer)}:${encodeURIComponent(session)}`;
const intent = { kind: "add" as const, source: { kind: "run" as const, run_id: "run-a", product_id: "product-a", sku_id: "sku-a" } };
function candidate(): Candidate {
  return { candidate_id: "candidate-a", product_id: "product-a", sku_id: "sku-a", saved_at: STAMP,
    source: { kind: "run", run_id: "run-a", captured_cursor: 1, captured_at: STAMP },
    identity_status: "bound", quote_status: "historical", snapshot: {
      product_id: "product-a", title: "旅行包", brand: "Voyacart", category: "bag", origin_country: "CN",
      price_major: 120, currency: "CNY", score: 0.8, highlights: ["轻便"], default_sku_id: "sku-a",
      skus: [{ sku_id: "sku-a", spec: "黑色", price_major: 120, currency: "CNY", stock: 5 }],
    } };
}
function workspace(revision = 1, session = "session-a", buyer = "buyer-a", items = [candidate()]): Record<string, unknown> {
  const result: CandidateWorkspace = { schema_version: 1, buyer_id: buyer, session_id: session, revision, items,
    comparison_ids: [], limits: { candidates: 30, comparison: 3 } };
  return result as unknown as Record<string, unknown>;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
type HookValue = ReturnType<typeof useCandidateWorkspace>;
let host: HTMLDivElement;
let root: Root | null;
let current: HookValue;
function Harness({ buyer = "buyer-a", session = "session-a", request, refreshKey = "run-a" }: {
  buyer?: string; session?: string; request: WorkspaceRequest; refreshKey?: string;
}) {
  current = useCandidateWorkspace(buyer, session, request, refreshKey);
  return <output data-session={session}>{current.workspace ? `${current.workspace.buyer_id}/${current.workspace.session_id}/${current.workspace.revision}` : "loading"}</output>;
}
async function render(request: WorkspaceRequest, options: { buyer?: string; session?: string; refreshKey?: string; strict?: boolean } = {}) {
  const child = <Harness request={request} {...options} />;
  await act(async () => root!.render(options.strict ? <StrictMode>{child}</StrictMode> : child));
}
async function unmount() { if (root) { await act(async () => root!.unmount()); root = null; } }
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  sessionStorage.clear(); host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await unmount(); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("useCandidateWorkspace 的挂载代次与恢复边界", () => {
  it("A→B→A 时旧 A 的高版本回包也不能污染新 A", async () => {
    const calls: { session: string; pending: ReturnType<typeof deferred<Record<string, unknown>>> }[] = [];
    const request = vi.fn<WorkspaceRequest>((path) => {
      const session = decodeURIComponent(path.split("/")[3]); const pending = deferred<Record<string, unknown>>();
      calls.push({ session, pending }); return pending.promise;
    });
    await render(request); const oldA = current.controller; const initialACalls = [...calls];
    expect(current.workspace).toBeNull();
    await render(request, { session: "session-b" }); const b = current.controller;
    const bCalls = calls.filter(call => call.session === "session-b");
    await act(async () => { for (const call of bCalls) call.pending.resolve(workspace(2, "session-b")); });
    expect(host.textContent).toBe("buyer-a/session-b/2");
    const oldCount = calls.length; await render(request); const newA = current.controller;
    expect(newA).not.toBe(oldA); expect(newA).not.toBe(b); expect(current.workspace).toBeNull();
    await act(async () => { for (const call of calls.slice(oldCount)) call.pending.resolve(workspace(3)); });
    expect(host.textContent).toBe("buyer-a/session-a/3");
    await act(async () => { for (const call of initialACalls) call.pending.resolve(workspace(99)); });
    expect(host.textContent).toBe("buyer-a/session-a/3");
    expect(current.controller).toBe(newA); expect(current.workspace?.revision).toBe(3);
  });

  it("买家切换不沿用同 session 的另一身份 workspace 或 pending", async () => {
    const pending = { ...intent, request_id: "buyer-a-pending", expected_revision: 1 };
    sessionStorage.setItem(pendingKey(), JSON.stringify(pending));
    let buyer = "buyer-a";
    const request = vi.fn<WorkspaceRequest>().mockImplementation(async () => workspace(1, "session-a", buyer));
    await render(request); const old = current.controller;
    expect(current.pending?.request_id).toBe("buyer-a-pending");
    buyer = "buyer-b"; await render(request, { buyer });
    expect(current.controller).not.toBe(old); expect(current.pending).toBeNull();
    expect(current.workspace?.buyer_id).toBe("buyer-b");
    expect(sessionStorage.getItem(pendingKey())).toBe(JSON.stringify(pending));
    expect(request.mock.calls.every(([, method]) => method === undefined)).toBe(true);
  });

  it("卸载后旧写回包不清除新挂载恢复的 pending，也不复活已删除候选", async () => {
    const write = deferred<Record<string, unknown>>(); let revision = 1;
    const request = vi.fn<WorkspaceRequest>((_path, method) => method ? write.promise : Promise.resolve(workspace(revision, "session-a", "buyer-a", revision === 1 ? [candidate()] : [])));
    await render(request); const old = current.controller;
    let mutation!: Promise<boolean>;
    await act(async () => { mutation = old.mutate(intent); });
    const pending = current.pending; expect(pending).not.toBeNull();
    await unmount(); revision = 4; root = createRoot(host); await render(request);
    const latest = current.controller; expect(latest).not.toBe(old);
    expect(current.pending).toEqual(pending); expect(current.workspace?.revision).toBe(4);
    await act(async () => { write.resolve(workspace(2)); expect(await mutation).toBe(false); });
    expect(current.workspace?.revision).toBe(4); expect(current.workspace?.items).toEqual([]);
    expect(current.pending).toEqual(pending);
    expect(JSON.parse(sessionStorage.getItem(pendingKey())!)).toEqual(pending);
    expect(request.mock.calls.filter(([, method]) => method === "POST")).toHaveLength(1);
  });

  it("刷新后恢复 pending 但只读服务器，显式 retry 使用保存的同一请求", async () => {
    const pending = { ...intent, request_id: "saved-request", expected_revision: 2 };
    sessionStorage.setItem(pendingKey(), JSON.stringify(pending));
    const request = vi.fn<WorkspaceRequest>().mockResolvedValue(workspace(4));
    await render(request);
    expect(current.pending).toEqual(pending); expect(current.error).toContain("上次");
    expect(request.mock.calls.every(([, method]) => method === undefined)).toBe(true);
    await act(async () => { expect(await current.controller.retry()).toBe(true); });
    const write = request.mock.calls.find(([, method]) => method === "POST");
    expect(write).toEqual(["/ag-ui/sessions/session-a/candidates", "POST", {
      request_id: "saved-request", expected_revision: 2, source: intent.source,
    }]);
    expect(current.pending).toBeNull(); expect(sessionStorage.getItem(pendingKey())).toBeNull();
  });

  it("sessionStorage getter 被浏览器拒绝仍能读取并操作，向用户保留风险提示", async () => {
    vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
    let revision = 1;
    const request = vi.fn<WorkspaceRequest>((_path, method) => Promise.resolve(workspace(method ? ++revision : revision)));
    await render(request); expect(current.workspace?.revision).toBe(1);
    await act(async () => { expect(await current.controller.mutate(intent)).toBe(true); });
    expect(current.workspace?.revision).toBe(2); expect(current.warning).toContain("无法保存");
  });

  it("focus/online/visible 与 refreshKey 更新重读；卸载移除监听", async () => {
    const request = vi.fn<WorkspaceRequest>().mockResolvedValue(workspace(1));
    await render(request); const initial = request.mock.calls.length; const instance = current.controller;
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(request).toHaveBeenCalledTimes(initial + 1);
    await act(async () => { window.dispatchEvent(new Event("online")); });
    expect(request).toHaveBeenCalledTimes(initial + 2);
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(request).toHaveBeenCalledTimes(initial + 2);
    visibility.mockReturnValue("visible");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(request).toHaveBeenCalledTimes(initial + 3);
    await render(request, { refreshKey: "run-b" });
    expect(current.controller).toBe(instance); expect(request).toHaveBeenCalledTimes(initial + 4);
    await render(request, { refreshKey: "run-b" });
    expect(request).toHaveBeenCalledTimes(initial + 4);
    await unmount();
    window.dispatchEvent(new Event("focus")); window.dispatchEvent(new Event("online")); document.dispatchEvent(new Event("visibilitychange"));
    expect(request).toHaveBeenCalledTimes(initial + 4);
  });

  it("切换 session 后旧监听已清理，focus 只刷新当前 session", async () => {
    const request = vi.fn<WorkspaceRequest>((path) => Promise.resolve(workspace(1, decodeURIComponent(path.split("/")[3]))));
    await render(request); await render(request, { session: "session-b" });
    const previous = request.mock.calls.length;
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    expect(request.mock.calls.slice(previous)).toEqual([["/ag-ui/sessions/session-b/candidates"]]);
  });

  it("request 身份改变建立新控制器，旧读响应不能流入新的授权连接", async () => {
    const oldReads: ReturnType<typeof deferred<Record<string, unknown>>>[] = [];
    const oldRequest = vi.fn<WorkspaceRequest>(() => { const pending = deferred<Record<string, unknown>>(); oldReads.push(pending); return pending.promise; });
    await render(oldRequest); const old = current.controller;
    const newRequest = vi.fn<WorkspaceRequest>().mockResolvedValue(workspace(3, "session-a", "buyer-a", []));
    await render(newRequest); expect(current.controller).not.toBe(old);
    await act(async () => { for (const pending of oldReads) pending.resolve(workspace(100)); });
    expect(current.workspace?.revision).toBe(3); expect(current.workspace?.items).toEqual([]);
  });

  it("StrictMode start→dispose→start 下只接收最新代次，不重复写入", async () => {
    const reads: ReturnType<typeof deferred<Record<string, unknown>>>[] = [];
    const request = vi.fn<WorkspaceRequest>(() => { const pending = deferred<Record<string, unknown>>(); reads.push(pending); return pending.promise; });
    await render(request, { strict: true }); expect(reads.length).toBeGreaterThan(1);
    await act(async () => { reads.at(-1)!.resolve(workspace(2, "session-a", "buyer-a", [])); });
    await act(async () => { for (const pending of reads.slice(0, -1)) pending.resolve(workspace(90)); });
    expect(current.workspace?.revision).toBe(2); expect(current.workspace?.items).toEqual([]);
    expect(current.loading).toBe(false); expect(request.mock.calls.every(([, method]) => method === undefined)).toBe(true);
  });
});
