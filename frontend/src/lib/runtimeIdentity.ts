import type { HttpAgentConfig } from "@ag-ui/client";

export interface BrowserStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}
export type VerifiedIdentity = { mode: "demo" | "hmac"; buyerId: string; expiresAt: string | null };
type Fetcher = NonNullable<HttpAgentConfig["fetch"]>;
export const LOGIN_REQUIRED = "登录凭证无效或已过期，请重新登录。刚才的写操作不会自动重发，请先核对已有记录。";

export function apiRoot(base: string, location = globalThis.location?.href ?? "http://localhost/"): string {
  const url = new URL(base || "/", location);
  if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.search || url.hash)
    throw new Error("API 地址必须为不含凭证、查询参数或片段的 HTTP(S) 地址。");
  return url.href.replace(/\/$/, "");
}
export function scopedStorage(storage: BrowserStorage | undefined, scope: string): BrowserStorage | undefined {
  if (!storage) return undefined;
  const prefix = `voyacart.identity.v1:${encodeURIComponent(scope)}:`;
  return { getItem: key => storage.getItem(prefix + key), setItem: (key, value) => storage.setItem(prefix + key, value) };
}

/** 身份只采用服务器校验结果。令牌仅放当前标签页存储，从不解析 sub 作为授权依据。 */
export class RuntimeIdentity {
  readonly base: string;
  readonly storageKey: string;
  private token = "";
  private generation = 0;
  private revoked = false;
  private durable = false;
  private fetcher: Fetcher;
  constructor(base: string, private storage?: BrowserStorage, fetcher?: Fetcher, private onUnauthorized: () => void = () => {}) {
    this.base = apiRoot(base);
    this.storageKey = `voyacart.runtime-token.v1:${this.base}`;
    this.fetcher = fetcher ?? globalThis.fetch;
  }
  get storageAvailable() { return this.durable; }
  private clearStoredToken() {
    try { this.storage?.setItem(this.storageKey, ""); } catch { /* 内存已清除，不回退到长期存储。 */ }
  }
  logout() {
    this.generation++;
    this.revoked = true;
    this.token = "";
    this.clearStoredToken();
  }
  async configure(): Promise<VerifiedIdentity | null> {
    const generation = ++this.generation;
    const response = await this.fetcher(`${this.base}/commerce/identity/config`, { redirect: "error", credentials: "omit" });
    if (!response.ok) throw new Error("暂时无法读取登录方式，请检查服务后重试。");
    const data = await response.json().catch(() => { throw new Error("登录服务返回格式无效，请稍后重试。"); });
    if (!data || typeof data !== "object") throw new Error("登录服务返回格式无效，请稍后重试。");
    if (generation !== this.generation) throw new Error("登录状态已改变，请重试。");
    if (data.mode === "demo" && typeof data.default_buyer_id === "string" && data.default_buyer_id.trim()) {
      this.token = ""; this.revoked = false; this.clearStoredToken();
      return { mode: "demo", buyerId: data.default_buyer_id, expiresAt: null };
    }
    if (data.mode !== "hmac") throw new Error("服务端登录配置无效，请联系演示维护者。");
    let saved = "";
    try { saved = this.storage?.getItem(this.storageKey) ?? ""; } catch { /* 存储受限时手动登录。 */ }
    return saved ? this.login(saved) : null;
  }
  async login(rawToken: string): Promise<VerifiedIdentity> {
    const token = rawToken.trim();
    if (!token || token.length > 8192 || /\s/.test(token)) throw new Error("请输入有效的短期演示凭证。");
    const generation = ++this.generation;
    const response = await this.fetcher(`${this.base}/commerce/identity`, {
      headers: { Authorization: `Bearer ${token}` }, redirect: "error", credentials: "omit",
    });
    if (generation !== this.generation) throw new Error("登录状态已改变，请重试。");
    if (response.status === 401) { this.logout(); throw new Error(LOGIN_REQUIRED); }
    if (!response.ok) throw new Error("身份验证暂时失败，请稍后重试。");
    const data = await response.json().catch(() => { throw new Error("登录服务返回格式无效，请稍后重试。"); });
    if (!data || typeof data !== "object") throw new Error("登录服务返回格式无效，请稍后重试。");
    if (generation !== this.generation) throw new Error("登录状态已改变，请重试。");
    if (data.mode !== "hmac" || typeof data.buyer_id !== "string" || !data.buyer_id.trim()
      || typeof data.expires_at !== "string" || !Number.isFinite(Date.parse(data.expires_at)) || Date.parse(data.expires_at) <= Date.now()) {
      this.logout(); throw new Error("服务端身份结果无效或已过期，请重新获取凭证。");
    }
    this.token = token; this.revoked = false;
    this.durable = false;
    try {
      this.storage?.setItem(this.storageKey, token);
      this.durable = this.storage?.getItem(this.storageKey) === token;
    } catch { /* 仅当前页面内存使用。 */ }
    return { mode: "hmac", buyerId: data.buyer_id, expiresAt: data.expires_at };
  }
  fetch: Fetcher = async (input, init) => {
    if (this.revoked) throw new Error(LOGIN_REQUIRED);
    const url = new URL(String(input), this.base + "/");
    const root = new URL(this.base);
    const prefix = root.pathname.replace(/\/$/, "") + "/commerce/";
    if (url.origin !== root.origin || !url.pathname.startsWith(prefix) || url.username || url.password)
      throw new Error("拒绝向配置外的 API 地址发送身份凭证。");
    const generation = this.generation;
    const headers = new Headers(init?.headers);
    headers.delete("Authorization");
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    const response = await this.fetcher(url.href, { ...init, headers, redirect: "error", credentials: "omit" });
    if (generation !== this.generation) throw new Error(LOGIN_REQUIRED);
    if (response.status === 401) {
      this.logout(); this.onUnauthorized(); throw new Error(LOGIN_REQUIRED);
    }
    return response;
  };
}
