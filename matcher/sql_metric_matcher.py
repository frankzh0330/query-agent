"""SQL 业务指标匹配器 — BaseMatcher 的指标目录实例

指标 = 业务口径（别名）→ 聚合表达式（如 revenue → sum(orders.amount)）。
doc name = metric_id，表达式由 MatcherService 从 schema 中查得。
"""
from __future__ import annotations

from typing import Any, Dict, Set

from matcher.base import DEFAULT_STOPWORDS, DEFAULT_SYNONYM_MAP, BaseMatcher, _stable_id


class SQLMetricMatcher(BaseMatcher):
    def _build_index(self, catalog_metrics: Dict[str, Dict[str, Any]], enable_jieba_userdict: bool = True) -> None:
        """
        :param catalog_metrics: {
            "revenue": {"aliases": ["销售额", "营收", "GMV"], "expr": "sum(orders.amount)"}
        }
        """
        tmp_index: Dict[str, Set[int]] = {}

        for metric_name, info in catalog_metrics.items():
            metric_id = _stable_id(metric_name)
            aliases = info.get("aliases", []) or []
            all_aliases = [metric_name] + list(aliases)
            all_aliases = list(dict.fromkeys(all_aliases))

            self.name_to_id[metric_name] = metric_id
            self.id_to_doc[metric_id] = {
                "metric_name": metric_name,
                "expr": info.get("expr", metric_name),
                "aliases": all_aliases,
            }
            self.id_to_norm_aliases[metric_id] = []

            if enable_jieba_userdict:
                self._add_to_jieba_dict(all_aliases)

        for metric_id, doc in self.id_to_doc.items():
            self._build_inverted_index(metric_id, doc["aliases"], tmp_index)

        self.token_to_ids = self._finalize_index(tmp_index)

    def _get_name_by_id(self, doc_id: int) -> str:
        return self.id_to_doc[doc_id]["metric_name"]


def build_sql_metric_matcher_from_schema(catalog_metrics: Dict[str, Dict[str, Any]]) -> SQLMetricMatcher:
    return SQLMetricMatcher(
        catalog_data=catalog_metrics,
        stopwords=DEFAULT_STOPWORDS,
        synonym_map=DEFAULT_SYNONYM_MAP,
        fuzzy_threshold=70.0,
        max_candidates=200,
        max_posting_per_token=1000,
        enable_jieba_userdict=True,
    )
