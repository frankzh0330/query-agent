from __future__ import annotations

from typing import Any, Dict, Set

from matcher.base import DEFAULT_STOPWORDS, DEFAULT_SYNONYM_MAP, BaseMatcher, _stable_id


class MetricMatcher(BaseMatcher):
    """
    指标匹配器

    支持：
    1. exact alias 命中
    2. 中英混合分词
    3. 倒排索引召回
    4. RapidFuzz 重排
    """

    def _build_index(self, catalog_metrics: Dict[str, Dict[str, Any]], enable_jieba_userdict: bool = True) -> None:
        """
        构建指标索引

        :param catalog_metrics: 结构示例：
            {
                "pv": {
                    "aliases": ["PV", "浏览量", "访问量", "点击量"],
                    "indicator_show_name": "浏览量",
                    "event_indicator": "pv"
                },
                "uv": {
                    "aliases": ["UV", "独立访客", "访客数", "用户数"],
                    "indicator_show_name": "访客数",
                    "event_indicator": "uv"
                }
            }
        """
        tmp_index: Dict[str, Set[int]] = {}

        # 先分配 metric_id（使用稳定哈希 ID）
        for metric_name, info in catalog_metrics.items():
            metric_id = _stable_id(metric_name)  # 稳定的整数 ID
            aliases = info.get("aliases", []) or []
            all_aliases = [metric_name] + list(aliases)
            all_aliases = list(dict.fromkeys(all_aliases))  # 去重

            self.name_to_id[metric_name] = metric_id
            self.id_to_doc[metric_id] = {
                "metric_id": metric_id,
                "metric_name": metric_name,
                "indicator_show_name": info.get("indicator_show_name", metric_name),
                "event_indicator": info.get("event_indicator", metric_name),
                "aliases": all_aliases,
            }
            self.id_to_norm_aliases[metric_id] = []

            # 加入 jieba 词典
            if enable_jieba_userdict:
                self._add_to_jieba_dict(all_aliases)

        # 再建 exact map + 倒排索引
        for metric_id, doc in self.id_to_doc.items():
            self._build_inverted_index(metric_id, doc["aliases"], tmp_index)

        self.token_to_ids = self._finalize_index(tmp_index)

    def _get_name_by_id(self, doc_id: int) -> str:
        return self.id_to_doc[doc_id]["metric_name"]


def build_metric_matcher_from_catalog(catalog_metrics: Dict[str, Dict[str, Any]]) -> MetricMatcher:
    """从 catalog 创建 MetricMatcher"""
    return MetricMatcher(
        catalog_data=catalog_metrics,
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
    mock_catalog_metrics = {
        "pv": {
            "aliases": ["PV", "浏览量", "访问量", "点击量"],
            "indicator_show_name": "浏览量",
            "event_indicator": "pv"
        },
        "uv": {
            "aliases": ["UV", "独立访客", "访客数", "用户数"],
            "indicator_show_name": "访客数",
            "event_indicator": "uv"
        }
    }

    matcher = build_metric_matcher_from_catalog(mock_catalog_metrics)

    test_queries = [
        "PV",
        "pv",
        "浏览量",
        "访问量",
        "UV",
        "uv",
        "访客数",
        "用户数",
    ]

    for q in test_queries:
        result = matcher.match(q)
        print("=" * 60)
        print(f"query: {q}")
        print(f"matched: {result.matched}, score: {result.score}")
