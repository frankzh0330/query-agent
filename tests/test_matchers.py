from __future__ import annotations

import pytest

from common.text_utils import (
    contains_chinese,
    is_single_chinese_char,
    normalize,
    tokenize_mixed,
)
from matcher.column_matcher import ColumnMatcher, build_column_matcher_from_schema
from matcher.schema_loader import load_sql_schema
from matcher.sql_metric_matcher import SQLMetricMatcher, build_sql_metric_matcher_from_schema
from matcher.table_matcher import TableMatcher, build_table_matcher_from_schema
from matcher.time_matcher import TimeMatcher
from matcher.matcher_service import MatcherService
from common.types import MatcherType

# =========================
# 测试数据
# =========================

MOCK_TABLES = {
    "orders": {
        "aliases": ["订单", "订单表", "下单", "order record"],
        "time_column": "created_at",
        "columns": {},
    },
    "users": {
        "aliases": ["用户", "用户表", "会员"],
        "columns": {},
    },
}

MOCK_COLUMNS = {
    "users.region": {
        "table": "users",
        "column": "region",
        "type": "LowCardinality(String)",
        "aliases": ["region", "地区", "区域"],
    },
    "users.vip_level": {
        "table": "users",
        "column": "vip_level",
        "type": "LowCardinality(String)",
        "aliases": ["会员等级", "VIP等级", "等级"],
    },
    "orders.amount": {
        "table": "orders",
        "column": "amount",
        "type": "Float64",
        "aliases": ["金额", "订单金额"],
    },
}

MOCK_METRICS = {
    "revenue": {
        "aliases": ["销售额", "营收", "GMV"],
        "expr": "sum(orders.amount)",
    },
    "order_count": {
        "aliases": ["订单量", "订单数", "单量"],
        "expr": "count()",
    },
}


# =========================
# text_utils 测试
# =========================

class TestTextUtils:
    """测试文本工具函数"""

    def test_normalize_lowercase(self):
        """测试小写转换"""
        assert normalize("APP_LAUNCH") == "app launch"

    def test_normalize_camel_case(self):
        """测试 camelCase 处理"""
        assert normalize("appLaunch") == "app launch"

    def test_normalize_underscore(self):
        """测试下划线处理"""
        assert normalize("app_launch") == "app launch"

    def test_normalize_special_chars(self):
        """测试特殊字符处理"""
        assert normalize("app@launch#test") == "app launch test"

    def test_tokenize_mixed_english(self):
        """测试英文分词"""
        tokens = tokenize_mixed("app launch test")
        assert "app" in tokens
        assert "launch" in tokens
        assert "test" in tokens

    def test_tokenize_mixed_chinese(self):
        """测试中文分词"""
        tokens = tokenize_mixed("订单表")
        assert len(tokens) > 0

    def test_tokenize_mixed_combined(self):
        """测试中英混合分词"""
        tokens = tokenize_mixed("orders订单")
        assert "orders" in tokens
        assert len(tokens) > 1

    def test_contains_chinese_true(self):
        """测试检测中文 - 包含中文"""
        assert contains_chinese("订单表") is True

    def test_contains_chinese_false(self):
        """测试检测中文 - 不包含中文"""
        assert contains_chinese("app launch") is False

    def test_is_single_chinese_char_true(self):
        """测试单字中文 - 是单字"""
        assert is_single_chinese_char("订") is True

    def test_is_single_chinese_char_false(self):
        """测试单字中文 - 不是单字"""
        assert is_single_chinese_char("订单") is False
        assert is_single_chinese_char("app") is False


# =========================
# TableMatcher 测试
# =========================

class TestTableMatcher:
    """测试表匹配器"""

    @pytest.fixture
    def matcher(self):
        return build_table_matcher_from_schema(MOCK_TABLES)

    def test_exact_match(self, matcher: TableMatcher):
        result = matcher.match("orders")
        assert result.matched == "orders"
        assert result.score == 100.0

    def test_alias_match(self, matcher: TableMatcher):
        result = matcher.match("订单表")
        assert result.matched == "orders"
        assert result.score == 100.0

    def test_fuzzy_match(self, matcher: TableMatcher):
        """英文 typo：token 召回 + RapidFuzz 重排（不依赖 jieba 全局词典状态）"""
        result = matcher.match("order recrd")
        assert result.matched == "orders"
        assert 70.0 <= result.score < 100.0

    def test_no_match(self, matcher: TableMatcher):
        result = matcher.match("xyz123不存在的")
        assert result.score < 100.0


