"""列匹配器 — BaseMatcher 的列目录实例

doc name = 限定列名 "table.column"（如 "users.region"），
因此同名列（orders.user_id 与 users.id 的别名都可能叫"用户ID"）可区分归属表，
歧义时由候选列表 + 确认流兜底。
"""
from __future__ import annotations

from typing import Any, Dict, Set

from matcher.base import DEFAULT_STOPWORDS, DEFAULT_SYNONYM_MAP, BaseMatcher, _stable_id


class ColumnMatcher(BaseMatcher):
    def _build_index(self, catalog_columns: Dict[str, Dict[str, Any]], enable_jieba_userdict: bool = True) -> None:
        """
        :param catalog_columns: {
            "users.region": {"table": "users", "column": "region",
                             "type": "LowCardinality(String)", "aliases": ["region", "地区"]}
        }
        """
        tmp_index: Dict[str, Set[int]] = {}

        for qualified_name, info in catalog_columns.items():
            col_id = _stable_id(qualified_name)
            aliases = info.get("aliases", []) or []
            all_aliases = list(dict.fromkeys(aliases))

            self.name_to_id[qualified_name] = col_id
            self.id_to_doc[col_id] = {
                "qualified_name": qualified_name,
                "table": info.get("table", qualified_name.split(".")[0]),
                "column": info.get("column", qualified_name.split(".")[-1]),
                "type": info.get("type", "String"),
                "aliases": all_aliases,
            }
            self.id_to_norm_aliases[col_id] = []

            if enable_jieba_userdict:
                self._add_to_jieba_dict(all_aliases)

        for col_id, doc in self.id_to_doc.items():
            self._build_inverted_index(col_id, doc["aliases"], tmp_index)

        self.token_to_ids = self._finalize_index(tmp_index)

    def _get_name_by_id(self, doc_id: int) -> str:
        return self.id_to_doc[doc_id]["qualified_name"]

    def get_doc(self, qualified_name: str) -> Dict[str, Any] | None:
        col_id = self.name_to_id.get(qualified_name)
        return self.id_to_doc.get(col_id) if col_id is not None else None


def build_column_matcher_from_schema(catalog_columns: Dict[str, Dict[str, Any]]) -> ColumnMatcher:
    return ColumnMatcher(
        catalog_data=catalog_columns,
        stopwords=DEFAULT_STOPWORDS,
        synonym_map=DEFAULT_SYNONYM_MAP,
        fuzzy_threshold=70.0,
        max_candidates=200,
        max_posting_per_token=1000,
        enable_jieba_userdict=True,
    )
