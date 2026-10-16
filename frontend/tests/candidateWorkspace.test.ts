import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CandidateWorkspaceController,
  candidateActionRequest,
  mergeCandidateWorkspace,
  readCandidateVerification,
  readCandidateWorkspace,
  selectedSku,
  selectionKey,
  type Candidate,
  type CandidateAction,
  type CandidateIntent,
  type CandidateWorkspace,
  type WorkspaceRequest,
} from "../src/lib/candidateWorkspace";
import type { ProductCard } from "../src/types";

const BUYER = "buyer-a";
const SESSION = "session-a";
const BASE = `/ag-ui/sessions/${SESSION}/candidates`;
const STAMP = "2026-10-16T04:00:00.000Z";
const STORAGE_KEY = `voyacart.candidate.pending.v1:${BUYER}:${SESSION}`;
const add: CandidateIntent = {
  kind: "add",
  source: { kind: "run", run_id: "run-a", product_id: "product-a", sku_id: "sku-a" },
};
function product(productId = "product-a", skuId = "sku-a"): ProductCard {
  return {
    product_id: productId, title: "旅行包", brand: "Voyacart", category: "bag", origin_country: "CN",
    price_major: 120, currency: "CNY", score: 0.8, highlights: ["轻便"], default_sku_id: skuId,
    skus: [{ sku_id: skuId, spec: "黑色", price_major: 120, currency: "CNY", stock: 5 }],
  };
}
function candidate(candidateId = "candidate-a", productId = "product-a", skuId = "sku-a"): Candidate {
  return {
    candidate_id: candidateId, product_id: productId, sku_id: skuId, saved_at: STAMP,
    source: { kind: "run", run_id: "run-a", captured_cursor: 3, captured_at: STAMP },
    identity_status: "bound", quote_status: "historical", snapshot: product(productId, skuId),
  };
}
function workspace(revision = 1, items: Candidate[] = [candidate()], sessionId = SESSION, buyerId = BUYER): CandidateWorkspace {
  return { schema_version: 1, buyer_id: buyerId, session_id: sessionId, revision, items,
    comparison_ids: [], limits: { candidates: 30, comparison: 3 } };
}
function verification(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { candidate_id: "candidate-a", session_id: SESSION, workspace_revision: 1,
    checked_at: STAMP, source: "local_catalog", status: "available", reason: "库存已核验",
    product: product(), ...overrides };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
function response(value: CandidateWorkspace): Record<string, unknown> { return value as unknown as Record<string, unknown>; }
function storage(initial?: CandidateAction) {
  const values = new Map<string, string>(initial ? [[STORAGE_KEY, JSON.stringify(initial)]] : []);
  return { values, getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { values.set(key, value); }),
    removeItem: vi.fn((key: string) => { values.delete(key); }) };
}
const controllers: CandidateWorkspaceController[] = [];
function controller(request: WorkspaceRequest, pendingStorage = storage(), timeoutMs = 15000, sessionId = SESSION, buyerId = BUYER) {
  const instance = new CandidateWorkspaceController(buyerId, sessionId, request, pendingStorage, timeoutMs);
  controllers.push(instance);
  return instance;
}
async function settle() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
async function started(request: WorkspaceRequest, pendingStorage = storage(), timeoutMs = 15000) {
  const instance = controller(request, pendingStorage, timeoutMs);
  instance.start(); await settle();
  expect(instance.getSnapshot().workspace).not.toBeNull();
  return instance;
}
afterEach(() => { for (const instance of controllers.splice(0)) instance.dispose(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("候选快照的严格读取边界", () => {
  it("接受合法空集合与同商品不同规格，不混淆字符串拼接边界", () => {
    expect(readCandidateWorkspace(workspace(0, []), BUYER, SESSION)).toEqual(workspace(0, []));
    const first = candidate();
    const second = candidate("candidate-b", "product-a", "sku-b");
    const input = workspace(7, [first, second]); input.comparison_ids = [first.candidate_id, second.candidate_id];
    expect(readCandidateWorkspace(input, BUYER, SESSION)).toEqual(input);
    expect(selectionKey("a:b", "c")).not.toBe(selectionKey("a", "b:c"));
  });

  it.each([
    ["买家不匹配", (v: any) => { v.buyer_id = "buyer-b"; }],
    ["会话不匹配", (v: any) => { v.session_id = "session-b"; }],
    ["schema 未知", (v: any) => { v.schema_version = 2; }],
    ["schema 字符串", (v: any) => { v.schema_version = "1"; }],
    ["revision 负数", (v: any) => { v.revision = -1; }],
    ["revision 小数", (v: any) => { v.revision = 1.5; }],
    ["revision NaN", (v: any) => { v.revision = NaN; }],
    ["revision Infinity", (v: any) => { v.revision = Infinity; }],
    ["revision 超安全整数", (v: any) => { v.revision = Number.MAX_SAFE_INTEGER + 1; }],
    ["缺失 items", (v: any) => { delete v.items; }],
    ["items 非数组", (v: any) => { v.items = {}; }],
    ["比较集合非数组", (v: any) => { v.comparison_ids = {}; }],
    ["候选上限不符", (v: any) => { v.limits.candidates = 31; }],
    ["比较上限不符", (v: any) => { v.limits.comparison = 4; }],
    ["候选数超限", (v: any) => { v.items = Array.from({ length: 31 }, (_, i) => candidate(`c-${i}`, `p-${i}`, `s-${i}`)); }],
    ["比较数超限", (v: any) => { v.items = Array.from({ length: 4 }, (_, i) => candidate(`c-${i}`, `p-${i}`, `s-${i}`)); v.comparison_ids = v.items.map((c: Candidate) => c.candidate_id); }],
    ["候选 ID 重复", (v: any) => { v.items.push(candidate("candidate-a", "product-b", "sku-b")); }],
    ["商品规格重复", (v: any) => { v.items.push(candidate("candidate-b")); }],
    ["比较 ID 重复", (v: any) => { v.comparison_ids = ["candidate-a", "candidate-a"]; }],
    ["比较外键不存在", (v: any) => { v.comparison_ids = ["missing"]; }],
    ["候选 ID 为空白", (v: any) => { v.items[0].candidate_id = " "; }],
    ["候选 ID 过长", (v: any) => { v.items[0].candidate_id = "a".repeat(257); }],
    ["商品 ID 与快照不匹配", (v: any) => { v.items[0].product_id = "other"; }],
    ["规格 ID 与快照不匹配", (v: any) => { v.items[0].sku_id = "other"; }],
    ["规格缺少显式选择", (v: any) => { delete v.items[0].snapshot.default_sku_id; }],
    ["默认规格不存在", (v: any) => { v.items[0].snapshot.default_sku_id = "missing"; }],
    ["快照规格重复", (v: any) => { v.items[0].snapshot.skus.push({ ...v.items[0].snapshot.skus[0] }); }],
    ["快照没有规格", (v: any) => { v.items[0].snapshot.skus = []; }],
    ["快照价格 NaN", (v: any) => { v.items[0].snapshot.price_major = NaN; }],
    ["快照价格 Infinity", (v: any) => { v.items[0].snapshot.price_major = Infinity; }],
    ["快照价格负数", (v: any) => { v.items[0].snapshot.price_major = -1; }],
    ["快照评分 NaN", (v: any) => { v.items[0].snapshot.score = NaN; }],
    ["规格价格 NaN", (v: any) => { v.items[0].snapshot.skus[0].price_major = NaN; }],
    ["规格库存小数", (v: any) => { v.items[0].snapshot.skus[0].stock = 0.5; }],
    ["规格库存负数", (v: any) => { v.items[0].snapshot.skus[0].stock = -1; }],
    ["规格库存 NaN", (v: any) => { v.items[0].snapshot.skus[0].stock = NaN; }],
    ["规格说明非字符串", (v: any) => { v.items[0].snapshot.skus[0].spec = {}; }],
    ["快照币种非法", (v: any) => { v.items[0].snapshot.currency = "bad-currency"; }],
    ["来源类型未知", (v: any) => { v.items[0].source.kind = "client"; }],
    ["来源 run 缺失", (v: any) => { delete v.items[0].source.run_id; }],
    ["来源 cursor 负数", (v: any) => { v.items[0].source.captured_cursor = -1; }],
    ["来源 cursor NaN", (v: any) => { v.items[0].source.captured_cursor = NaN; }],
    ["保存时间非法", (v: any) => { v.items[0].saved_at = "yesterday"; }],
    ["来源时间非法", (v: any) => { v.items[0].source.captured_at = "invalid"; }],
    ["身份状态未知", (v: any) => { v.items[0].identity_status = "trusted"; }],
    ["身份状态不得强转数组", (v: any) => { v.items[0].identity_status = ["bound"]; }],
    ["报价不得升级成实时", (v: any) => { v.items[0].quote_status = "live"; }],
  ])("拒绝%s，不能静默丢弃坏候选", (_name, mutate) => {
    const input = workspace(); mutate(input);
    expect(() => readCandidateWorkspace(input, BUYER, SESSION)).toThrow();
  });

  it.each([undefined, null, [], "", 3])("拒绝非对象快照 %#", (value) => {
    expect(() => readCandidateWorkspace(value, BUYER, SESSION)).toThrow();
  });

  it("unknown 身份可保留历史候选；展示层不自动选中第一个规格", () => {
    const item = candidate(); item.identity_status = "unknown";
    expect(readCandidateWorkspace(workspace(1, [item]), BUYER, SESSION).items[0].identity_status).toBe("unknown");
    const card = product(); delete card.default_sku_id;
    expect(selectedSku(card)).toBeNull(); card.default_sku_id = "absent";
    expect(selectedSku(card)).toBeNull(); card.default_sku_id = "sku-a";
    expect(selectedSku(card)).toBe("sku-a");
  });

  it("revision 只前进；同版本及旧重放不复活已删除候选", () => {
    const current = workspace(4, []);
    expect(mergeCandidateWorkspace(null, current)).toBe(current);
    expect(mergeCandidateWorkspace(current, workspace(3))).toBe(current);
    expect(mergeCandidateWorkspace(current, workspace(4))).toBe(current);
    const newer = workspace(5); expect(mergeCandidateWorkspace(current, newer)).toBe(newer);
    expect(() => mergeCandidateWorkspace(current, workspace(5, [], "session-b"))).toThrow();
    expect(() => mergeCandidateWorkspace(current, workspace(5, [], SESSION, "buyer-b"))).toThrow();
  });
});

describe("写入只发送服务器动作合同", () => {
  it.each([
    ["add", { ...add, request_id: "request-a", expected_revision: 7 }, BASE, "POST", { request_id: "request-a", expected_revision: 7, source: (add as Extract<CandidateIntent, {kind: "add"}>).source }],
    ["remove", { kind: "remove", candidate_id: "id/with ?#", request_id: "request-a", expected_revision: 7 }, `${BASE}/id%2Fwith%20%3F%23`, "DELETE", { request_id: "request-a", expected_revision: 7 }],
    ["compare", { kind: "compare", candidate_ids: ["candidate-b", "candidate-a"], request_id: "request-a", expected_revision: 7 }, `${BASE}/comparison`, "PUT", { request_id: "request-a", expected_revision: 7, candidate_ids: ["candidate-b", "candidate-a"] }],
  ])("%s 的 endpoint/method/body 精确匹配，无客户端快照", (_name, action, path, method, body) => {
    expect(candidateActionRequest(BASE, action as CandidateAction)).toEqual({ path, method, body });
  });

  it("add 的来源使用字段白名单，忽略运行时夹带的快照与报价", () => {
    const action = { ...add, request_id: "request-a", expected_revision: 1, snapshot: product(),
      source: { kind: "run", run_id: "run-a", product_id: "product-a", sku_id: "sku-a", snapshot: product(), price_major: 1, buyer_id: "other" } };
    expect(candidateActionRequest(BASE, action as CandidateAction).body).toEqual({ request_id: "request-a", expected_revision: 1,
      source: { kind: "run", run_id: "run-a", product_id: "product-a", sku_id: "sku-a" } });
  });
});

describe("核验只向可执行入口返回 available 的当前规格", () => {
  it("available 返回精确身份、规格和有库存的服务器商品", () => {
    expect(readCandidateVerification(verification(), candidate(), SESSION)).toEqual(verification());
  });
  it.each(["out_of_stock", "unavailable", "identity_mismatch", "unknown_identity", "terms_changed"])("%s 不泄漏响应附带的商品到执行入口", (status) => {
    const result = readCandidateVerification(verification({ status }), candidate(), SESSION);
    expect(result.status).toBe(status); expect(result).not.toHaveProperty("product");
  });
  it.each([
    ["candidate 不匹配", { candidate_id: "candidate-b" }], ["session 不匹配", { session_id: "session-b" }],
    ["revision 非法", { workspace_revision: NaN }], ["时间非法", { checked_at: "unknown" }],
    ["来源非法", { source: "client" }], ["状态未知", { status: "ok" }], ["状态数组", { status: ["available"] }],
    ["原因非字符串", { reason: null }], ["available 没有商品", { product: undefined }],
    ["商品不匹配", { product: product("product-b") }], ["规格不匹配", { product: product("product-a", "sku-b") }],
    ["available 零库存", { product: { ...product(), skus: [{ ...product().skus[0], stock: 0 }] } }],
    ["available 库存 NaN", { product: { ...product(), skus: [{ ...product().skus[0], stock: NaN }] } }],
    ["available 重复规格", { product: { ...product(), skus: [product().skus[0], product().skus[0]] } }],
  ])("拒绝%s", (_name, patch) => {
    expect(() => readCandidateVerification(verification(patch), candidate(), SESSION)).toThrow();
  });
  it("未知来源身份不能由 available 响应升级为可执行商品", () => {
    const item = candidate(); item.identity_status = "unknown";
    expect(() => readCandidateVerification(verification(), item, SESSION)).toThrow();
  });
});

describe("候选控制器的并发、重试及服务端恢复", () => {
  it("GET 读取期间没有快照不能写，session 路径编码且不携带客户端买家声明", async () => {
    const waiting = deferred<Record<string, unknown>>(); const request = vi.fn(() => waiting.promise);
    const instance = controller(request, storage(), 15000, "session/a ?#", "buyer/one"); instance.start();
    expect(instance.getSnapshot()).toMatchObject({ loading: true, workspace: null });
    expect(await instance.mutate(add)).toBe(false);
    expect(request).toHaveBeenCalledExactlyOnceWith("/ag-ui/sessions/session%2Fa%20%3F%23/candidates");
    waiting.resolve(response(workspace(0, [], "session/a ?#", "buyer/one"))); await settle();
    expect(instance.getSnapshot()).toMatchObject({ loading: false, workspace: { revision: 0 } });
  });

  it("同一事件循环双击只发一次写入，pending 与 busy 立即生效", async () => {
    const writing = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace())).mockImplementation(() => writing.promise);
    const saved = storage(); const instance = await started(request, saved);
    const first = instance.mutate(add); const pending = instance.getSnapshot().pending;
    expect(instance.getSnapshot()).toMatchObject({ busy: true, pending: { expected_revision: 1 } });
    expect(JSON.parse(saved.values.get(STORAGE_KEY)!)).toEqual(pending);
    expect(await instance.mutate(add)).toBe(false); expect(await instance.retry()).toBe(false);
    expect(request).toHaveBeenCalledTimes(2);
    writing.resolve(response(workspace(2))); expect(await first).toBe(true);
    expect(instance.getSnapshot()).toMatchObject({ busy: false, pending: null, workspace: { revision: 2 } });
    expect(saved.values.has(STORAGE_KEY)).toBe(false);
  });

  it("迟到 GET 不覆盖较新写入，同 revision 也不复活候选", async () => {
    const lateGet = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(1)))
      .mockImplementationOnce(() => lateGet.promise).mockResolvedValueOnce(response(workspace(3, [])));
    const instance = await started(request); const read = instance.refresh();
    expect(await instance.mutate({ kind: "remove", candidate_id: "candidate-a" })).toBe(true);
    lateGet.resolve(response(workspace(2))); await read;
    expect(instance.getSnapshot().workspace).toEqual(workspace(3, []));
    request.mockResolvedValueOnce(response(workspace(3))); await instance.refresh();
    expect(instance.getSnapshot().workspace).toEqual(workspace(3, []));
  });

  it("较早开始但较晚返回的 GET 不能覆盖最新 GET", async () => {
    const older = deferred<Record<string, unknown>>(), newer = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(1)))
      .mockImplementationOnce(() => older.promise).mockImplementationOnce(() => newer.promise);
    const instance = await started(request); const read1 = instance.refresh(), read2 = instance.refresh();
    newer.resolve(response(workspace(4, []))); await read2; older.resolve(response(workspace(9))); await read1;
    expect(instance.getSnapshot()).toMatchObject({ loading: false, workspace: { revision: 4, items: [] } });
  });

  it("迟到写入的旧重放不回退当前版本，随后重新 GET 核对", async () => {
    const writing = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(1)))
      .mockImplementationOnce(() => writing.promise).mockResolvedValueOnce(response(workspace(4, [])))
      .mockResolvedValueOnce(response(workspace(4, [])));
    const instance = await started(request); const pending = instance.mutate(add); await instance.refresh();
    writing.resolve(response(workspace(2))); expect(await pending).toBe(true); await settle();
    expect(instance.getSnapshot().workspace).toEqual(workspace(4, []));
    expect(request.mock.calls.map(([, method]) => method ?? "GET")).toEqual(["GET", "POST", "GET", "GET"]);
  });

  it("网络断连保留原 request_id 和 expected_revision，显式重试同一请求", async () => {
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(3)))
      .mockRejectedValueOnce(new TypeError("network disconnected")).mockResolvedValueOnce(response(workspace(4)));
    const instance = await started(request); expect(await instance.mutate(add)).toBe(false);
    const failedCall = request.mock.calls[1]; const pending = instance.getSnapshot().pending;
    expect(instance.getSnapshot()).toMatchObject({ busy: false, conflict: false, retryable: true });
    expect(pending?.request_id).toBeTruthy(); expect(pending?.expected_revision).toBe(3);
    expect(await instance.mutate({ kind: "remove", candidate_id: "candidate-a" })).toBe(false);
    expect(await instance.retry()).toBe(true);
    expect(request.mock.calls[2]).toEqual(failedCall); expect(instance.getSnapshot().pending).toBeNull();
  });

  it("写入超时保留幂等键，迟到成功不清除 pending；重试仍用原键", async () => {
    vi.useFakeTimers();
    const writing = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(1)))
      .mockImplementationOnce(() => writing.promise).mockResolvedValueOnce(response(workspace(2)));
    const instance = await started(request, storage(), 50); const pending = instance.mutate(add);
    await vi.advanceTimersByTimeAsync(51); expect(await pending).toBe(false);
    expect(instance.getSnapshot().error).toContain("超时"); const original = instance.getSnapshot().pending;
    writing.resolve(response(workspace(2))); await settle();
    expect(instance.getSnapshot().pending).toEqual(original); expect(instance.getSnapshot().workspace?.revision).toBe(1);
    expect(await instance.retry()).toBe(true); expect(request.mock.calls[2]).toEqual(request.mock.calls[1]);
  });

  it("409 仅重读而不自动重放，显式重试使用新键和最新 revision", async () => {
    const refreshed = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(1)))
      .mockRejectedValueOnce(Object.assign(new Error("revision conflict"), { status: 409, code: "CANDIDATE_REVISION_CONFLICT" }))
      .mockImplementationOnce(() => refreshed.promise).mockResolvedValueOnce(response(workspace(8)));
    const instance = await started(request); const mutation = instance.mutate(add); await settle();
    expect(instance.getSnapshot()).toMatchObject({ conflict: true, loading: true, busy: true });
    expect(await instance.retry()).toBe(false); expect(request).toHaveBeenCalledTimes(3);
    refreshed.resolve(response(workspace(7))); expect(await mutation).toBe(false);
    expect(instance.getSnapshot()).toMatchObject({ conflict: true, loading: false, busy: false, pending: { expected_revision: 1 } });
    expect(request.mock.calls.filter(([, method]) => method === "POST")).toHaveLength(1);
    const originalId = instance.getSnapshot().pending?.request_id;
    expect(await instance.retry()).toBe(true);
    const retried = request.mock.calls[3][2]!;
    expect(retried.expected_revision).toBe(7); expect(retried.request_id).not.toBe(originalId);
    expect(retried.source).toEqual(request.mock.calls[1][2]!.source);
  });

  it("两个标签共享服务端版本：A 修改后 B 冲突重读，核对后才能以新键提交", async () => {
    let server = workspace(1);
    const writes: { method: string; body: Record<string, unknown> }[] = [];
    const request = vi.fn<WorkspaceRequest>(async (_path, method, body) => {
      if (!method) return response(server);
      writes.push({ method, body: { ...body! } });
      if (body!.expected_revision !== server.revision)
        throw Object.assign(new Error("other tab changed candidates"), { status: 409, code: "CANDIDATE_REVISION_CONFLICT" });
      server = { ...server, revision: server.revision + 1,
        comparison_ids: method === "DELETE" ? [] : method === "PUT" ? body!.candidate_ids as string[] : server.comparison_ids,
        items: method === "DELETE" ? [] : server.items };
      return response(server);
    });
    const tabA = await started(request, storage()), tabB = await started(request, storage());
    expect(await tabA.mutate({ kind: "compare", candidate_ids: ["candidate-a"] })).toBe(true);
    expect(tabA.getSnapshot().workspace?.revision).toBe(2);
    expect(tabB.getSnapshot().workspace?.revision).toBe(1);
    expect(await tabB.mutate({ kind: "remove", candidate_id: "candidate-a" })).toBe(false);
    expect(writes).toHaveLength(2);
    expect(tabB.getSnapshot()).toMatchObject({ conflict: true, pending: { expected_revision: 1 }, workspace: { revision: 2, comparison_ids: ["candidate-a"] } });
    // 新请求必须由用户显式 retry 触发，服务端原子移除候选及其比较外键。
    const conflictingKey = writes[1].body.request_id;
    expect(await tabB.retry()).toBe(true);
    expect(writes).toHaveLength(3);
    expect(writes[2]).toEqual({ method: "DELETE", body: { request_id: expect.any(String), expected_revision: 2 } });
    expect(writes[2].body.request_id).not.toBe(conflictingKey);
    expect(tabB.getSnapshot()).toMatchObject({ pending: null, conflict: false, workspace: { revision: 3, items: [] } });
    await tabA.refresh(); expect(tabA.getSnapshot().workspace).toEqual(tabB.getSnapshot().workspace);
  });

  it("409后的最新读取失败先禁止换键，成功刷新后才允许明确重试", async () => {
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(1)))
      .mockRejectedValueOnce(Object.assign(new Error("changed"), {code: "CANDIDATE_REVISION_CONFLICT"}))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(response(workspace(4)))
      .mockResolvedValueOnce(response(workspace(5)));
    const instance = await started(request);
    expect(await instance.mutate(add)).toBe(false);
    expect(instance.getSnapshot()).toMatchObject({conflict: true, retryable: false});
    expect(await instance.retry()).toBe(false); expect(request).toHaveBeenCalledTimes(3);
    await instance.refresh(); expect(instance.getSnapshot().retryable).toBe(true);
    expect(await instance.retry()).toBe(true);
    expect(request.mock.calls[4][2]?.expected_revision).toBe(4);
  });

  it("request_id 载荷冲突不提供同键重试，不自动重复变更", async () => {
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace()))
      .mockRejectedValueOnce(Object.assign(new Error("request conflict"), { status: 409, code: "CANDIDATE_REQUEST_CONFLICT" }));
    const instance = await started(request); expect(await instance.mutate(add)).toBe(false);
    expect(instance.getSnapshot()).toMatchObject({ retryable: false, conflict: false });
    expect(await instance.retry()).toBe(false); expect(request).toHaveBeenCalledTimes(2);
  });

  it("sessionStorage 恢复只取 pending 动作，不自动写入，重试保留原幂等键", async () => {
    const pending: CandidateAction = { ...add, request_id: "restored-request", expected_revision: 2 };
    const saved = storage(pending); const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(5)))
      .mockResolvedValueOnce(response(workspace(3))).mockResolvedValueOnce(response(workspace(5)));
    const instance = await started(request, saved);
    expect(instance.getSnapshot()).toMatchObject({ pending, workspace: { revision: 5 } });
    expect(request).toHaveBeenCalledExactlyOnceWith(BASE);
    expect(await instance.retry()).toBe(true); await settle();
    expect(request.mock.calls[1]).toEqual([BASE, "POST", { request_id: "restored-request", expected_revision: 2, source: (add as Extract<CandidateIntent, {kind: "add"}>).source }]);
    expect(instance.getSnapshot().workspace?.revision).toBe(5); expect(saved.values.has(STORAGE_KEY)).toBe(false);
  });

  it.each([
    ["截断 JSON", "{"], ["缺幂等键", JSON.stringify({ kind: "remove", candidate_id: "candidate-a", expected_revision: 1 })],
    ["负版本", JSON.stringify({ kind: "remove", candidate_id: "candidate-a", expected_revision: -1, request_id: "r" })],
    ["未知动作", JSON.stringify({ kind: "replace", expected_revision: 1, request_id: "r", snapshot: workspace() })],
    ["重复比较", JSON.stringify({ kind: "compare", candidate_ids: ["c", "c"], expected_revision: 1, request_id: "r" })],
    ["比较超限", JSON.stringify({ kind: "compare", candidate_ids: ["a", "b", "c", "d"], expected_revision: 1, request_id: "r" })],
  ])("pending 存储%s 不影响服务器读取，也不会盲写", async (_name, raw) => {
    const saved = storage(); saved.values.set(STORAGE_KEY, raw);
    const request = vi.fn<WorkspaceRequest>().mockResolvedValue(response(workspace())); const instance = await started(request, saved);
    expect(instance.getSnapshot()).toMatchObject({ pending: null, workspace: { revision: 1 } });
    expect(instance.getSnapshot().warning).toBeTruthy(); expect(request).toHaveBeenCalledTimes(1);
  });

  it.each([
    { kind: "remove" as const, candidate_id: "candidate-a", request_id: "saved-remove", expected_revision: 1 },
    { kind: "compare" as const, candidate_ids: ["candidate-a"], request_id: "saved-compare", expected_revision: 1 },
  ])("恢复 $kind 动作时严格保留原载荷，不自动提交", async (pending) => {
    const saved = storage(pending); const request = vi.fn<WorkspaceRequest>().mockResolvedValue(response(workspace(2)));
    const instance = await started(request, saved); expect(instance.getSnapshot().pending).toEqual(pending);
    expect(request.mock.calls).toEqual([[BASE]]);
    expect(await instance.retry()).toBe(true);
    const expected = candidateActionRequest(BASE, pending);
    expect(request.mock.calls[1]).toEqual([expected.path, expected.method, expected.body]);
  });

  it("恢复来源只读白名单字段，存储夹带的客户端快照不会被重放", async () => {
    const saved = storage(); saved.values.set(STORAGE_KEY, JSON.stringify({ ...add, request_id: "saved-add", expected_revision: 1,
      snapshot: product(), source: { ...(add as Extract<CandidateIntent, {kind: "add"}>).source, snapshot: product(), price_major: 1 } }));
    const request = vi.fn<WorkspaceRequest>().mockResolvedValue(response(workspace(2)));
    const instance = await started(request, saved);
    expect(instance.getSnapshot().pending).toEqual({ ...add, request_id: "saved-add", expected_revision: 1 });
    expect(await instance.retry()).toBe(true);
    expect(request.mock.calls[1][2]).toEqual({ source: (add as Extract<CandidateIntent, {kind: "add"}>).source, request_id: "saved-add", expected_revision: 1 });
  });

  it("存储读取/写入失败仍能展示服务端候选、完成动作并持续提示恢复限制", async () => {
    const saved = storage(); saved.getItem.mockImplementation(() => { throw new Error("denied"); });
    saved.setItem.mockImplementation(() => { throw new Error("quota"); }); saved.removeItem.mockImplementation(() => { throw new Error("denied"); });
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace())).mockResolvedValueOnce(response(workspace(2)));
    const instance = await started(request, saved); expect(instance.getSnapshot().warning).toBeTruthy();
    expect(await instance.mutate(add)).toBe(true);
    expect(instance.getSnapshot()).toMatchObject({ busy: false, pending: null, workspace: { revision: 2 } });
    expect(instance.getSnapshot().warning).toContain("无法保存");
  });

  it("读取错误/404/坏响应保留已有集合，不伪装成空集合", async () => {
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(2)))
      .mockRejectedValueOnce(Object.assign(new Error("not found"), { status: 404 }))
      .mockResolvedValueOnce({ schema_version: 999 }).mockRejectedValueOnce(new Error("offline"));
    const instance = await started(request);
    await instance.refresh(); expect(instance.getSnapshot().error).toContain("会话尚未建立");
    await instance.refresh(); expect(instance.getSnapshot().error).toContain("响应不完整");
    await instance.refresh(); expect(instance.getSnapshot().error).toContain("offline");
    expect(instance.getSnapshot().workspace).toEqual(workspace(2));
  });

  it("比较超限在本地拒绝，不发请求；清空比较发送明确空列表", async () => {
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace())).mockResolvedValueOnce(response(workspace(2)));
    const instance = await started(request); expect(await instance.mutate({ kind: "compare", candidate_ids: ["a", "b", "c", "d"] })).toBe(false);
    expect(instance.getSnapshot().error).toContain("最多比较 3"); expect(request).toHaveBeenCalledTimes(1);
    expect(await instance.mutate({ kind: "compare", candidate_ids: [] })).toBe(true);
    expect(request.mock.calls[1]).toEqual([`${BASE}/comparison`, "PUT", expect.objectContaining({ candidate_ids: [], expected_revision: 1 })]);
  });

  it("dismiss 只解除 pending 并重新 GET，不自动执行原动作", async () => {
    const saved = storage({ ...add, request_id: "r", expected_revision: 1 });
    const request = vi.fn<WorkspaceRequest>().mockResolvedValue(response(workspace(2))); const instance = await started(request, saved);
    instance.dismiss(); await settle(); expect(instance.getSnapshot().pending).toBeNull();
    expect(saved.values.has(STORAGE_KEY)).toBe(false); expect(request.mock.calls.map(([, method]) => method ?? "GET")).toEqual(["GET", "GET"]);
  });

  it("dispose 屏蔽迟到写入与通知，也不误清理待核对存储", async () => {
    const writing = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace())).mockImplementation(() => writing.promise);
    const saved = storage(); const instance = await started(request, saved); const changed = vi.fn(); instance.subscribe(changed);
    const mutation = instance.mutate(add); const before = instance.getSnapshot(); const count = changed.mock.calls.length;
    instance.dispose(); writing.resolve(response(workspace(2))); expect(await mutation).toBe(false);
    expect(instance.getSnapshot()).toBe(before); expect(changed).toHaveBeenCalledTimes(count);
    expect(saved.values.has(STORAGE_KEY)).toBe(true); expect(await instance.mutate(add)).toBe(false);
  });

  it("同实例停止后重新 start，也只接受新代次 GET", async () => {
    const oldRead = deferred<Record<string, unknown>>(), newRead = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockImplementationOnce(() => oldRead.promise).mockImplementationOnce(() => newRead.promise);
    const instance = controller(request); instance.start(); instance.dispose(); instance.start();
    newRead.resolve(response(workspace(2, []))); await settle(); oldRead.resolve(response(workspace(9))); await settle();
    expect(instance.getSnapshot()).toMatchObject({ workspace: { revision: 2, items: [] }, loading: false });
  });

  it("verification 使用转义后的只读 endpoint，当前候选+当前 revision 才返回", async () => {
    const item = candidate("candidate/ ?#");
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(2, [item])))
      .mockResolvedValueOnce(verification({ candidate_id: item.candidate_id, workspace_revision: 2 }));
    const instance = await started(request); const result = await instance.verify(item, "CNY", "CN ?#");
    expect(result?.status).toBe("available");
    expect(request.mock.calls[1]).toEqual([`${BASE}/candidate%2F%20%3F%23/verification?target_currency=CNY&ship_to=CN%20%3F%23`]);
  });

  it("verification 报告更新 revision 时返回 null 并主动读取最新候选", async () => {
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace(1)))
      .mockResolvedValueOnce(verification({ workspace_revision: 3 })).mockResolvedValueOnce(response(workspace(3)));
    const instance = await started(request);
    expect(await instance.verify(candidate(), "CNY", "CN")).toBeNull(); await settle();
    expect(request.mock.calls.map(([path]) => path)).toEqual([BASE, `${BASE}/candidate-a/verification?target_currency=CNY&ship_to=CN`, BASE]);
    expect(instance.getSnapshot().workspace?.revision).toBe(3);
  });

  it.each(["revision 已变化", "候选已移除", "实例已卸载"])("verification 返回前%s，不传给下一步", async (change) => {
    const check = deferred<Record<string, unknown>>();
    const request = vi.fn<WorkspaceRequest>().mockResolvedValueOnce(response(workspace()))
      .mockImplementationOnce(() => check.promise).mockResolvedValueOnce(response(workspace(2, change === "候选已移除" ? [] : [candidate()])));
    const instance = await started(request); const verifying = instance.verify(candidate(), "CNY", "CN");
    if (change === "实例已卸载") instance.dispose(); else await instance.refresh();
    check.resolve(verification()); expect(await verifying).toBeNull();
  });
});