# =========================
# ColumnMatcher 测试
# =========================

class TestColumnMatcher:
    """测试列匹配器（doc = table.column）"""

    @pytest.fixture
    def matcher(self):
        return build_column_matcher_from_schema(MOCK_COLUMNS)

    def test_exact_match(self, matcher: ColumnMatcher):
        result = matcher.match("region")
        assert result.matched == "users.region"

    def test_chinese_alias_match(self, matcher: ColumnMatcher):
        result = matcher.match("地区")
        assert result.matched == "users.region"

    def test_qualified_names_distinguish_tables(self, matcher: ColumnMatcher):
        """同名列可区分归属表：金额 → orders.amount 而不是别的表的列"""
        result = matcher.match("金额")
        assert result.matched == "orders.amount"

    def test_get_doc(self, matcher: ColumnMatcher):
        doc = matcher.get_doc("users.region")
        assert doc is not None
        assert doc["table"] == "users"
        assert doc["column"] == "region"


# =========================
# SQLMetricMatcher 测试
# =========================

class TestSQLMetricMatcher:
    """测试业务指标匹配器"""

    @pytest.fixture
    def matcher(self):
        return build_sql_metric_matcher_from_schema(MOCK_METRICS)

    def test_revenue_match(self, matcher: SQLMetricMatcher):
        result = matcher.match("销售额")
        assert result.matched == "revenue"

    def test_order_count_match(self, matcher: SQLMetricMatcher):
        result = matcher.match("订单量")
        assert result.matched == "order_count"

    def test_english_alias_match(self, matcher: SQLMetricMatcher):
        result = matcher.match("GMV")
        assert result.matched == "revenue"


# =========================
# TimeMatcher 测试
# =========================

class TestTimeMatcher:
    """测试时间匹配器"""

    @pytest.fixture
    def matcher(self):
        return TimeMatcher()

    def test_last_n_days_chinese(self, matcher: TimeMatcher):
        result = matcher.match("近7天")
        assert result.days == 7
        assert result.time_type == "last_n_days"

    def test_last_n_days_english(self, matcher: TimeMatcher):
        result = matcher.match("last 30 days")
        assert result.days == 30
        assert result.time_type == "last_n_days"

    def test_yesterday(self, matcher: TimeMatcher):
        result = matcher.match("昨天")
        assert result.days == 1
        assert result.time_type == "yesterday"

    def test_today(self, matcher: TimeMatcher):
        result = matcher.match("今天")
        assert result.days == 1
        assert result.time_type == "today"

    def test_this_week(self, matcher: TimeMatcher):
        result = matcher.match("本周")
        assert result.days == 7
        assert result.time_type == "this_week"

    def test_default(self, matcher: TimeMatcher):
        result = matcher.match("无效输入")
        assert result.days == 7
        assert result.time_type == "default"


# =========================
# SchemaLoader 测试
# =========================

class TestSchemaLoader:
    """测试 schema YAML 加载（真实 demo catalog）"""

    @pytest.fixture
    def schema(self):
        return load_sql_schema("catalog")

    def test_tables_loaded(self, schema):
        assert "orders" in schema.tables
        assert "users" in schema.tables
        assert "products" in schema.tables

    def test_columns_qualified_names(self, schema):
        assert "users.region" in schema.columns
        assert schema.columns["users.region"]["table"] == "users"

    def test_joins_loaded(self, schema):
        # YAML 1.1 会把裸 on 解析为布尔，condition key 必须可用
        assert len(schema.joins) == 5
        assert schema.joins[0]["condition"] == "orders.user_id = users.id"

    def test_metrics_expr(self, schema):
        assert schema.metrics["revenue"]["expr"] == "sum(orders.amount)"

    def test_find_join(self, schema):
        j = schema.find_join("users", "orders")
        assert j is not None
        assert j["condition"] == "orders.user_id = users.id"


