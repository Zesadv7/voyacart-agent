import { describe, expect, it, vi } from "vitest";
import { canonicalPrepare, PrepareRequests, type PrepareLock } from "../src/lib/prepareRequests";
import { CommerceClient } from "../src/lib/commerceClient";
import type { PrepareOrderInput, TradeConfirmation } from "../src/types";

const input: PrepareOrderInput = {
  items:[{product_id:"P1",sku_id:"S1",quantity:2}],
  shipping_address:{recipient_name:"合成测试人",country:"CN",state:"浙江",city:"杭州",address_line:"合成地址不应持久副本",postal_code:"",phone:""},
};
const canonical = canonicalPrepare("create", input);
function store() {
  const values=new Map<string,string>();
  return {values,getItem:(key:string)=>values.get(key)??null,setItem:(key:string,value:string)=>{values.set(key,value);}};
}
function locks():PrepareLock {
  const queues = new Map<string, Promise<unknown>>();
  return { request<T>(name:string, callback:()=>Promise<T>):Promise<T> {
    const next=(queues.get(name)??Promise.resolve()).catch(()=>{}).then(callback);
    queues.set(name,next);return next;
  }};
}
function confirmation(body:Record<string,any>, id="confirmation-1"):TradeConfirmation {
  return {confirmation_id:id,operation_id:"operation-1",action:"create",buyer_id:body.buyer_id,session_id:body.session_id,
    snapshot_hash:"a".repeat(64),expires_at:"2099-01-01T00:00:00Z",expired:false,status:"pending",result:null,
    payload:{items:[{...input.items[0],title:"合成商品",unit_price_minor:100,currency:"CNY"}],shipping_address:input.shipping_address,
      total_amount_minor:200,currency:"CNY",amount_scope:"merchandise_only",order_kind:"purchase_intent"}};
}
const json = (body:unknown)=>Response.json(body);

