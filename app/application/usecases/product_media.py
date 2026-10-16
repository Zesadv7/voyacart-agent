# -*- coding: utf-8 -*-
"""商品单图字段：图集裁切由独立展示注册表提供，不推断真实商品照片。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProductMedia:
    image_url: str | None
    image_kind: Literal["illustration", "placeholder"]
    image_alt: str


def product_media(product_id: str, title: str) -> ProductMedia:
    # 公开快照不附带来源材料待补的专属单图；前端通过精确 ID 注册表读取图集。
    # 单图字段不支持裁切，不能把整张图集当成某个商品的照片。
    return ProductMedia(None, "placeholder", f"{title}：暂无商品图片")
