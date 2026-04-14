from __future__ import annotations

import zlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import jieba
from rapidfuzz import fuzz, process

from common.text_utils import (
    contains_chinese,
    is_single_chinese_char,
    normalize,
    tokenize_mixed,
)

# =========================
# 配置项
# =========================

DEFAULT_STOPWORDS = {"", " ", "_", "-", "event", "埋点", "数据"}

DEFAULT_SYNONYM_MAP = {
    # 中文 -> 英文 / 同义词扩展
    "应用": ["app"],
    "启动": ["launch", "打开", "拉起"],
    "打开": ["launch", "启动"],
    "支付": ["payment"],
    "成功": ["success"],
    "失败": ["failed"],
    "播放": ["play"],
    "视频": ["video"],
    "注册": ["signup", "register"],
}

TIME_LIKE_WORDS = {"今天", "昨天", "近7天", "最近7天", "过去7天"}


# =========================
# 工具函数
# =========================

def _stable_id(name: str) -> int:
    """
    生成稳定的整数 ID

    - 相同名称永远产生相同的正整数 ID
    - ID 范围：0 ~ 2^31-1（避免负数）
    - 使用 CRC32 算法，速度快且分布均匀

    示例：
        _stable_id("app_launch") = 382517219
        _stable_id("payment_success") = 129485012
        # 任何顺序、任何次数启动，永远相同
    """
    return zlib.crc32(name.encode()) & 0x7fffffff


# =========================
# 数据结构
# =========================

@dataclass(frozen=True)
class MatchResult:
    """匹配结果"""
    matched: Optional[str]
    score: float
    explain: Dict[str, Any]


# =========================
# BaseMatcher 基类
# =========================

