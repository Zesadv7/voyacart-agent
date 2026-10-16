import { describe, expect, it } from "vitest";
import { CommerceClient } from "../src/lib/commerceClient";

const product = {
  product_id: "P1003",
  title: "旅行背包",
  price_major: 129,
  currency: "CNY",
  brand: "Wanderlite",
  category: "bag",
  origin_country: "CN",
  highlights: ["轻便"],
  skus: [],
  score: 0.8,
};
const sse = (events: unknown[]) =>
  new Response(
    events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
    { headers: { "Content-Type": "text/event-stream" } },
  );
type RequestBody = {
  threadId: string;
  runId: string;
  messages: Array<{ id: string; role: string; content: string }>;
};
const started = (body: RequestBody) => ({
  type: "RUN_STARTED",
  threadId: body.threadId,
  runId: body.runId,
});
const finished = (body: RequestBody) => ({
  type: "RUN_FINISHED",
  threadId: body.threadId,
  runId: body.runId,
});

describe("通过官方 SDK 消费真实 AG-UI 协议", () => {
  it("逐字内容被最终快照纠正，JSON Patch 更新商品，第二轮空结果清除旧卡", async () => {
    let calls = 0;
    const runIds: string[] = [];
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run",
      fetch: async (_, init) => {
        const body: RequestBody = JSON.parse(String(init.body));
        calls++;
        runIds.push(body.runId);
        expect(client.getSnapshot()).toMatchObject({ products: [], productsRunId: null });
        if (calls === 2)
          return sse([
            started(body),
            {
              type: "STATE_SNAPSHOT",
              snapshot: { products: [], searchCompleted: true, progress: [] },
            },
            finished(body),
          ]);
        expect(body.messages.at(-1)?.content).toBe("找旅行背包");
        return sse([
          started(body),
          {
            type: "TEXT_MESSAGE_START",
            messageId: "assistant-1",
            role: "assistant",
          },
          {
            type: "TEXT_MESSAGE_CONTENT",
            messageId: "assistant-1",
            delta: "流式草稿",
          },
          { type: "TEXT_MESSAGE_END", messageId: "assistant-1" },
          {
            type: "STATE_SNAPSHOT",
            snapshot: { products: [], searchCompleted: false, progress: [] },
          },
          {
            type: "STATE_DELTA",
            delta: [
              { op: "replace", path: "/products", value: [product] },
              { op: "replace", path: "/searchCompleted", value: true },
            ],
          },
          {
            type: "MESSAGES_SNAPSHOT",
            messages: [
              ...body.messages,
              { id: "assistant-1", role: "assistant", content: "审核后的建议" },
            ],
          },
          finished(body),
        ]);
      },
    });
    await client.submit("找旅行背包");
    expect(client.getSnapshot()).toMatchObject({
      status: "idle",
      products: [product],
      productsRunId: runIds[0],
      searchCompleted: true,
    });
    expect(client.getSnapshot().messages.at(-1)?.content).toBe("审核后的建议");
    await client.submit("不存在的商品");
    expect(client.getSnapshot()).toMatchObject({
      products: [],
      productsRunId: runIds[1],
      searchCompleted: true,
      status: "idle",
    });
  });

  it("工具参数结束与工具结果分别显示，不把参数结束伪装成成功", async () => {
    const client = new CommerceClient({
      url: "/run",
      fetch: async (_, init) => {
        const body: RequestBody = JSON.parse(String(init.body));
        return sse([
          started(body),
          {
            type: "TOOL_CALL_START",
            toolCallId: "call-1",
            toolCallName: "product_search_tool",
          },
          {
            type: "TOOL_CALL_ARGS",
            toolCallId: "call-1",
            delta: '{"query":"背包"}',
          },
          { type: "TOOL_CALL_END", toolCallId: "call-1" },
          {
            type: "TOOL_CALL_RESULT",
            messageId: "tool-result-1",
            toolCallId: "call-1",
            content: '{"hits":[]}',
            role: "tool",
          },
          finished(body),
        ]);
      },
    });
    await client.submit("背包");
    expect(
      client
        .getSnapshot()
        .events.filter((event) => event.type.startsWith("TOOL_"))
        .map((event) => event.label),
    ).toEqual(["调用工具", "工具参数已就绪", "收到工具结果"]);
  });

  it("RUN_ERROR 和没有终止事件的断流都保留失败状态", async () => {
    for (const isRunError of [true, false]) {
      const client = new CommerceClient({
        url: "/run",
        fetch: async (_, init) => {
          const body: RequestBody = JSON.parse(String(init.body));
          return sse([
            started(body),
            ...(isRunError
              ? [
                  {
                    type: "RUN_ERROR",
                    message: "模型暂不可用",
                    code: "UPSTREAM_ERROR",
                  },
                ]
              : []),
          ]);
        },
      });
      await client.submit("背包");
      expect(client.getSnapshot().status).toBe("error");
      expect(client.getSnapshot().error).toBeTruthy();
    }
  });

  it("HTTP 错误显示可恢复提示，不把服务器错误页直接渲染给用户", async () => {
    const client = new CommerceClient({
      url: "/run",
      fetch: async () =>
        new Response("<html>internal traceback</html>", { status: 503 }),
    });
    await client.submit("背包");
    expect(client.getSnapshot()).toMatchObject({
      status: "error",
      error: "选购服务暂时不可用，请稍后重试。",
    });
  });

  it("取消会中断 HTTP，迟到结果不会污染新会话", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    let firstBody: RequestBody | undefined;
    let signal: AbortSignal | null | undefined;
    const client = new CommerceClient({
      url: "/run",
      fetch: async (_, init) => {
        signal = init.signal;
        firstBody = JSON.parse(String(init.body));
        return new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        });
      },
    });
    const pending = client.submit("背包");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(client.getSnapshot().status).toBe("running");
    client.stop();
    expect(signal?.aborted).toBe(true);
    expect(client.getSnapshot().status).toBe("stopped");
    const oldSession = client.getSnapshot().sessionId;
    client.reset();
    expect(client.getSnapshot().sessionId).not.toBe(oldSession);
    if (!firstBody || !resolveRequest) throw new Error("未发出请求");
    resolveRequest(sse([started(firstBody),
      {type: "STATE_SNAPSHOT", snapshot: {products: [product], searchCompleted: true}},
      finished(firstBody)]));
    await pending;
    expect(client.getSnapshot()).toMatchObject({
      status: "idle",
      messages: [],
      products: [],
      productsRunId: null,
    });
  });

  it("本机历史可恢复，并且存储受限不会阻断运行", async () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => {
        values.set(key, value);
      },
    };
    const fetch = async (_: string, init: RequestInit) => {
      const body: RequestBody = JSON.parse(String(init.body));
      return sse([started(body), finished(body)]);
    };
    const first = new CommerceClient({ url: "/run", storage, fetch });
    await first.submit("第一次选购");
    const id = first.getSnapshot().sessionId;
    const second = new CommerceClient({ url: "/run", storage, fetch });
    second.setSession(id);
    expect(second.getSnapshot().messages[0].content).toBe("第一次选购");
    const restricted = new CommerceClient({
      url: "/run",
      fetch,
      storage: {
        getItem: () => {
          throw new Error("storage unavailable");
        },
        setItem: () => {
          throw new Error("quota");
        },
      },
    });
    await restricted.submit("正常运行");
    expect(restricted.getSnapshot().status).toBe("idle");
  });
});

