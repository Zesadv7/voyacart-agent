import { describe, expect, it, vi } from "vitest";
import { apiRoot, RuntimeIdentity, scopedStorage } from "../src/lib/runtimeIdentity";
const storage = () => {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
};
const identity = (buyer_id = "buyer-a") => ({ mode: "hmac", buyer_id, expires_at: new Date(Date.now() + 3600000).toISOString() });

describe("运行时身份与固定API边界", () => {
  it("凭证仅发到身份验证Header，买家来自服务端，刷新先重新验证", async () => {
    const saved = storage(), calls: { url: string; headers: Headers }[] = [];
    const fetcher = vi.fn(async (url, init) => {
      calls.push({ url: String(url), headers: new Headers(init?.headers) });
      return Response.json(String(url).endsWith("config") ? { mode: "hmac" } : identity());
    });
    const runtime = new RuntimeIdentity("https://api.example", saved, fetcher);
    expect(await runtime.configure()).toBeNull();
    expect(await runtime.login(" synthetic.jwt.token ")).toMatchObject({ buyerId: "buyer-a" });
    expect(calls[1].url).toBe("https://api.example/commerce/identity");
    expect(calls[1].headers.get("Authorization")).toBe("Bearer synthetic.jwt.token");
    expect(calls.every(call => !call.url.includes("synthetic"))).toBe(true);
    const reloaded = new RuntimeIdentity("https://api.example", saved, fetcher);
    expect(await reloaded.configure()).toMatchObject({ buyerId: "buyer-a" });
    expect(fetcher).toHaveBeenCalledTimes(4);
    expect(saved.values.size).toBe(1);
  });
  it("公开演示使用服务端默认买家，不读取任何旧长期token", async () => {
    const fetcher = vi.fn(async () => Response.json({mode:"demo",default_buyer_id:"demo-only"}));
    const runtime = new RuntimeIdentity("https://api.example", storage(), fetcher);
    expect(await runtime.configure()).toEqual({mode:"demo",buyerId:"demo-only",expiresAt:null});
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).has("Authorization")).toBe(false);
  });
  it("401清除凭证只通知一次，禁止自动重发写请求，403不伪装成过期", async () => {
    const saved = storage(), unauthorized = vi.fn();
    let status = 401;
    const fetcher = vi.fn(async (url) => String(url).endsWith("/identity") ? Response.json(identity()) : new Response("{}", {status}));
    const runtime = new RuntimeIdentity("https://api.example", saved, fetcher, unauthorized);
    await runtime.login("synthetic-token");
    await expect(runtime.fetch("https://api.example/commerce/confirmations/orders", {method:"POST",body:"{}"})).rejects.toThrow("不会自动重发");
    await expect(runtime.fetch("https://api.example/commerce/orders", {})).rejects.toThrow("重新登录");
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(unauthorized).toHaveBeenCalledTimes(1);
    expect(saved.getItem(runtime.storageKey)).toBe("");
    status = 403; await runtime.login("replacement-token");
    expect((await runtime.fetch("https://api.example/commerce/orders", {})).status).toBe(403);
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });
  it("拒绝外部Origin/路径穿越/非commerce目标，禁止重定向和cookie", async () => {
    const fetcher = vi.fn(async () => Response.json(identity()));
    const runtime = new RuntimeIdentity("https://api.example/private", storage(), fetcher);
    await runtime.login("synthetic-token");
    for (const url of ["https://evil.example/commerce/orders", "https://api.example/private/commerce/../../secret", "https://api.example/commerce/orders", "https://name:secret@api.example/private/commerce/orders"])
      await expect(runtime.fetch(url, {})).rejects.toThrow("配置外");
    await runtime.fetch("https://api.example/private/commerce/orders", {headers:{Authorization:"Bearer overwritten"},redirect:"follow",credentials:"include"});
    const init = fetcher.mock.calls.at(-1)![1]!;
    expect(init.redirect).toBe("error"); expect(init.credentials).toBe("omit");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer synthetic-token");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it("已登出或切换后拒绝旧身份的迟到响应", async () => {
    let finish!: (response:Response)=>void;
    const runtime = new RuntimeIdentity("https://api.example", storage(), async url => String(url).endsWith("identity")
      ? Response.json(identity()) : new Promise<Response>(resolve => {finish=resolve;}));
    await runtime.login("old-token");
    const pending=runtime.fetch("https://api.example/commerce/orders",{});
    runtime.logout(); await runtime.login("new-token"); finish(Response.json({private:"old buyer data"}));
    await expect(pending).rejects.toThrow("重新登录");
  });
  it("无效身份/过期时间/伪造decode不放行；存储异常时仅内存可用", async () => {
    for (const data of [{mode:"hmac",buyer_id:"forged",expires_at:"bad"},{...identity(),expires_at:"2000-01-01T00:00:00Z"}, {mode:"demo",buyer_id:"forged"}]) {
      const runtime = new RuntimeIdentity("https://api.example", storage(), async () => Response.json(data));
      await expect(runtime.login("forged-sub-is-ignored")).rejects.toThrow("身份结果无效");
    }
    const runtime = new RuntimeIdentity("https://api.example", {getItem(){throw Error();},setItem(){throw Error();}}, async () => Response.json(identity()));
    expect(await runtime.login("memory-token")).toMatchObject({buyerId:"buyer-a"});
    expect(runtime.storageAvailable).toBe(false);
  });
  it("两个买家与API缓存命名空间隔离；不迁移旧共享缓存", () => {
    const saved = storage(); saved.setItem("voyacart.agui.sessions.v1", "old unverified buyer");
    const a=scopedStorage(saved,"https://api.example:hmac:a")!, b=scopedStorage(saved,"https://api.example:hmac:b")!;
    expect(a.getItem("voyacart.agui.sessions.v1")).toBeNull();
    a.setItem("sessions","buyer-a-secret"); b.setItem("sessions","buyer-b-secret");
    expect(a.getItem("sessions")).toBe("buyer-a-secret"); expect(b.getItem("sessions")).toBe("buyer-b-secret");
    expect(scopedStorage(saved,"https://other.example:hmac:a")!.getItem("sessions")).toBeNull();
  });
  it("API配置拒绝凭据与查询token", () => {
    for(const value of ["https://name:token@example.com","https://example.com/?token=secret","javascript:alert(1)","https://example.com/#token"])
      expect(()=>apiRoot(value)).toThrow();
  });
});