# =========================
# 运行测试
# =========================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestEnumNormalization:
    """schema enum_values：过滤值的确定性规范化"""

    @pytest.fixture
    def schema(self):
        return load_sql_schema("catalog")

    @pytest.mark.parametrize("raw,expected,method", [
        ("credit_card", "credit_card", "exact"),
        ("credit card", "credit_card", "normalized"),
        ("Credit-Card", "credit_card", "normalized"),
        ("Gold", "gold", "normalized"),
        ("cancelled", "canceled", "fuzzy"),
        ("vip", "vip", "unmatched"),
    ])
    def test_normalize(self, schema, raw, expected, method):
        col = "users.vip_level" if raw in ("Gold", "vip") else (
            "orders.status" if raw == "cancelled" else "payments.payment_type")
        assert schema.normalize_enum_value(col, raw) == (expected, method)

    def test_non_enum_column_untouched(self, schema):
        assert schema.normalize_enum_value("orders.amount", "100") == ("100", "not_enum")


# =========================
# 召回增强与类型化阈值测试
# =========================

class TestRecallEnhancements:
    """IDF 加权召回 + typo 容忍（matcher/base.py）"""

    @pytest.fixture
    def schema_matcher(self):
        return build_table_matcher_from_schema(load_sql_schema("catalog").tables)

    def test_typo_in_every_token_still_recalls(self, schema_matcher):
        """'orde tablez' 两个 token 全是 typo：edit-distance-1 探测救回召回"""
        result = schema_matcher.match("orde tablez")
        assert result.matched == "orders"
        assert result.score >= 80.0
        assert result.explain["recall_explain"]["typo_matched"] == {
            "orde": "order", "tablez": "table",
        }

    def test_typo_probe_ignored_for_short_tokens(self, schema_matcher):
        """<4 字符 token 不做探测（避免 'id'/'if' 这类误纠）"""
        result = schema_matcher.match("id xyzq")
        assert result.explain["recall_explain"]["candidate_count"] == 0

    def test_idf_weighting_prefers_rare_token_doc(self):
        """命中数打平时，命中稀有 token 的文档胜出（朴素计数下按 doc id 任意排序）"""
        catalog = {
            "doc_a": {"aliases": ["alpha", "alpha two", "alpha three"], "columns": {}},
            "doc_b": {"aliases": ["alpha", "alpha four", "alpha five"], "columns": {}},
            "doc_c": {"aliases": ["alpha", "alpha six", "alpha seven"], "columns": {}},
            "doc_rare": {"aliases": ["zephyr dashboard"], "columns": {}},
        }
        m = build_table_matcher_from_schema(catalog)
        # 'alpha' 高 df（3/4 文档），'zephyr' 仅 1 个文档
        _, explain = m._recall_candidates(["alpha", "zephyr"])
        top = explain["top_candidates"][0]
        assert top["name"] == "doc_rare"
        # 原始命中数一致（各 1），但加权分不同
        others = [c for c in explain["top_candidates"] if c["name"] != "doc_rare"]
        assert all(top["score"] > c["score"] for c in others)

    def test_recall_explain_keeps_raw_hit_count(self, schema_matcher):
        _, explain = schema_matcher._recall_candidates(["order", "table"])
        for c in explain["top_candidates"]:
            assert "hit_count" in c and "score" in c


