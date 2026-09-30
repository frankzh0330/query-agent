"""LLM Cross-Encoder 重排器测试（mock LLM 调用）"""
from __future__ import annotations

import asyncio
from unittest import mock

from service import reranker


CANDIDATES = [
    {"value": "products", "score": 55.0},
    {"value": "orders", "score": 48.0},
]


def _run(coro):
    return asyncio.run(coro)


class TestRerankCandidates:

    def test_auto_accept_when_relevance_high_and_margin_wide(self):
        llm_result = {"scores": [
            {"value": "products", "relevance": 92, "reason": "货物即商品"},
            {"value": "orders", "relevance": 40, "reason": "不太可能"},
        ]}
        with mock.patch.object(reranker, "_rerank_via_llm", return_value=llm_result):
            reranked, explain = _run(reranker.rerank_candidates("近7天货物的销售额", "table", CANDIDATES))

        assert explain["applied"] is True
        assert explain["auto_accept"] is True
        assert explain["best"] == "products"
        assert explain["relevance"] == 92.0
        assert explain["margin"] == 52.0
        assert reranked[0]["value"] == "products"
        # recall 分保留
        assert reranked[0]["score"] == 55.0

    def test_keep_confirmation_when_ambiguous(self):
        """relevance 不够高 → 只重排不采纳"""
        llm_result = {"scores": [
            {"value": "orders", "relevance": 62},
            {"value": "products", "relevance": 58},
        ]}
        with mock.patch.object(reranker, "_rerank_via_llm", return_value=llm_result):
            reranked, explain = _run(reranker.rerank_candidates("看下单记录", "table", CANDIDATES))

        assert explain["applied"] is True
        assert explain["auto_accept"] is False
        assert reranked[0]["value"] == "orders"

    def test_no_margin_no_auto_accept(self):
        """top1 高分但与 top2 分差小 → 不采纳"""
        llm_result = {"scores": [
            {"value": "products", "relevance": 90},
            {"value": "orders", "relevance": 88},
        ]}
        with mock.patch.object(reranker, "_rerank_via_llm", return_value=llm_result):
            _, explain = _run(reranker.rerank_candidates("货物", "table", CANDIDATES))
        assert explain["auto_accept"] is False

    def test_invented_values_are_ignored(self):
        """LLM 发明的新值被丢弃（受限终选），未打分候选补 0"""
        llm_result = {"scores": [
            {"value": "HACKED_TABLE", "relevance": 99},
            {"value": "products", "relevance": 91},
        ]}
        with mock.patch.object(reranker, "_rerank_via_llm", return_value=llm_result):
            reranked, explain = _run(reranker.rerank_candidates("货物", "table", CANDIDATES))

        values = [c["value"] for c in reranked]
        assert "HACKED_TABLE" not in values
        assert set(values) == {"products", "orders"}
        assert explain["best"] == "products"
        # orders 未被打分 → relevance 0 排第二
        assert reranked[1]["value"] == "orders"
        assert reranked[1]["relevance"] == 0.0

    def test_llm_failure_falls_back_to_original_order(self):
        with mock.patch.object(reranker, "_rerank_via_llm", side_effect=RuntimeError("api down")):
            reranked, explain = _run(reranker.rerank_candidates("货物", "table", CANDIDATES))
        assert explain["applied"] is False
        assert "llm_error" in explain["reason"]
        assert reranked == CANDIDATES

    def test_single_candidate_skipped(self):
        reranked, explain = _run(reranker.rerank_candidates("q", "table", [{"value": "x", "score": 50}]))
        assert explain["applied"] is False
        assert explain["reason"] == "insufficient_candidates"


class TestEnvGate:

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("RERANKER_ENABLED", raising=False)
        assert reranker.is_reranker_enabled() is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("RERANKER_ENABLED", "true")
        assert reranker.is_reranker_enabled() is True
