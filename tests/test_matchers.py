from __future__ import annotations

import pytest

from common.text_utils import (
    contains_chinese,
    is_single_chinese_char,
    normalize,
    tokenize_mixed,
)
from matcher.dimension_matcher import (
    DimensionMatcher,
    build_dimension_matcher_from_catalog,
)
from matcher.event_matcher import EventMatcher, build_event_matcher_from_catalog
from matcher.metric_matcher import MetricMatcher, build_metric_matcher_from_catalog
from matcher.time_matcher import TimeMatcher

# =========================
# 测试数据
# =========================

MOCK_EVENTS = {
    "app_launch": {
        "aliases": ["应用启动", "启动应用", "app启动", "打开app", "打开应用"]
    },
    "payment_success": {
        "aliases": ["支付成功", "支付完成", "payment success"]
    },
    "video_play_start": {
        "aliases": ["开始播放", "视频开始播放", "video play start"]
    },
}

MOCK_DIMENSIONS = {
    "country": {
        "aliases": ["country", "国家", "地区"],
        "property_name": "country",
    },
    "city": {
        "aliases": ["city", "城市"],
        "property_name": "city",
    },
}

MOCK_METRICS = {
    "pv": {
        "aliases": ["PV", "浏览量", "访问量"],
    },
    "uv": {
        "aliases": ["UV", "独立访客", "访客数"],
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
        tokens = tokenize_mixed("应用启动")
        assert len(tokens) > 0

    def test_tokenize_mixed_combined(self):
        """测试中英混合分词"""
        tokens = tokenize_mixed("app_launch启动")
        assert "app" in tokens
        assert "launch" in tokens
        assert "启动" in tokens

    def test_contains_chinese_true(self):
        """测试检测中文 - 包含中文"""
        assert contains_chinese("应用启动") is True

    def test_contains_chinese_false(self):
        """测试检测中文 - 不包含中文"""
        assert contains_chinese("app launch") is False

    def test_is_single_chinese_char_true(self):
        """测试单字中文 - 是单字"""
        assert is_single_chinese_char("应") is True

    def test_is_single_chinese_char_false(self):
        """测试单字中文 - 不是单字"""
        assert is_single_chinese_char("应用") is False
        assert is_single_chinese_char("app") is False


# =========================
# EventMatcher 测试
# =========================

class TestEventMatcher:
    """测试事件匹配器"""

    @pytest.fixture
    def matcher(self):
        return build_event_matcher_from_catalog(MOCK_EVENTS)

    def test_exact_match(self, matcher: EventMatcher):
        """测试精确匹配"""
        result = matcher.match("app_launch")
        assert result.matched == "app_launch"
        assert result.score == 100.0

    def test_alias_match(self, matcher: EventMatcher):
        """测试别名匹配"""
        result = matcher.match("应用启动")
        assert result.matched == "app_launch"
        assert result.score == 100.0

    def test_fuzzy_match(self, matcher: EventMatcher):
        """测试模糊匹配"""
        result = matcher.match("app启动")
        assert result.matched is not None

    def test_no_match(self, matcher: EventMatcher):
        """测试无匹配"""
        result = matcher.match("xyz123不存在的")
        # 可能返回 None 或低分匹配
        assert result.score < 100.0


# =========================
# DimensionMatcher 测试
# =========================

class TestDimensionMatcher:
    """测试维度匹配器"""

    @pytest.fixture
    def matcher(self):
        return build_dimension_matcher_from_catalog(MOCK_DIMENSIONS)

    def test_exact_match(self, matcher: DimensionMatcher):
        """测试精确匹配"""
        result = matcher.match("country")
        assert result.matched == "country"

    def test_chinese_alias_match(self, matcher: DimensionMatcher):
        """测试中文别名匹配"""
        result = matcher.match("国家")
        assert result.matched == "country"

    def test_match_property(self, matcher: DimensionMatcher):
        """测试匹配 property_name"""
        prop_name, score, explain = matcher.match_property("country")
        assert prop_name == "country"


# =========================
# MetricMatcher 测试
# =========================

class TestMetricMatcher:
    """测试指标匹配器"""

    @pytest.fixture
    def matcher(self):
        return build_metric_matcher_from_catalog(MOCK_METRICS)

    def test_pv_match(self, matcher: MetricMatcher):
        """测试 PV 匹配"""
        result = matcher.match("PV")
        assert result.matched == "pv"

    def test_uv_match(self, matcher: MetricMatcher):
        """测试 UV 匹配"""
        result = matcher.match("UV")
        assert result.matched == "uv"

    def test_chinese_alias_match(self, matcher: MetricMatcher):
        """测试中文别名匹配"""
        result = matcher.match("浏览量")
        assert result.matched == "pv"


# =========================
# TimeMatcher 测试
# =========================

class TestTimeMatcher:
    """测试时间匹配器"""

    @pytest.fixture
    def matcher(self):
        return TimeMatcher()

    def test_last_n_days_chinese(self, matcher: TimeMatcher):
        """测试中文时间表达式"""
        result = matcher.match("近7天")
        assert result.days == 7
        assert result.time_type == "last_n_days"

    def test_last_n_days_english(self, matcher: TimeMatcher):
        """测试英文时间表达式"""
        result = matcher.match("last 30 days")
        assert result.days == 30
        assert result.time_type == "last_n_days"

    def test_yesterday(self, matcher: TimeMatcher):
        """测试昨天"""
        result = matcher.match("昨天")
        assert result.days == 1
        assert result.time_type == "yesterday"

    def test_today(self, matcher: TimeMatcher):
        """测试今天"""
        result = matcher.match("今天")
        assert result.days == 1
        assert result.time_type == "today"

    def test_this_week(self, matcher: TimeMatcher):
        """测试本周"""
        result = matcher.match("本周")
        assert result.days == 7
        assert result.time_type == "this_week"

    def test_default(self, matcher: TimeMatcher):
        """测试默认值"""
        result = matcher.match("无效输入")
        assert result.days == 7
        assert result.time_type == "default"


# =========================
# 运行测试
# =========================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