describe("prepare 稳定请求键",()=>{
  it("行排序、重复SKU聚合与全部字段trim规范等价，不合并跨商品相同SKU",()=>{
    const alternative={...input,items:[{product_id:" P1 ",sku_id:"S1",quantity:1},{product_id:"P1",sku_id:" S1 ",quantity:1}],
      shipping_address:Object.fromEntries(Object.entries(input.shipping_address).map(([key,value])=>[key,` ${value} `])) as any};
    expect(canonicalPrepare("create",alternative)).toBe(canonical);
    expect(canonicalPrepare("create",{...input,shipping_address:{...input.shipping_address,phone:undefined,postal_code:undefined} as any})).toBe(canonical);
    expect(JSON.parse(canonicalPrepare("create",{...input,items:[...input.items,{product_id:"P2",sku_id:"S1",quantity:1}]})).items).toHaveLength(2);
    expect(canonicalPrepare("cancel",{order_id:" order-1 ",reason:" change "})).toBe(canonicalPrepare("cancel",{order_id:"order-1",reason:"change"}));
  });
  it("两个客户端同锁同存储并发产生同一键，刷新继续；只保存摘要和ID",async()=>{
    const storage=store(), lock=locks();
    const a=new PrepareRequests(storage,lock),b=new PrepareRequests(storage,lock);
    const [one,two]=await Promise.all([a.acquire("buyer/session","create",canonical),b.acquire("buyer/session","create",canonical)]);
    expect(one.requestId).toBe(two.requestId);expect(one.warning).toBeNull();
    await a.confirmed("buyer/session","create",canonical,one.requestId,"confirmation-1");
    expect(await new PrepareRequests(storage,lock).acquire("buyer/session","create",canonical)).toMatchObject({requestId:one.requestId,confirmationId:"confirmation-1"});
    expect(JSON.stringify([...storage.values])).not.toContain(input.shipping_address.address_line);
    expect([...storage.values.keys()][0]).toMatch(/^voyacart.prepare.v1:[a-f0-9]{64}$/);
  });
  it("新载荷/买家/会话/action隔离；显式新尝试换键且不被旧确认回写覆盖",async()=>{
    const storage=store(), manager=new PrepareRequests(storage,locks());
    const one=await manager.acquire("buyer/session","create",canonical);
    for(const [scope,action,payload] of [["other/session","create",canonical],["buyer/other","create",canonical],["buyer/session","cancel",canonical],
      ["buyer/session","create",canonicalPrepare("create",{...input,items:[{...input.items[0],quantity:3}]})]] as const)
      expect((await manager.acquire(scope,action,payload)).requestId).not.toBe(one.requestId);
    const two=await manager.acquire("buyer/session","create",canonical,true);
    expect(two.requestId).not.toBe(one.requestId);
    await manager.confirmed("buyer/session","create",canonical,one.requestId,"stale-confirmation");
    expect(await manager.acquire("buyer/session","create",canonical)).toMatchObject({requestId:two.requestId});
    expect((await manager.acquire("buyer/session","create",canonical)).confirmationId).toBeUndefined();
  });
  it.each([undefined, {locks:undefined}])("无锁仍持久刷新键并明确双标签限制；不可写退回本页且不误用旧持久键（navigator=%j）",async(navigator)=>{
    // Node 24 也提供 navigator.locks；显式模拟缺少浏览器锁，而非依赖运行时版本。
    vi.stubGlobal("navigator",navigator);
    try {
      const storage=store(), first=new PrepareRequests(storage);
      const one=await first.acquire("scope","create",canonical);
      expect(one.warning).toContain("不支持跨标签页");
      expect((await new PrepareRequests(storage).acquire("scope","create",canonical)).requestId).toBe(one.requestId);
      const blocked={getItem:storage.getItem,setItem(){throw Error("quota");}};
      const memory=new PrepareRequests(blocked);
      const two=await memory.acquire("scope","create",canonical,true);
      expect(two.warning).toContain("本页内存");expect(two.requestId).not.toBe(one.requestId);
      expect((await memory.acquire("scope","create",canonical)).requestId).toBe(two.requestId);
    } finally { vi.unstubAllGlobals(); }
  });
  it("未注入锁时使用浏览器提供的锁，不误报双标签限制",async()=>{
    const lock={request:vi.fn(locks().request)};
    vi.stubGlobal("navigator",{locks:lock});
    try {
      const result=await new PrepareRequests(store()).acquire("scope","create",canonical);
      expect(result.warning).toBeNull();
      expect(lock.request).toHaveBeenCalledTimes(1);
    } finally { vi.unstubAllGlobals(); }
  });
  it("损坏持久键阻止静默换键",async()=>{
    const storage=store(), manager=new PrepareRequests(storage,locks());
    await manager.acquire("scope","create",canonical);
    storage.values.set([...storage.values.keys()][0],"not-json");
    await expect(manager.acquire("scope","create",canonical)).rejects.toThrow("请求键损坏");
  });
  it("第一次写请求前已保存请求键和空会话；丢响应后刷新仍用同一键",async()=>{
    const storage=store(), calls:any[]=[];
    const fetcher=vi.fn(async(_url,init)=>{
      if(init?.method!=="POST") return String(_url).includes("/sessions?") ? json({sessions:[{id:"unrelated-old-session",title:"旧对话",updatedAt:1}]}) : json({});
      const body=JSON.parse(String(init.body));calls.push(body);
      expect(storage.getItem("voyacart.agui.active-session")).toBe(body.session_id);
      expect([...storage.values.values()].some(value=>value.includes(body.request_id))).toBe(true);
      if(calls.length===1)throw Error("response lost after commit");
      return json({confirmation:confirmation(body)});
    });
    const first=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"b",storage,fetch:fetcher,prepareLocks:locks()});
    expect(await first.prepareOrder(input)).toBe(false);
    const reload=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"b",storage,fetch:fetcher,prepareLocks:locks()});
    await reload.initialize();
    expect(reload.getSnapshot().sessionId).toBe(first.getSnapshot().sessionId);
    expect(await reload.prepareOrder(input)).toBe(true);
    expect(calls[1].request_id).toBe(calls[0].request_id);
    expect(JSON.stringify([...storage.values])).not.toContain(input.shipping_address.address_line);
  });
  it("响应解析失败保留键；成功后重复点击仍用原键；显式新购买才换键",async()=>{
    const calls:any[]=[], storage=store();
    const client=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"b",storage,prepareLocks:locks(),fetch:async(_url,init)=>{
      const body=JSON.parse(String(init.body));calls.push(body);
      return calls.length===1?new Response("truncated"):json({confirmation:confirmation(body)});
    }});
    expect(await client.prepareOrder(input)).toBe(false);
    expect(await client.prepareOrder(input)).toBe(true);
    expect(await client.prepareOrder(input)).toBe(true);
    expect(new Set(calls.map(body=>body.request_id)).size).toBe(1);
    expect(await client.prepareOrder(input,{newAttempt:true})).toBe(true);
    expect(calls[3].request_id).not.toBe(calls[0].request_id);
    expect(client.getSnapshot().confirmations).toHaveLength(1);
  });
  it("重新核验以原确认卡为尝试锚点；同卡失败重试/刷新不换键，新卡另开尝试",async()=>{
    const calls:any[]=[], storage=store(), lock=locks();
    const fetcher=async(_url:any,init:any)=>{
      const body=JSON.parse(String(init.body));calls.push(body);
      if(calls.length===2)throw Error("lost");
      return json({confirmation:confirmation(body,`confirmation-${calls.length}`)});
    };
    const first=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"b",storage,prepareLocks:lock,fetch:fetcher});
    await first.prepareOrder(input);const original=first.getSnapshot().confirmations[0];
    expect(await first.revalidateConfirmation(original)).toBe(false);
    const reload=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"b",storage,prepareLocks:lock,fetch:fetcher});
    expect(await reload.revalidateConfirmation(original)).toBe(true);
    expect(calls[1].request_id).not.toBe(calls[0].request_id);expect(calls[2].request_id).toBe(calls[1].request_id);
    await reload.revalidateConfirmation(reload.getSnapshot().confirmations[0]);
    expect(calls[3].request_id).not.toBe(calls[2].request_id);
  });
  it("取消请求重试同键，变更原因或订单换键，resolve不携带prepare键",async()=>{
    const calls:any[]=[], client=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"b",storage:store(),prepareLocks:locks(),fetch:async(_url,init)=>{
      const body=JSON.parse(String(init.body));calls.push(body);return json({confirmation:confirmation(body)});
    }});
    await client.prepareCancel("order-1"," reason ");await client.prepareCancel("order-1","reason");
    await client.prepareCancel("order-1","other");await client.prepareCancel("order-2","other");
    expect(calls[0].request_id).toBe(calls[1].request_id);expect(new Set(calls.map(body=>body.request_id)).size).toBe(3);
    await client.resolveConfirmation(client.getSnapshot().confirmations[0],false);
    expect(calls[4].request_id).toBeUndefined();
  });
  it("切换会话后摘要仍在计算也不得把旧写请求发出去",async()=>{
    const fetcher=vi.fn(async()=>json({}));
    const client=new CommerceClient({url:"/commerce/ag-ui/run",buyerId:"b",storage:store(),prepareLocks:locks(),fetch:fetcher});
    const pending=client.prepareOrder(input);client.reset();
    expect(await pending).toBe(false);expect(fetcher).not.toHaveBeenCalled();
  });
});
