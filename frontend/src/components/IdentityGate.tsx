import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { RuntimeIdentity, LOGIN_REQUIRED, scopedStorage, type VerifiedIdentity, type BrowserStorage } from "../lib/runtimeIdentity";
import "./identityGate.css";

type Session = { identity: VerifiedIdentity; runtime: RuntimeIdentity; storage?: BrowserStorage };
const IdentityContext = createContext<Session | null>(null);
export function useRuntimeIdentity() {
  const session = useContext(IdentityContext);
  if (!session) throw new Error("请先验证当前买家身份。");
  return session;
}
export default function IdentityGate({ children }: { children: ReactNode }) {
  const [identity, setIdentity] = useState<VerifiedIdentity | null>(null);
  const [error, setError] = useState(""), [loading, setLoading] = useState(true), [token, setToken] = useState("");
  const [runtime] = useState(() => {
    let storage: Storage | undefined;
    try { storage = window.sessionStorage; } catch { /* 无持久凭据。 */ }
    // 清除旧版长期共享凭据，绝不继续读取或使用。
    try { window.localStorage.removeItem("voyacart.access-token"); } catch { /* 无长期存储访问。 */ }
    return new RuntimeIdentity(import.meta.env.VITE_API_BASE ?? "", storage, undefined, () => {
      setIdentity(null); setError(LOGIN_REQUIRED); setToken("");
    });
  });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let disposed = false;
    setLoading(true);
    void runtime.configure().then(result => { if (!disposed) { setIdentity(result); setError(""); } })
      .catch(e => { if (!disposed) { setIdentity(null); setError(e instanceof Error ? e.message : "登录暂时不可用。"); } })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [runtime, attempt]);
  useEffect(() => {
    if (!identity?.expiresAt) return;
    const timer = window.setTimeout(() => { runtime.logout(); setIdentity(null); setToken(""); setError(LOGIN_REQUIRED); },
      Math.max(0, Date.parse(identity.expiresAt) - Date.now()));
    return () => window.clearTimeout(timer);
  }, [runtime, identity]);
  let storage: BrowserStorage | undefined;
  try { storage = scopedStorage(window.localStorage, `${runtime.base}:${identity?.mode}:${identity?.buyerId}`); } catch { /* 可继续内存会话。 */ }
  if (identity) return <IdentityContext.Provider value={{ identity, runtime, storage }}>
    <div className="identity-status" aria-label="当前登录身份">
      <span>{identity.mode === "demo" ? "公开演示" : "已验证买家"} · {identity.buyerId}</span>
      {identity.mode === "hmac" && <><small>有效至 {new Date(identity.expiresAt!).toLocaleString("zh-CN")}</small>
        <button onClick={() => { runtime.logout(); setIdentity(null); setError(""); setToken(""); }}>退出并切换身份</button></>}
    </div>
    {identity.mode === "hmac" && !runtime.storageAvailable && <p className="identity-warning" role="status">浏览器不允许会话存储，凭证只留在本页内存；刷新后需要重新登录。</p>}
    <div key={`${identity.mode}:${identity.buyerId}`}>{children}</div>
  </IdentityContext.Provider>;
  return <main className="identity-gate"><section aria-labelledby="identity-title">
    <span>Voyacart · 私人演示</span><h1 id="identity-title">登录买家身份</h1>
    <p>请输入维护者通过受信渠道提供的短期签名凭证。凭证仅用于此 API，并在当前标签页会话中保留。</p>
    <p>这里不签发新凭证。登录后，请先核对订单与确认记录，再自行继续之前的操作。</p>
    {error && <p role="alert">{error}</p>}
    <form onSubmit={async event => {
      event.preventDefault(); if (loading || !token.trim()) return;
      setLoading(true); setError("");
      const submitted = token; setToken("");
      try { setIdentity(await runtime.login(submitted)); }
      catch (e) { setError(e instanceof Error ? e.message : "登录暂时不可用。"); }
      finally { setLoading(false); }
    }}><label htmlFor="runtime-token">短期演示凭证</label>
      <input id="runtime-token" type="password" autoComplete="off" value={token} maxLength={8192} disabled={loading} onChange={event => setToken(event.target.value)} />
      <button type="submit" disabled={loading || !token.trim()}>{loading ? "正在验证…" : "验证并进入"}</button>
    </form><button className="identity-retry" disabled={loading} onClick={() => setAttempt(current => current + 1)}>重新读取登录方式</button>
  </section></main>;
}
