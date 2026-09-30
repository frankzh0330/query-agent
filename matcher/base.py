from __future__ import annotations

import math
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

# 停用词：索引和查询两侧都跳过的 token（exact 别名整串匹配不受影响）
# - "", " ", "_", "-": normalize() 残片防御
# - at: created_at/paid_at 等 snake_case 列名拆词副产物（实测 df=5，非用户意图词）
# - of: 指标别名 "number of X" 的介词（实测 df=4，纯功能词）
# - 其余为 L1 抽取短语常见介词/冠词（词表 df=0，清理 token_hits explain，
#   并避免长词误触 typo 探测）
# 注意：table/id/count/date 等高 df 词不停用——泛化词降权是 IDF 的职责
# （见 _recall_candidates），且 table 的 typo 探测行为被测试钉住
DEFAULT_STOPWORDS = {
    "", " ", "_", "-",
    "at", "of",
    "by", "per", "the", "for", "in", "and", "or", "with", "each", "every",
}

# 同义词扩展：只对 query 侧 token 生效（见 _expand_query_tokens），索引侧不扩展。
# 现行 English-only schema 的同义词能力由 sql_schema.yaml 每实体的 aliases 承担，
# 此处保留空表；仅当出现 schema 别名无法表达的词汇等价（如跨语言缩写）时再补条目。
# （旧条目为事件目录时代遗留：app_launch/payment_success 等事件实体已随迁移移除）
DEFAULT_SYNONYM_MAP = {}

TIME_LIKE_WORDS = {"今天", "昨天", "近7天", "最近7天", "过去7天"}

# 召回增强配置
_TYPO_MIN_TOKEN_LEN = 4   # 只对 ≥4 字符的零命中 token 做 edit-distance-1 探测
_TYPO_WEIGHT = 0.75       # typo 探测命中的 token 权重折减


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

        # exact alias: normalized_alias -> id（首注册者）
        self.exact_alias_map: Dict[str, int] = {}
        # exact 冲突：normalized_alias -> 所有命中该别名的 doc id（如 amount 在 orders/payments 两列）
        self.exact_alias_collisions: Dict[str, List[int]] = {}

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

            # exact alias map（冲突别名单独记录，查询时暴露歧义而非静默 first-wins）
            existing = self.exact_alias_map.get(norm_alias)
            if existing is None:
                self.exact_alias_map[norm_alias] = doc_id
            elif existing != doc_id:
                coll = self.exact_alias_collisions.setdefault(norm_alias, [existing])
                if doc_id not in coll:
                    coll.append(doc_id)

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

        # 0. exact 冲突优先：同一别名命中多个实体（如 amount / time / region）
        collision_ids = self.exact_alias_collisions.get(norm_query)
        if collision_ids:
            return MatchResult(
                matched=None,
                score=100.0,
                explain={
                    "method": "exact_alias_collision",
                    "query": query,
                    "normalized_query": norm_query,
                    "collision_candidates": [
                        {"name": self._get_name_by_id(d), "score": 100.0}
                        for d in collision_ids
                    ],
                },
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
        """从倒排索引召回候选（IDF 加权 + typo 容忍）

        与朴素命中计数的两点差异：
        - IDF 加权（BM25-lite）：df 高的泛化 token（如 table/amount/id）降权，
          判别性 token（如 freight）主导排序——多表时命中数打平，稀有 token 胜出
        - typo 容忍：零命中且长度达标的 token 做 edit-distance-1 词表探测
          （'orde tablez' -> order/table），权重打 75 折；模糊容忍不再只存在于重排段
        """
        hit_scores: Dict[int, float] = defaultdict(float)
        raw_counts: Dict[int, int] = defaultdict(int)
        token_hits = []
        typo_matched: Dict[str, str] = {}

        n_docs = max(1, len(self.id_to_doc))

        for token in expanded_tokens:
            token = normalize(token)
            if not token or token in self.stopwords or token in self.time_like_words:
                continue

            ids = self.token_to_ids.get(token, [])
            weight_factor = 1.0
            if not ids and len(token) >= _TYPO_MIN_TOKEN_LEN:
                probed = self._probe_typo_token(token)
                if probed:
                    typo_matched[token] = probed
                    ids = self.token_to_ids.get(probed, [])
                    weight_factor = _TYPO_WEIGHT

            token_hits.append((token, len(ids)))
            if not ids:
                continue

            df = len(ids)
            idf = math.log(1.0 + n_docs / (1.0 + df))
            weight = idf * weight_factor
            for doc_id in ids:
                hit_scores[doc_id] += weight
                raw_counts[doc_id] += 1

        if not hit_scores:
            return [], {
                "token_hits": token_hits,
                "typo_matched": typo_matched,
                "candidate_count": 0,
                "top_candidates": [],
            }

        ranked = sorted(hit_scores.items(), key=lambda x: (-x[1], x[0]))
        candidate_ids = [doc_id for doc_id, _ in ranked[:self.max_candidates]]

        explain = {
            "token_hits": token_hits,
            "typo_matched": typo_matched,
            "candidate_count": len(candidate_ids),
            "top_candidates": [
                {
                    "id": doc_id,
                    "name": self._get_name_by_id(doc_id),
                    "hit_count": raw_counts[doc_id],
                    "score": round(score, 3),
                }
                for doc_id, score in ranked[:10]
            ],
        }
        return candidate_ids, explain

    def _probe_typo_token(self, token: str) -> Optional[str]:
        """零命中 token 的 edit-distance-1 词表探测（词表遍历，规模到万级词时换 deletes-1 索引）"""
        from rapidfuzz.distance import Levenshtein

        for vocab in sorted(self.token_to_ids):
            if len(vocab) < _TYPO_MIN_TOKEN_LEN or abs(len(vocab) - len(token)) > 1:
                continue
            if Levenshtein.distance(token, vocab, score_cutoff=1) <= 1:
                return vocab
        return None

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
