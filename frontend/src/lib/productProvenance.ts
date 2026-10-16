import type { ProductCard, ProductProvenance } from "../types";

type SourceProduct = Pick<Partial<ProductCard>, "data_provenance" | "source_locale" | "origin_country" | "material_tags">;
const sources = ["esci", "synthetic", "catalog", "test-fixture", "constructed_fixture"];
const syntheticFields = ["skus.price_major", "skus.stock", "ships_to", "price", "stock", "shipping"];
const derivedFields = ["category", "material_tags", "origin_country"];
const enums = {
  category_source: ["query-class-proxy-v1", "product-enrich-v1", "product-enrich-batch-v1"],
  category_method: ["train_exact_query_majority"],
  category_status: ["provisional_unreviewed"],
  material_status: ["unknown"],
  origin_country_method: ["source_locale_proxy"],
  source_text_processing: ["html_to_text; description_max_1000; bullet_max_200_and_5"],
} as const;

/** 复制白名单标量与数组；不把模型原文、未知状态或嵌套对象带入界面。 */
export function readProductProvenance(value: unknown): ProductCard["data_provenance"] {
  if (typeof value === "string") return sources.includes(value) ? value : undefined;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const data = value as Record<string, unknown>;
  if (typeof data.source !== "string" || !sources.includes(data.source) || !Array.isArray(data.synthetic) || !Array.isArray(data.derived)) return undefined;
  const result: ProductProvenance = { source: data.source,
    synthetic: data.synthetic.filter((item): item is string => typeof item === "string" && syntheticFields.includes(item)),
    derived: data.derived.filter((item): item is string => typeof item === "string" && derivedFields.includes(item)) };
  for (const key of Object.keys(enums) as (keyof typeof enums)[]) {
    const value = data[key];
    if (typeof value === "string" && (enums[key] as readonly string[]).includes(value)) {
      Object.assign(result, { [key]: value });
    }
  }
  return result;
}

function structured(product: SourceProduct): ProductProvenance | undefined {
  const source = readProductProvenance(product.data_provenance);
  return typeof source === "object" ? source : undefined;
}

export function isEsciProduct(product: SourceProduct) {
  return structured(product)?.source === "esci" || product.data_provenance === "esci";
}

export function isSyntheticProduct(product: SourceProduct) {
  return product.data_provenance === "synthetic" || structured(product)?.source === "synthetic";
}

export function hasDemoCommerce(product: SourceProduct) {
  // ESCI 的旧版本可能缺少 method，但价格、库存和配送从来不是商家实时数据。
  return isSyntheticProduct(product) || isEsciProduct(product) || !!structured(product)?.synthetic.length;
}

export function categoryProvenanceLabel(product: SourceProduct): string {
  if (isSyntheticProduct(product)) return "合成样例";
  const provenance = structured(product);
  if (provenance?.category_method === "train_exact_query_majority" || provenance?.category_source === "query-class-proxy-v1") {
    return "查询代理，未人工核验";
  }
  if (provenance?.category_source === "product-enrich-v1" || provenance?.category_source === "product-enrich-batch-v1") {
    return "模型推断，未人工核验";
  }
  if (provenance?.derived.includes("category") || isEsciProduct(product)) return "派生分类，未人工核验";
  return "来源未说明";
}

export function materialProvenanceLabel(product: SourceProduct): string {
  const provenance = structured(product);
  if (provenance?.material_status === "unknown" || !product.material_tags?.length) return "未知，不能据此判断有无某种材质";
  if (isSyntheticProduct(product)) return "合成样例";
  if (provenance?.category_source === "product-enrich-v1" || provenance?.category_source === "product-enrich-batch-v1") return "模型推断，未人工核验";
  if (provenance?.derived.includes("material_tags") || isEsciProduct(product)) return "派生属性，未人工核验";
  return "来源未说明";
}

export function originProvenance(product: SourceProduct): { label: string; value: string; note: string } {
  const site = product.source_locale?.split(/[-_]/).pop()?.toUpperCase();
  if (isEsciProduct(product) || structured(product)?.origin_country_method === "source_locale_proxy") {
    return { label: "来源站点", value: site || product.origin_country || "未提供", note: "站点代理，不代表发货地或产地" };
  }
  if (isSyntheticProduct(product)) return { label: "模拟国家/地区", value: product.origin_country || "未提供", note: "合成样例，不代表真实发货地或产地" };
  // origin_country 本身也不是仓库地址；缺少来源说明时不升级为发货地事实。
  return { label: "目录国家/地区", value: product.origin_country || "未提供", note: "来源未说明，实际发货地与产地待核验" };
}

export function sourceProcessingLabel(product: SourceProduct): string | undefined {
  if (structured(product)?.source_text_processing || isEsciProduct(product)) {
    return "原文 HTML 转纯文本；描述最多 1000 字符，原始要点每条最多 200 字符、最多 5 条；颜色另列。未提供商品译文。";
  }
  return undefined;
}
