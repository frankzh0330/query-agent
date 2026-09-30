"""SQL 生成器纯函数测试：window/order 文本解析与降级"""
from __future__ import annotations

from service.sql_generator import parse_order_text, parse_window_text, time_range_to_ch_expr


class TestParseWindowText:
    def test_per_group_ranking(self):
        w = parse_window_text("每个地区前3")
        assert w == {"group_text": "地区", "limit": 3, "raw": "每个地区前3"}

    def test_ge_variant(self):
        w = parse_window_text("各品类前10")
        assert w["group_text"] == "品类"
        assert w["limit"] == 10

    def test_direction_word_demotes_to_none(self):
        """'各品类销售额最高的前5' 是全局 TopN，不是分组排名 → None 交由 order 路径"""
        assert parse_window_text("各品类销售额最高的前5") is None

    def test_plain_text(self):
        assert parse_window_text("随便一句话") is None


class TestParseOrderText:
    def test_direction_desc(self):
        o = parse_order_text("销售额最高的前5")
        assert o["metric_text"] == "销售额"
        assert o["limit"] == 5
        assert o["direction"] == "DESC"

    def test_direction_asc(self):
        o = parse_order_text("客单价最低的3个")
        assert o["metric_text"] == "客单价"
        assert o["direction"] == "ASC"

    def test_bare_topn_fallback(self):
        o = parse_order_text("销售额前5")
        assert o["metric_text"] == "销售额"
        assert o["limit"] == 5
        assert o["direction"] == "DESC"

    def test_prefix_quantifier_stripped(self):
        """'各品类销售额最高的前5' → 指标文本剥掉前缀量词'各'"""
        o = parse_order_text("各品类销售额最高的前5")
        assert o["metric_text"] == "品类销售额"
        assert o["limit"] == 5


class TestTimeRangeToChExpr:
    def test_last_n_days(self):
        assert time_range_to_ch_expr({"type": "last_n_days", "n": 7}, "orders.created_at") == \
            "orders.created_at >= now() - INTERVAL 7 DAY"

    def test_yesterday_half_open(self):
        assert time_range_to_ch_expr({"type": "yesterday", "n": 1}, "t") == \
            "t >= today() - 1 AND t < today()"

    def test_this_month(self):
        assert "toStartOfMonth" in time_range_to_ch_expr({"type": "this_month", "n": 30}, "t")

    def test_no_time_column_returns_empty(self):
        assert time_range_to_ch_expr({"type": "last_n_days", "n": 7}, "") == ""