class TestTypeThresholds:
    """按类型阈值 + 并列歧义守卫（matcher_service.py）"""

    @staticmethod
    def _service_with(matcher, matcher_type):
        svc = MatcherService.__new__(MatcherService)
        setattr(svc, f"{matcher_type.value}_matcher", matcher)
        return svc

    def test_metric_auto_accept_is_stricter(self):
        """b02 回归：metric fuzzy 85.5 落入确认带（metric 阈值 90）"""
        svc = MatcherService.__new__(MatcherService)
        svc.metric_matcher = FakeMatcher({"customer lifetime value": _mr_with_cands(
            "avg_order_value", 85.5, [("avg_order_value", 85.5), ("revenue", 70.0)])})
        r = svc.resolve_with_candidates(MatcherType.METRIC, [_E("customer lifetime value")])
        assert r.needs_confirmation is True
        assert r.method == "fuzzy_low_confidence"

    def test_metric_exact_still_auto_accepted(self):
        svc = MatcherService.__new__(MatcherService)
        svc.metric_matcher = FakeMatcher({"gmv": _mr_with_cands("revenue", 100.0, [])})
        r = svc.resolve_with_candidates(MatcherType.METRIC, [_E("gmv")])
        assert r.needs_confirmation is False

    def test_tied_high_scores_force_confirmation(self):
        """table 95 分但第二名 88（分差<10）→ 并列歧义进确认"""
        svc = MatcherService.__new__(MatcherService)
        svc.table_matcher = FakeMatcher({"payment table": _mr_with_cands(
            "payments", 95.0, [("payments", 95.0), ("orders", 88.0)])})
        r = svc.resolve_with_candidates(MatcherType.TABLE, [_E("payment table")])
        assert r.needs_confirmation is True
        assert r.method == "fuzzy_tied"

    def test_clear_margin_auto_accepted(self):
        """95 分且领先第二名 >= 10 → 直接采纳"""
        svc = MatcherService.__new__(MatcherService)
        svc.table_matcher = FakeMatcher({"orde table": _mr_with_cands(
            "orders", 95.0, [("orders", 95.0), ("reviews", 73.0)])})
        r = svc.resolve_with_candidates(MatcherType.TABLE, [_E("orde table")])
        assert r.needs_confirmation is False


class _E:
    def __init__(self, text):
        self.text = text


class FakeMatcher:
    """text -> 预置 MatchResult 的假匹配器（仅测试用）"""

    def __init__(self, results=None):
        self.results = results or {}

    def match(self, text):
        if text in self.results:
            return self.results[text]
        from matcher.base import MatchResult
        return MatchResult(matched=None, score=0.0, explain={})


def _mr_with_cands(matched, score, candidates):
    from matcher.base import MatchResult
    explain = {"rerank_explain": {"top5": [{"name": v, "score": s} for v, s in candidates]}}
    return MatchResult(matched=matched, score=score, explain=explain)


class TestExactAliasCollision:
    """同名别名冲突：暴露歧义而非 first-wins（a01/a02 修复的单元层）"""

    @pytest.fixture
    def column_matcher(self):
        return build_column_matcher_from_schema(load_sql_schema("catalog").columns)

    def test_collision_detected(self, column_matcher):
        """'amount' 在 orders/payments 两列 → matched=None + 冲突候选"""
        r = column_matcher.match("amount")
        assert r.matched is None
        assert r.explain["method"] == "exact_alias_collision"
        names = {c["name"] for c in r.explain["collision_candidates"]}
        assert names == {"orders.amount", "payments.amount"}

    def test_three_way_collision(self, column_matcher):
        """'time' 三路冲突（orders/payments/reviews 的时间列）"""
        r = column_matcher.match("time")
        assert r.explain["method"] == "exact_alias_collision"
        assert len(r.explain["collision_candidates"]) == 3

    def test_unique_alias_unaffected(self, column_matcher):
        r = column_matcher.match("payment type")
        assert r.matched == "payments.payment_type"
        assert r.explain["method"] == "exact_alias_match"


class TestCollisionDistancePolicy:
    """exact 冲突的 join 距离消歧（matcher_service 层）"""

    @pytest.fixture
    def service(self):
        return MatcherService(catalog_path="catalog")

    def test_two_way_resolved_by_distance(self, service):
        """revenue 语境（base=orders）下 region：users 1 跳 vs sellers 2 跳 → 自动选 users"""
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("region")], base_table="orders")
        assert r.value == "users.region"
        assert r.method == "exact_collision_distance_resolved"
        assert r.needs_confirmation is False

    def test_no_base_table_confirms(self, service):
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("region")])
        assert r.needs_confirmation is True
        assert r.method == "exact_alias_collision"
        assert {c["value"] for c in r.candidates} == {"users.region", "sellers.region"}

    def test_three_way_always_confirms(self, service):
        """3 路超泛化词（time）即使有基表也不猜"""
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("time")], base_table="orders")
        assert r.needs_confirmation is True
        assert len(r.candidates) == 3

    def test_base_table_own_column_wins(self, service):
        """base=payments 下 amount：payments.amount 距离 0"""
        r = service.resolve_with_candidates(MatcherType.COLUMN, [_E("amount")], base_table="payments")
        assert r.value == "payments.amount"
        assert r.needs_confirmation is False
