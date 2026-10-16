// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import ProductCards from "../src/components/ProductCards";
import ProductHistory from "../src/components/ProductHistory";
import ProductDetail from "../src/components/ProductDetail";
import { selectionKey } from "../src/lib/candidateWorkspace";
import type { ProductCard } from "../src/types";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const product: ProductCard = {product_id: "P1", title: "历史测试商品", brand: "示例", category: "旅行", origin_country: "CN", price_major: 5, currency: "CNY", score: 1, highlights: [], default_sku_id: "S1",
  skus: [{sku_id: "S1", spec: "小号", price_major: 5, currency: "CNY", stock: 0}, {sku_id: "S2", spec: "大号", price_major: 9, currency: "CNY", stock: 5}]};
it("卡片保留需要显式SKU，不从首规格猜；已有候选按product+SKU识别", async () => {
  const host = document.createElement("div"), root = createRoot(host), save = vi.fn();
  try {
    await act(async () => root.render(<ProductCards products={[{...product, default_sku_id: undefined}]} onSaveCandidate={save} />));
    expect(host.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true);
    await act(async () => root.render(<ProductCards products={[product]} onSaveCandidate={save} candidateKeys={new Set([selectionKey("P1", "S2")])} />));
    expect(host.querySelector<HTMLButtonElement>('button')!.disabled).toBe(false);
    await act(async () => host.querySelector<HTMLButtonElement>('button')!.click());
    expect(save).toHaveBeenCalledWith(product, "S1");
    await act(async () => root.render(<ProductCards products={[product]} onSaveCandidate={save} candidateKeys={new Set([selectionKey("P1", "S1")])} />));
    expect(host.querySelector<HTMLButtonElement>('button')!.disabled).toBe(true); expect(host.textContent).toContain("已保留");
  } finally { await act(async () => root.unmount()); }
});
it("历史批次按自己的run保留原SKU，保存时缺货不禁用保留，详情来源保持该批次", async () => {
  const host = document.createElement("div"), root = createRoot(host), save = vi.fn(), detail = vi.fn();
  try {
    await act(async () => root.render(<ProductHistory batches={[{runId: "historical-run", updatedAt: 1, products: [product]}]} onSaveCandidate={save} onDetail={detail} />));
    expect(host.textContent).toContain("当时缺货");
    const buttons = [...host.querySelectorAll<HTMLButtonElement>('button')];
    await act(async () => buttons.find(button => button.textContent === "保留候选")!.click());
    expect(save).toHaveBeenCalledWith(product, "S1", "historical-run");
    await act(async () => buttons.find(button => button.textContent?.startsWith("查看详情"))!.click());
    expect(detail).toHaveBeenCalledWith(product, "historical-run");
  } finally { await act(async () => root.unmount()); }
});
it("历史详情可显式改SKU后保留，但不能直接准备或把未知默认SKU补成首规格", async () => {
  const host = document.createElement("div"), root = createRoot(host), save = vi.fn(), prepare = vi.fn(); document.body.append(host);
  try {
    await act(async () => root.render(<ProductDetail product={{...product, default_sku_id: undefined}} historical busy={false}
      onSaveCandidate={save} onClose={() => {}} onAsk={() => {}} onPrepare={prepare} />));
    const keep = [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === "保留当前规格候选")!;
    expect(keep.disabled).toBe(true);
    await act(async () => document.querySelector<HTMLInputElement>('input[value="S2"]')!.click());
    await act(async () => keep.click()); expect(save).toHaveBeenCalledWith(expect.objectContaining({product_id: "P1"}), "S2");
    const prepareButton = [...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.trim() === "准备下单意向")!;
    expect(prepareButton.disabled).toBe(true); expect(prepare).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("保存时规格商品价");
  } finally { await act(async () => root.unmount()); host.remove(); }
});
