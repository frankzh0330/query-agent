"""表名匹配器 — BaseMatcher 的表目录实例

doc name = 表名（如 "orders"），aliases 来自 schema YAML。
匹配核心（倒排索引召回 + RapidFuzz 重排 + 同义词扩展）完全复用 BaseMatcher。
"""
from __future__ import annotations

from typing import Any, Dict, Set

from matcher.base import DEFAULT_STOPWORDS, DEFAULT_SYNONYM_MAP, BaseMatcher, _stable_id


class TableMatcher(BaseMatcher):
    def _build_index(self, catalog_tables: Dict[str, Dict[str, Any]], enable_jieba_userdict: bool = True) -> None:
        """
        :param catalog_tables: {"orders": {"aliases": ["订单", "订单表"], "columns": {...}}}
        """
        tmp_index: Dict[str, Set[int]] = {}

        for table_name, info in catalog_tables.items():
            table_id = _stable_id(table_name)
            aliases = info.get("aliases", []) or []
            all_aliases = [table_name] + list(aliases)
            all_aliases = list(dict.fromkeys(all_aliases))

            self.name_to_id[table_name] = table_id
            self.id_to_doc[table_id] = {
                "table_name": table_name,
                "time_column": info.get("time_column", ""),
                "aliases": all_aliases,
            }
            self.id_to_norm_aliases[table_id] = []

            if enable_jieba_userdict:
                self._add_to_jieba_dict(all_aliases)

        for table_id, doc in self.id_to_doc.items():
            self._build_inverted_index(table_id, doc["aliases"], tmp_index)

        self.token_to_ids = self._finalize_index(tmp_index)

    def _get_name_by_id(self, doc_id: int) -> str:
        return self.id_to_doc[doc_id]["table_name"]


def build_table_matcher_from_schema(catalog_tables: Dict[str, Dict[str, Any]]) -> TableMatcher:
    return TableMatcher(
        catalog_data=catalog_tables,
        stopwords=DEFAULT_STOPWORDS,
        synonym_map=DEFAULT_SYNONYM_MAP,
        fuzzy_threshold=70.0,
        max_candidates=200,
        max_posting_per_token=1000,
        enable_jieba_userdict=True,
    )