it("没有浏览器缓存或对话记录时，按买家从服务端恢复选购表单会话",async()=>{
  const form={session_id:"draft-from-db",form_id:"form-a",created_at:Date.now(),revision:1};
  const client=new CommerceClient({url:"/commerce/ag-ui/run",fetch:async(url)=>{
    const body=String(url).includes("shopping-forms")?{form}:{sessions:[]};
    return new Response(JSON.stringify(body),{headers:{"Content-Type":"application/json"}});
  }});
  await client.initialize();
  expect(client.getSnapshot().sessionId).toBe("draft-from-db");
  expect(client.getSnapshot().shoppingForm).toEqual(form);
});

it("已保存的表单重复继续使用相同运行和消息 ID，不重复插入买家消息",async()=>{
  const sent:RequestBody[]=[];
  const client=new CommerceClient({url:"/run",fetch:async(_url,init)=>{
    const body=JSON.parse(String(init.body));sent.push(body);
    return sse([started(body),finished(body)]);
  }});
  const runId="form-run-"+"a".repeat(32);
  await client.submitForm("背包；单价300元",runId);
  await client.submitForm("背包；单价300元",runId);
  expect(sent).toHaveLength(2);
  expect(sent[0].runId).toBe(sent[1].runId);
  expect(sent[1].messages.filter(m=>m.id===runId+":user")).toHaveLength(1);
  expect(client.getSnapshot().messages.filter(m=>m.role==="user")).toHaveLength(1);
});

