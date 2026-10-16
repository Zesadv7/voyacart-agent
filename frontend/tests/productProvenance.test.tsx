// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { readProducts } from "../src/lib/commerceClient";
import ProductDetail from "../src/components/ProductDetail";
import ProductCards from "../src/components/ProductCards";
import ProductComparison from "../src/components/ProductComparison";
import ToolProductResult from "../src/components/ToolProductResult";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
const base = {
  product_id: "P100000", title: "Original product title", brand: "Example", category: "办公学习",
  origin_country: "US", source_platform: "amazon", source_locale: "us", source_language: "en",
  external_product_id: "B-EXAMPLE", price_major: 20, currency: "USD", score: 1,
  highlights: ["Original bullet"], material_tags: ["纸"], ships_to: ["US", "CN"],
  default_sku_id: "P100000-S1", skus: [{sku_id: "P100000-S1", spec: "Blue", price_major: 20, currency: "USD", stock: 8}],
};
const esci = { source: "esci", synthetic: ["skus.price_major", "skus.stock", "ships_to"],
  derived: ["category", "material_tags", "origin_country"] };
const demo = { ...esci, category_method: "train_exact_query_majority", category_status: "provisional_unreviewed",
  material_status: "unknown", origin_country_method: "source_locale_proxy",
  source_text_processing: "html_to_text; description_max_1000; bullet_max_200_and_5" };

async function renderDetail(input: unknown, check: (host: HTMLElement) => void) {
  const host = document.createElement("div"), root = createRoot(host); document.body.append(host);
  const [product] = readProducts([input]);
  try {
    await act(async () => root.render(<ProductDetail product={product} busy={false}
      onClose={() => {}} onCompare={() => {}} onAsk={() => {}} onPrepare={() => {}} />));
    check(document.body);
  } finally { await act(async () => root.unmount()); host.remove(); }
}

const rowText = (host: HTMLElement, label: string) => [...host.querySelectorAll("dt")]
  .find(node => node.textContent === label)?.nextElementSibling?.textContent;

describe("M3 目录来源展示", () => {
  it("保留已知来源字段，丢弃未知对象与未经支持的核验状态，复制来源数组", () => {
    const input = { ...base, data_provenance: { ...demo, category_source: "product-enrich-batch-v1",
      raw_model: { secret: "never-display" }, reviewer: "human" } };
    const [product] = readProducts([input]);
    expect(product.data_provenance).toEqual({ ...demo, category_source: "product-enrich-batch-v1" });
    if (typeof product.data_provenance === "object") product.data_provenance.derived.push("new");
    expect(input.data_provenance.derived).toEqual(esci.derived);
    const [unknown] = readProducts([{ ...base, data_provenance: { ...esci,
      category_source: { prompt: "never-display" }, category_method: "merchant_verified",
      category_status: "human_verified", material_status: "safe", origin_country_method: "warehouse",
      source_text_processing: { instructions: "never-display" } } }]);
    expect(unknown.data_provenance).toEqual(esci);
  });

  it("演示 ESCI 分类是查询代理，材质未知，来源站点不是发货地", async () => {
    await renderDetail({ ...base, material_tags: [], data_provenance: demo }, host => {
      expect(rowText(host, "来源站点")).toContain("US");
      expect(rowText(host, "来源站点")).toContain("不代表发货地或产地");
      expect(rowText(host, "发货地")).toBeUndefined();
      expect(rowText(host, "商品分类")).toContain("查询代理，未人工核验");
      expect(rowText(host, "材质")).toContain("未知");
      expect(host.textContent).toContain("价格、库存与配送为演示数据");
      expect(host.textContent).toContain("不构成真实报价、商家库存或履约保证");
      expect(host.textContent).toContain("HTML 转纯文本");
      expect(host.textContent).not.toContain("已翻译");
      expect(host.textContent).toContain(base.title);
    });
  });

  it.each(["product-enrich-batch-v1", "query-class-proxy-v1"])("旧 ESCI %s 没有 method 字段仍不能展示真实发货地", async category_source => {
    await renderDetail({ ...base, data_provenance: { ...esci, category_source } }, host => {
      expect(rowText(host, "发货地")).toBeUndefined();
      expect(rowText(host, "来源站点")).toContain("不代表发货地或产地");
      expect(rowText(host, "商品分类")).toContain(category_source.startsWith("product-")
        ? "模型推断，未人工核验" : "查询代理，未人工核验");
      expect(rowText(host, "材质")).toContain("未人工核验");
      expect(host.textContent).toContain("不能用于过敏、安全用途或无塑料保证");
    });
  });

  it("明确合成样例保留模拟来源，未知来源不能冒充已核验事实", async () => {
    await renderDetail({ ...base, data_provenance: "synthetic" }, host => {
      expect(rowText(host, "模拟国家/地区")).toContain("US");
      expect(rowText(host, "商品分类")).toContain("合成样例");
      expect(rowText(host, "发货地")).toBeUndefined();
    });
    const { source_locale: _locale, source_platform: _platform, ...unknown } = base;
    await renderDetail(unknown, host => {
      expect(rowText(host, "目录国家/地区")).toContain("来源未说明");
      expect(rowText(host, "商品分类")).toContain("来源未说明");
      expect(rowText(host, "发货地")).toBeUndefined();
      expect(host.textContent).not.toContain("模型推断");
      expect(host.textContent).not.toContain("合成样例");
    });
  });

  it("商品卡和比较保持派生分类警示，未展示原始对象元数据", async () => {
    const host = document.createElement("div"), root = createRoot(host); document.body.append(host);
    const [product] = readProducts([{...base, data_provenance: {...esci, category_source: "product-enrich-batch-v1", raw: {private: "never-display"}}}]);
    try {
      await act(async () => root.render(<ProductCards products={[product]} />));
      expect(host.textContent).toContain("模型推断，未人工核验");
      await act(async () => root.render(<ProductComparison products={[product]} onClose={() => {}} />));
      expect(document.body.textContent).toContain("模型推断，未人工核验");
      expect(document.body.textContent).not.toContain("never-display");
    } finally { await act(async () => root.unmount()); host.remove(); }
  });
  it("工具迷你卡保留来源警示但不补造缺失 SKU 或展示原始元数据", async () => {
    const host = document.createElement("div"), root = createRoot(host);
    const sparse = { product_id: base.product_id, title: base.title, price_major: 20, currency: "USD",
      category: base.category, source_locale: "us", data_provenance: { ...esci,
        category_source: "product-enrich-batch-v1", raw: { text: "never-display" } } };
    try {
      await act(async () => root.render(<ToolProductResult runId="r1" toolCallId="t1" load={async () => ({
        runId: "r1", toolCallId: "t1", status: "ready", products: [sparse], totalCount: 1, truncated: false,
      })} />));
      expect(host.textContent).toContain("模型推断，未人工核验");
      expect(host.textContent).toContain("演示数据");
      expect(host.textContent).toContain("当时库存未记录");
      expect(host.textContent).not.toContain("never-display");
    } finally { await act(async () => root.unmount()); }
  });

});
