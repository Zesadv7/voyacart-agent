import type { PrepareOrderInput } from "../types";
import type { BrowserStorage } from "./runtimeIdentity";

export type PrepareAction = "create" | "cancel";
export type PrepareAttempt = { requestId: string; confirmationId?: string; warning: string | null };
export type PrepareLock = { request<T>(name: string, callback: () => Promise<T>): Promise<T> };
type RecordValue = { requestId: string; confirmationId?: string };
const PREFIX = "voyacart.prepare.v1:";
const SINGLE_TAB = "当前浏览器不支持跨标签页请求锁，请只在此标签页准备同一操作；本页刷新后仍会复用已保存的请求键。";
const MEMORY_ONLY = "请求键只能保留在本页内存。刷新或关闭前请先核对确认记录；重新打开后无法保证重试复用原请求键。";

/** 与服务端 v1 输入合同一致；不含报价，不在存储中保存地址副本。 */
export function canonicalPrepare(action: PrepareAction, input: PrepareOrderInput | { order_id: string; reason: string }): string {
  if (action === "cancel") {
    const cancel = input as { order_id: string; reason: string };
    return JSON.stringify({ request_version: 1, action, order_id: cancel.order_id.trim(), reason: cancel.reason.trim() });
  }
  const order = input as PrepareOrderInput;
  const grouped = new Map<string, PrepareOrderInput["items"][number]>();
  for (const item of order.items) {
    const product_id = item.product_id.trim(), sku_id = item.sku_id.trim();
    const key = JSON.stringify([product_id, sku_id]);
    const previous = grouped.get(key);
    grouped.set(key, { product_id, sku_id, quantity: (previous?.quantity ?? 0) + item.quantity });
  }
  const items = [...grouped.values()].sort((a, b) => a.product_id < b.product_id ? -1 : a.product_id > b.product_id ? 1 : a.sku_id < b.sku_id ? -1 : a.sku_id > b.sku_id ? 1 : 0);
  const address = order.shipping_address;
  const shipping_address = Object.fromEntries(["recipient_name", "country", "state", "city", "address_line", "postal_code", "phone"]
    .map(key => [key, (address[key as keyof typeof address] ?? "").trim()]));
  return JSON.stringify({ request_version: 1, action, items, shipping_address });
}
async function digest(value: string) {
  if (!globalThis.crypto?.subtle) throw new Error("浏览器无法安全生成请求摘要，请使用 HTTPS 或本机 localhost，并保留当前输入。");
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
}
function readRecord(raw: string | null): RecordValue | undefined {
  if (!raw) return undefined;
  const value: unknown = JSON.parse(raw);
  if (typeof value !== "object" || !value || !("requestId" in value) || typeof value.requestId !== "string" || !/^[a-f0-9-]{36}$/.test(value.requestId))
    throw new Error("已保存的请求键损坏，请先核对服务端确认记录，避免重复准备。");
  return value as RecordValue;
}

export class PrepareRequests {
  private memory = new Map<string, RecordValue>();
  private volatile = new Set<string>();
  private pending = new Map<string, Promise<PrepareAttempt>>();
  constructor(private storage?: BrowserStorage, private locks: PrepareLock | undefined = globalThis.navigator?.locks) {}
  private async key(scope: string, action: PrepareAction, canonical: string) {
    return PREFIX + await digest(JSON.stringify([scope, action, canonical]));
  }
  private async locked<T>(key: string, operation: () => Promise<T>): Promise<T> {
    return this.locks ? this.locks.request(key, operation) : operation();
  }
  async acquire(scope: string, action: PrepareAction, canonical: string, fresh = false): Promise<PrepareAttempt> {
    const key = await this.key(scope, action, canonical);
    const pending = this.pending.get(key);
    if (pending) return pending;
    const task = this.locked(key, async () => {
      let persistent = !!this.storage && !this.volatile.has(key);
      let saved: RecordValue | undefined;
      try {
        const raw = persistent ? this.storage?.getItem(key) ?? null : null;
        if (raw) {
          try { saved = readRecord(raw); }
          catch { throw new Error("已保存的请求键损坏，请先核对服务端确认记录，避免重复准备。"); }
        }
      } catch (error) {
        if (error instanceof Error && error.message.startsWith("已保存")) throw error;
        persistent = false;
      }
      saved ??= this.memory.get(key);
      const record = !fresh && saved ? saved : { requestId: crypto.randomUUID() };
      if (persistent) {
        try {
          this.storage!.setItem(key, JSON.stringify(record));
          if (this.storage!.getItem(key) !== JSON.stringify(record)) persistent = false;
        } catch { persistent = false; }
      }
      if (!persistent) this.volatile.add(key);
      this.memory.set(key, record);
      return { ...record, warning: !persistent ? MEMORY_ONLY : !this.locks ? SINGLE_TAB : null };
    });
    this.pending.set(key, task);
    try { return await task; }
    finally { if (this.pending.get(key) === task) this.pending.delete(key); }
  }
  async confirmed(scope: string, action: PrepareAction, canonical: string, requestId: string, confirmationId: string) {
    const key = await this.key(scope, action, canonical);
    await this.locked(key, async () => {
      let record = this.memory.get(key);
      try { if (!this.volatile.has(key)) record = readRecord(this.storage?.getItem(key) ?? null) ?? record; } catch { /* 不覆盖更新的尝试。 */ }
      if (record?.requestId !== requestId) return;
      const next = { requestId, confirmationId };
      this.memory.set(key, next);
      try { this.storage?.setItem(key, JSON.stringify(next)); } catch { /* 已提交结果不因缓存失败而变成失败。 */ }
    });
  }
}
