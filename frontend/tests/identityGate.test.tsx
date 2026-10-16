// @vitest-environment jsdom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import IdentityGate, { useRuntimeIdentity } from "../src/components/IdentityGate";

let host:HTMLDivElement,root:Root,requests:{url:string;method:string;token:string|null}[], writeStatus:number;
function Workspace() {
  const {identity,runtime,storage}=useRuntimeIdentity();
  const [privateValue,setPrivateValue]=useState(()=>storage?.getItem("test-private")??"empty");
  return <section aria-label="测试买家页面"><p>{identity.buyerId}:{privateValue}</p>
    <button onClick={()=>{storage?.setItem("test-private",identity.buyerId+"-private");setPrivateValue(identity.buyerId+"-private");}}>保存私有缓存</button>
    <button onClick={()=>void runtime.fetch(`${runtime.base}/commerce/test-write`,{method:"POST",body:"{}"}).catch(()=>{})}>测试写操作</button></section>;
}
beforeEach(()=>{
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
  sessionStorage.clear();localStorage.clear();requests=[];writeStatus=200;
  vi.stubGlobal("fetch",vi.fn(async(url:string,init?:RequestInit)=>{
    const token=new Headers(init?.headers).get("Authorization");requests.push({url:String(url),method:init?.method??"GET",token});
    if(String(url).endsWith("/identity/config"))return Response.json({mode:"hmac"});
    if(String(url).endsWith("/identity"))return Response.json({mode:"hmac",buyer_id:token?.includes("token-b")?"buyer-b":"buyer-a",expires_at:new Date(Date.now()+60000).toISOString()});
    return new Response("{}",{status:writeStatus});
  }));
  host=document.createElement("div");document.body.append(host);root=createRoot(host);
});
afterEach(async()=>{await act(async()=>root.unmount());host.remove();vi.unstubAllGlobals();});
const click=async(text:string)=>{const button=[...host.querySelectorAll("button")].find(b=>b.textContent===text)!;expect(button).toBeTruthy();await act(async()=>button.dispatchEvent(new MouseEvent("click",{bubbles:true})));};
const mount=async()=>{await act(async()=>root.render(<IdentityGate><Workspace/></IdentityGate>));};
async function login(token:string){
  await act(async()=>{
    const input=host.querySelector<HTMLInputElement>("#runtime-token")!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")!.set!.call(input,token);
    input.dispatchEvent(new Event("input",{bubbles:true}));
  });
  await act(async()=>host.querySelector("form")!.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
}
it("未验证不创建买家界面；运行时输入后短期token只在sessionStorage，退出清除",async()=>{
  localStorage.setItem("voyacart.access-token","legacy-token");await mount();
  expect(host.querySelector('[aria-label="测试买家页面"]')).toBeNull();
  expect(localStorage.getItem("voyacart.access-token")).toBeNull();
  await login("token-a");expect(host.textContent).toContain("buyer-a:empty");
  expect(sessionStorage.getItem(`voyacart.runtime-token.v1:${location.origin}`)).toBe("token-a");
  expect(JSON.stringify(localStorage)).not.toContain("token-a");expect(host.textContent).not.toContain("token-a");
  await click("退出并切换身份");expect(host.querySelector('[aria-label="测试买家页面"]')).toBeNull();
  expect(sessionStorage.getItem(`voyacart.runtime-token.v1:${location.origin}`)).toBe("");
});
it("401立即返回登录入口；重新登录只恢复身份，不自动重发写请求",async()=>{
  await mount();await login("token-a");writeStatus=401;await click("测试写操作");
  expect(host.querySelector('[aria-label="测试买家页面"]')).toBeNull();expect(host.textContent).toContain("不会自动重发");
  expect(requests.filter(r=>r.method==="POST")).toHaveLength(1);
  await login("token-a-new");expect(host.textContent).toContain("buyer-a:empty");
  expect(requests.filter(r=>r.method==="POST")).toHaveLength(1);
});
it("两个已验证买家各自缓存隔离，切换卸载旧页面，回到原买家才恢复",async()=>{
  await mount();await login("token-a");await click("保存私有缓存");expect(host.textContent).toContain("buyer-a-private");
  await click("退出并切换身份");await login("token-b");expect(host.textContent).toContain("buyer-b:empty");expect(host.textContent).not.toContain("buyer-a-private");
  await click("保存私有缓存");await click("退出并切换身份");await login("token-a");
  expect(host.textContent).toContain("buyer-a:buyer-a-private");expect(host.textContent).not.toContain("buyer-b-private");
});
it("重载时先服务端验证已存token，过期token不能恢复缓存界面",async()=>{
  await mount();await login("token-a");await act(async()=>root.unmount());root=createRoot(host);
  vi.stubGlobal("fetch",vi.fn(async(url:string)=>String(url).endsWith("config")?Response.json({mode:"hmac"}):new Response("{}",{status:401})));
  await mount();expect(host.querySelector('[aria-label="测试买家页面"]')).toBeNull();expect(host.textContent).toContain("重新登录");
  expect(sessionStorage.getItem(`voyacart.runtime-token.v1:${location.origin}`)).toBe("");
});
