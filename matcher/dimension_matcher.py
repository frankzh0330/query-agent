from __future__ import annotations

from typing import Any, Dict, Set

from matcher.base import DEFAULT_STOPWORDS, DEFAULT_SYNONYM_MAP, BaseMatcher, _stable_id


class DimensionMatcher(BaseMatcher):
    """
    维度匹配器，用于匹配 property_name（如 country）

    支持：
    1. exact alias 命中
    2. 中英混合分词
    3. 倒排索引召回
    4. RapidFuzz 重排
    5. query 侧同义词扩展
    """

    def _build_index(self, catalog_dimensions: Dict[str, Dict[str, Any]], enable_jieba_userdict: bool = True) -> None:
        """
        构建维度索引

        :param catalog_dimensions: 结构示例：
            {
                "country": {
                    "aliases": ["country", "国家"],
                    "property_type": "profile",
                    "property_name": "country",
                    "property_compose_type": "origin"
                }
            }
        """
        tmp_index: Dict[str, Set[int]] = {}

        # 先分配 dimension_id（使用稳定哈希 ID）
        for dimension_name, info in catalog_dimensions.items():
            dim_id = _stable_id(dimension_name)  # 稳定的整数 ID
            aliases = info.get("aliases", []) or []
            all_aliases = [dimension_name] + list(aliases)
            all_aliases = list(dict.fromkeys(all_aliases))  # 去重

            self.name_to_id[dimension_name] = dim_id
            self.id_to_doc[dim_id] = {
                "dimension_id": dim_id,
                "dimension_name": dimension_name,
                "property_name": info.get("property_name", dimension_name),
                "property_type": info.get("property_type", "profile"),
                "property_compose_type": info.get("property_compose_type", "origin"),
                "aliases": all_aliases,
            }
            self.id_to_norm_aliases[dim_id] = []

            # 加入 jieba 词典
            if enable_jieba_userdict:
                self._add_to_jieba_dict(all_aliases)

        # 再建 exact map + 倒排索引
        for dim_id, doc in self.id_to_doc.items():
            self._build_inverted_index(dim_id, doc["aliases"], tmp_index)

        self.token_to_ids = self._finalize_index(tmp_index)

    def _get_name_by_id(self, doc_id: int) -> str:
        return self.id_to_doc[doc_id]["dimension_name"]

    def match_property(self, query: str) -> tuple:
        """
        匹配并返回 property_name

        :return: (property_name, score, explain)
        """
        result = self.match(query)
        if result.matched:
            doc = self.id_to_doc[self.name_to_id[result.matched]]
            return doc["property_name"], result.score, result.explain
        return None, result.score, result.explain


# =========================
# 帮助函数：从 catalog 创建 matcher
# =========================

def build_dimension_matcher_from_catalog(catalog_dimensions: Dict[str, Dict[str, Any]]) -> DimensionMatcher:
    """从 catalog 创建 DimensionMatcher"""
    return DimensionMatcher(
        catalog_data=catalog_dimensions,
        stopwords=DEFAULT_STOPWORDS,
        synonym_map=DEFAULT_SYNONYM_MAP,
        fuzzy_threshold=70.0,
        max_candidates=200,
        max_posting_per_token=1000,
        enable_jieba_userdict=True,
    )


# =========================
# 本地测试
# =========================

if __name__ == "__main__":
    mock_catalog_dimensions = {
        "country": {
            "aliases": ["country", "国家", "地区"],
            "property_type": "profile",
            "property_name": "country",
            "property_compose_type": "origin"
        },
        "city": {
            "aliases": ["city", "城市"],
            "property_type": "profile",
            "property_name": "city",
            "property_compose_type": "origin"
        },
        "app_version": {
            "aliases": ["app_version", "应用版本", "版本"],
            "property_type": "profile",
            "property_name": "app_version",
            "property_compose_type": "origin"
        },
        "os": {
            "aliases": ["os", "操作系统", "系统"],
            "property_type": "profile",
            "property_name": "os",
            "property_compose_type": "origin"
        },
    }

    matcher = build_dimension_matcher_from_catalog(mock_catalog_dimensions)

    test_queries = [
        "country",
        "国家",
        "地区",
        "城市",
        "版本",
        "应用版本",
        "操作系统",
        "os",
    ]

    for q in test_queries:
        result = matcher.match(q)
        prop_name, score, explain = matcher.match_property(q)
        print("=" * 100)
        print("query:", q)
        print("matched dimension:", result.matched)
        print("property_name:", prop_name)
        print("score:", score)