class BaseMatcher:
    """
    匹配器基类，提供：
    1. 文本规范化
    2. 中英混合分词
    3. 同义词扩展
    4. 倒排索引召回
    5. RapidFuzz 重排
    """

    def __init__(
        self,
        catalog_data: Dict[str, Dict[str, Any]],
        stopwords: Optional[Set[str]] = None,
        synonym_map: Optional[Dict[str, List[str]]] = None,
        fuzzy_threshold: float = 70.0,
        max_candidates: int = 200,
        max_posting_per_token: int = 1000,
        enable_jieba_userdict: bool = True,
        time_like_words: Optional[Set[str]] = None,
    ):
        """
        :param catalog_data: 配置数据，如 {"app_launch": {"aliases": ["应用启动"]}}
        :param stopwords: 停用词
        :param synonym_map: query 侧同义词扩展
        :param fuzzy_threshold: RapidFuzz 阈值
        :param max_candidates: 召回后最多保留多少候选
        :param max_posting_per_token: 每个 token 最多保留多少 posting
        :param enable_jieba_userdict: 是否把 alias 加入 jieba 词典
        :param time_like_words: 时间相关词汇（不参与匹配）
        """
        self.stopwords = stopwords or DEFAULT_STOPWORDS
        self.synonym_map = synonym_map or DEFAULT_SYNONYM_MAP
        self.fuzzy_threshold = fuzzy_threshold
        self.max_candidates = max_candidates
        self.max_posting_per_token = max_posting_per_token
        self.time_like_words = time_like_words or TIME_LIKE_WORDS

        # 主存储
        self.id_to_doc: Dict[int, Dict[str, Any]] = {}
        self.name_to_id: Dict[str, int] = {}

        # exact alias: normalized_alias -> id
        self.exact_alias_map: Dict[str, int] = {}

        # 倒排索引: token -> list[id]
        self.token_to_ids: Dict[str, List[int]] = {}

        # 每个 doc 对应的 normalized aliases（供 rerank 用）
        self.id_to_norm_aliases: Dict[int, List[str]] = {}

        # 子类实现具体索引构建
        self._build_index(catalog_data, enable_jieba_userdict=enable_jieba_userdict)

    def _build_index(self, catalog_data: Dict[str, Dict[str, Any]], enable_jieba_userdict: bool = True) -> None:
        """构建索引，子类需要实现"""
        raise NotImplementedError

    def _add_to_jieba_dict(self, aliases: List[str]) -> None:
        """将别名加入 jieba 词典"""
        for alias in aliases:
            if contains_chinese(alias):
                jieba.add_word(alias)

    def _build_inverted_index(
        self,
        doc_id: int,
        aliases: List[str],
        tmp_index: Dict[str, Set[int]],
    ) -> None:
        """为文档构建倒排索引"""
        MAX_ALIAS_LENGTH = 20  # 别名最大长度

        for alias in aliases:
            # 跳过过长的别名
            if len(alias) > MAX_ALIAS_LENGTH:
                continue

            norm_alias = normalize(alias)
            if not norm_alias:
                continue

            # exact alias map
            self.exact_alias_map.setdefault(norm_alias, doc_id)

            # 保存 normalized aliases 用于 rerank
            self.id_to_norm_aliases[doc_id].append(norm_alias)

            # tokenize for inverted index，限制最多 5 个 token
            tokens = tokenize_mixed(alias, max_tokens=5)

            for token in tokens:
                token = normalize(token)
                if not token or token in self.stopwords or token in self.time_like_words:
                    continue

                # 不索引中文单字，避免爆炸
                if is_single_chinese_char(token):
                    continue

                # 确保 token 在 tmp_index 中存在
                if token not in tmp_index:
                    tmp_index[token] = set()
                tmp_index[token].add(doc_id)

    def _finalize_index(self, tmp_index: Dict[str, Set[int]]) -> Dict[str, List[int]]:
        """完成索引构建，限制 posting 长度"""
        final_index: Dict[str, List[int]] = {}
        for token, ids in tmp_index.items():
            id_list = sorted(ids)
            if len(id_list) > self.max_posting_per_token:
                id_list = id_list[:self.max_posting_per_token]
            final_index[token] = id_list
        return final_index

    # =========================
    # 匹配主入口
    # =========================

    def match(self, query: str) -> MatchResult:
        """匹配入口"""
        norm_query = normalize(query)

        if not norm_query:
            return MatchResult(
                matched=None,
                score=0.0,
                explain={"method": "empty_query", "query": query},
            )

        # 1. exact alias 命中
        exact_id = self.exact_alias_map.get(norm_query)
        if exact_id is not None:
            return MatchResult(
                matched=self._get_name_by_id(exact_id),
                score=100.0,
                explain={
                    "method": "exact_alias_match",
                    "query": query,
                    "normalized_query": norm_query,
                    "matched_id": exact_id,
                    "matched_name": self._get_name_by_id(exact_id),
                },
            )

        # 2. query tokenize + synonym expand
        query_tokens = tokenize_mixed(query)
        expanded_tokens = self._expand_query_tokens(query_tokens)

        # 3. 倒排索引召回
        candidates, recall_explain = self._recall_candidates(expanded_tokens)

        if not candidates:
            return MatchResult(
                matched=None,
                score=0.0,
                explain={
                    "method": "no_candidates",
                    "query": query,
                    "normalized_query": norm_query,
                    "query_tokens": query_tokens,
                    "expanded_tokens": expanded_tokens,
                    "recall_explain": recall_explain,
                },
            )

        # 4. RapidFuzz 重排
        rerank_result = self._rerank(query, candidates)
        if rerank_result is None:
            return MatchResult(
                matched=None,
                score=0.0,
                explain={
                    "method": "rerank_failed",
                    "query": query,
                    "normalized_query": norm_query,
                    "query_tokens": query_tokens,
                    "expanded_tokens": expanded_tokens,
                    "recall_explain": recall_explain,
                },
            )

        best_id, best_score, rerank_explain = rerank_result
        best_name = self._get_name_by_id(best_id)

        if best_score < self.fuzzy_threshold:
            return MatchResult(
                matched=None,
                score=best_score,
                explain={
                    "method": "score_below_threshold",
                    "query": query,
                    "normalized_query": norm_query,
                    "query_tokens": query_tokens,
                    "expanded_tokens": expanded_tokens,
                    "threshold": self.fuzzy_threshold,
                    "recall_explain": recall_explain,
                    "rerank_explain": rerank_explain,
                },
            )

        return MatchResult(
            matched=best_name,
            score=best_score,
            explain={
                "method": "inverted_index_plus_rerank",
                "query": query,
                "normalized_query": norm_query,
                "query_tokens": query_tokens,
                "expanded_tokens": expanded_tokens,
                "matched_id": best_id,
                "matched_name": best_name,
                "score": best_score,
                "recall_explain": recall_explain,
                "rerank_explain": rerank_explain,
            },
        )

    def _get_name_by_id(self, doc_id: int) -> str:
        """根据 ID 获取名称，子类实现"""
        raise NotImplementedError

    # =========================
    # 召回与重排
    # =========================

    def _recall_candidates(self, expanded_tokens: List[str]) -> Tuple[List[int], Dict[str, Any]]:
        """从倒排索引召回候选"""
        hit_counts: Dict[int, int] = defaultdict(int)
        token_hits = []

        for token in expanded_tokens:
            token = normalize(token)
            if not token or token in self.stopwords or token in self.time_like_words:
                continue

            ids = self.token_to_ids.get(token, [])
            token_hits.append((token, len(ids)))

            for doc_id in ids:
                hit_counts[doc_id] += 1

        if not hit_counts:
            return [], {
                "token_hits": token_hits,
                "candidate_count": 0,
                "top_candidates": [],
            }

        ranked = sorted(hit_counts.items(), key=lambda x: (-x[1], x[0]))
        candidate_ids = [doc_id for doc_id, _ in ranked[:self.max_candidates]]

        explain = {
            "token_hits": token_hits,
            "candidate_count": len(candidate_ids),
            "top_candidates": [
                {
                    "id": doc_id,
                    "name": self._get_name_by_id(doc_id),
                    "hit_count": cnt,
                }
                for doc_id, cnt in ranked[:10]
            ],
        }
        return candidate_ids, explain

    def _rerank(self, query: str, candidate_ids: List[int]) -> Optional[Tuple[int, float, Dict[str, Any]]]:
        """RapidFuzz 重排"""
        norm_query = normalize(query)

        best_id = None
        best_score = 0.0
        top_details = []

        for doc_id in candidate_ids:
            norm_aliases = self.id_to_norm_aliases.get(doc_id, [])
            if not norm_aliases:
                continue

            # 在该 doc 的 aliases 中找最匹配的一个
            best_alias_match = process.extractOne(
                norm_query,
                norm_aliases,
                scorer=fuzz.WRatio,
            )

            if best_alias_match is None:
                continue

            matched_alias, score, _ = best_alias_match
            score = float(score)

            top_details.append({
                "id": doc_id,
                "name": self._get_name_by_id(doc_id),
                "matched_alias": matched_alias,
                "score": score,
            })

            if score > best_score:
                best_score = score
                best_id = doc_id

        if best_id is None:
            return None

        top_details.sort(key=lambda x: (-x["score"], x["name"]))

        explain = {
            "best_id": best_id,
            "best_name": self._get_name_by_id(best_id),
            "best_score": best_score,
            "top5": top_details[:5],
        }

        return best_id, best_score, explain

    # =========================
    # 同义词扩展
    # =========================

    def _expand_query_tokens(self, tokens: List[str]) -> List[str]:
        """只在 query 侧做同义词扩展，避免索引爆炸"""
        expanded: List[str] = []
        for token in tokens:
            if not token or token in self.stopwords:
                continue

            expanded.append(token)

            # 同义词扩展
            for syn in self.synonym_map.get(token, []):
                syn = normalize(syn)
                if syn and syn not in expanded:
                    expanded.append(syn)

        return expanded