describe("自动化工作区的买家作用域", () => {
  it.each([
    ["/automations", "POST"],
    ["/automations/preview", "POST"],
    ["/automations/task-a", "PATCH"],
    ["/automations/task-a/runs", "POST"],
    ["/automation-runs/run-a/actions", "POST"],
  ])("%s 在请求正文附带当前买家，不能被表单覆盖", async (path, method) => {
    let requestBody: Record<string, unknown> = {};
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run", buyerId: "isolated-buyer",
      fetch: async (url, init) => {
        expect(String(url)).toContain("buyer_id=isolated-buyer");
        requestBody = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ok:true}));
      },
    });
    await client.workspaceRequest(path, method, {buyer_id:"another-buyer",request_id:"one"});
    expect(requestBody).toEqual({buyer_id:"isolated-buyer",request_id:"one"});
  });
});


describe("商品运行来源与工作区结构化错误", () => {
  it("只有 SDK 收到商品数组才绑定实际运行，忽略状态中的伪来源", async () => {
    const runIds: string[] = [];
    const client = new CommerceClient({ url: "/run", fetch: async (_, init) => {
      const body: RequestBody = JSON.parse(String(init.body));
      runIds.push(body.runId);
      return sse([
        started(body),
        { type: "STATE_SNAPSHOT", snapshot: runIds.length === 1
          ? {products: [product], productsRunId: "untrusted-state-run", searchCompleted: true}
          : {productsRunId: runIds[0], searchCompleted: false} },
        finished(body),
      ]);
    }});
    expect(client.getSnapshot().productsRunId).toBeNull();
    await client.submit("第一轮商品");
    expect(client.getSnapshot()).toMatchObject({products: [product], productsRunId: runIds[0]});
    await client.submit("本轮没有商品状态");
    expect(client.getSnapshot()).toMatchObject({products: [], productsRunId: null});
    expect(runIds[1]).not.toBe(runIds[0]);
  });

  it("商品来源独立于已完成运行的恢复指针持久化，刷新后仍能证明来源", async () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
    };
    let runId = "";
    const fetch = async (_: string, init: RequestInit) => {
      const body: RequestBody = JSON.parse(String(init.body));
      runId = body.runId;
      return sse([started(body), {type: "STATE_SNAPSHOT", snapshot: {products: [product], searchCompleted: true}}, finished(body)]);
    };
    const client = new CommerceClient({url: "/run", storage, fetch});
    await client.submit("保存商品");
    expect(client.getSnapshot()).toMatchObject({productsRunId: runId, recoverableRunId: null});
    const saved = JSON.parse(values.get("voyacart.agui.sessions.v1")!)[0];
    expect(saved).toMatchObject({productsRunId: runId, runId: null});
    const restored = new CommerceClient({url: "/run", storage, fetch});
    expect(restored.getSnapshot()).toMatchObject({products: [product], productsRunId: runId, recoverableRunId: null});
    restored.reset();
    expect(restored.getSnapshot()).toMatchObject({products: [], productsRunId: null});
    restored.setSession(saved.id);
    expect(restored.getSnapshot()).toMatchObject({products: [product], productsRunId: runId});
  });

  it.each([
    {status: 409, detail: {message: "候选清单版本冲突", code: "REVISION_CONFLICT", current_revision: 7}},
    {status: 422, detail: {message: "商品来源无效", code: "INVALID_SOURCE_RUN", current_revision: 0}},
  ])("透传 $status 错误的状态、代码和当前版本，并保留 Error 兼容性", async ({status, detail}) => {
    const client = new CommerceClient({url: "/commerce/ag-ui/run", fetch: async (url, init) => {
      expect(url).toContain("/candidate-lists?buyer_id=");
      expect(init.method).toBe("PATCH");
      expect(JSON.parse(String(init.body))).toEqual({expected_revision: 1});
      return new Response(JSON.stringify({detail}), {status, headers: {"Content-Type": "application/json"}});
    }});
    const error = await client.workspaceRequest("/candidate-lists", "PATCH", {expected_revision: 1}).catch(error => error);
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({message: detail.message, status, code: detail.code, current_revision: detail.current_revision, detail});
  });

  it.each([
    {status: 400, detail: "原有文本错误", message: "原有文本错误"},
    {status: 403, detail: null, message: "无法访问个人资料，请检查当前登录身份。"},
  ])("保留原有 $status 文本和默认错误提示", async ({status, detail, message}) => {
    const client = new CommerceClient({url: "/commerce/ag-ui/run", fetch: async () =>
      new Response(JSON.stringify({detail}), {status, headers: {"Content-Type": "application/json"}})});
    const error = await client.workspaceRequest("/candidate-lists").catch(error => error);
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({message, status});
    expect(error.code).toBeUndefined();
    expect(error.current_revision).toBeUndefined();
  });
});
