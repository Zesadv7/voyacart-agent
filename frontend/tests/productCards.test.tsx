// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import ProductCards from "../src/components/ProductCards";
import ProductDetail from "../src/components/ProductDetail";
import ProductComparison from "../src/components/ProductComparison";
import ProductHistory from "../src/components/ProductHistory";
import ToolProductResult from "../src/components/ToolProductResult";
import type { ProductCard } from "../src/types";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const product: ProductCard = {
  product_id: "P100000", title: "Lightweight travel backpack 20L", brand: "Trail", category: "旅行装备",
  origin_country: "US", price_major: 210, currency: "CNY", source_price_major: 29, source_currency: "USD",
  score: 1, highlights: ["Lightweight fabric", "Folds into its own pocket", "Adjustable shoulder straps"],
  source_locale: "us", source_language: "en", external_product_id: "B-EXAMPLE",
  data_provenance: { source: "test-fixture", synthetic: ["price", "stock"], derived: [] },
  default_sku_id: "P100000-S1", skus: [{sku_id: "P100000-S1", spec: "Blue / 20L", price_major: 29, currency: "USD", stock: 18}],
  ships_to: ["US", "CN"], landed_price: {ship_to: "CN", subtotal_major: 210, freight_major: 30, tariff_major: 0,
    tariff_rate: 0, de_minimis_applied: true, landed_total_major: 240, currency: "CNY"},
};

it("商品卡展示原币单价、到手价、库存与前两条要点，保留收藏和比较", async () => {
  const host = document.createElement("div"), root = createRoot(host);
  const favorite = vi.fn(), compare = vi.fn();
  try {
    await act(async () => root.render(<ProductCards products={[product]} onFavorite={favorite} onCompare={compare} />));
    expect(host.textContent).toContain("演示数据");
    expect(host.textContent).toContain("US");
    expect(host.textContent).toContain("原币单价 USD");
    expect(host.textContent).toContain("240.00");
    expect(host.textContent).toContain("库存 18");
    expect(host.textContent).toContain("Folds into its own pocket");
    expect(host.textContent).not.toContain("Adjustable shoulder straps");
    expect(host.textContent).not.toMatch(/评分|重量/);
    await act(async () => host.querySelector<HTMLButtonElement>(".heart")!.click());
    await act(async () => host.querySelector<HTMLInputElement>("input")!.click());
    expect(favorite).toHaveBeenCalledWith(product); expect(compare).toHaveBeenCalledWith(product);
  } finally { await act(async () => root.unmount()); }
});

it.each(["us", "es", "jp"])("显示 %s 站点，详情保留全部要点并隐藏缺失的评分重量", async source_locale => {
  const host = document.createElement("div"), root = createRoot(host); document.body.append(host);
  try {
    await act(async () => root.render(<ProductDetail product={{...product, source_locale}} busy={false}
      onClose={() => {}} onCompare={() => {}} onAsk={() => {}} onPrepare={() => {}} />));
    expect(document.body.textContent).toContain(source_locale.toUpperCase());
    expect(document.body.textContent).toContain("Adjustable shoulder straps");
    expect(document.body.textContent).not.toMatch(/评分|重量/);
    expect(document.body.textContent).toContain("Blue / 20L");
  } finally { await act(async () => root.unmount()); host.remove(); }
});

it("比较使用当前选中规格，历史批次与工具结果复用文字卡片", async () => {
  const host = document.createElement("div"), root = createRoot(host); document.body.append(host);
  const selected = {...product, default_sku_id: "P100000-S2", skus: [...product.skus,
    {sku_id: "P100000-S2", spec: "Green / 30L", price_major: 39, currency: "USD", stock: 0}]};
  try {
    await act(async () => root.render(<ProductComparison products={[selected]} onClose={() => {}} />));
    expect(document.body.textContent).toContain("39.00");
    expect(document.body.textContent).toContain("缺货");
    expect(document.body.textContent).not.toContain("评分");
    await act(async () => root.render(<ProductHistory batches={[{runId: "r1", updatedAt: 1, products: [product]}]} />));
    expect(host.querySelector(".text-product-card")).not.toBeNull();
    expect(host.textContent).toContain("当时库存 18");
    expect(host.querySelector("button")).toBeNull();
    await act(async () => root.render(<ToolProductResult runId="r1" toolCallId="tool1" load={async () => ({
      runId: "r1", toolCallId: "tool1", status: "ready", products: [product], totalCount: 1, truncated: false,
    })} />));
    expect(host.querySelector(".text-product-card")).not.toBeNull();
    expect(host.textContent).toContain("当时库存 18");
  } finally { await act(async () => root.unmount()); host.remove(); }
});
