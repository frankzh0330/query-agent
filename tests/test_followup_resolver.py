"""followup_resolver.py tests."""

from service.followup_resolver import detect_followup


class TestDetectFollowup:

    def test_returns_new_query_without_previous_state(self):
        decision = detect_followup("换成昨天", None)
        assert decision.is_followup is False
        assert decision.mode == "new_query"
        assert decision.reason == "no_previous_state"

    def test_detects_time_only_followup(self, sample_query_state):
        decision = detect_followup("昨天", sample_query_state)
        assert decision.is_followup is True
        assert decision.mode == "followup_patch"
        assert decision.patch_hints["time_range"]["n"] == 1

    def test_detects_prefix_followup(self, sample_query_state):
        decision = detect_followup("换成客单价", sample_query_state)
        assert decision.is_followup is True
        assert decision.reason == "followup_prefix"

    def test_detects_short_patch_like_table(self, sample_query_state):
        decision = detect_followup("那商品呢", sample_query_state)
        assert decision.is_followup is True
        assert decision.mode == "followup_patch"
        assert "table_token" in decision.matched_signals

    def test_detects_contextual_group_by_patch(self, sample_query_state):
        decision = detect_followup("还是按品类看吧", sample_query_state)
        assert decision.is_followup is True
        assert decision.reason == "followup_prefix"
        assert "group_by_token" in decision.matched_signals
        assert "patch_verb" in decision.matched_signals

    def test_detects_comparison_phrase_followup(self, sample_query_state):
        decision = detect_followup("和昨天比一下", sample_query_state)
        assert decision.is_followup is True
        assert decision.reason == "comparison_phrase"
        assert "comparison_phrase" in decision.matched_signals

    def test_detects_contextual_metric_patch(self, sample_query_state):
        decision = detect_followup("也看看客单价", sample_query_state)
        assert decision.is_followup is True
        assert "metric_token" in decision.matched_signals

    def test_confirmation_reply_wins_when_pending_task(self, sample_query_state):
        decision = detect_followup("1", sample_query_state, pending_task=True)
        assert decision.is_followup is False
        assert decision.mode == "confirmation_reply"

    def test_full_query_is_not_followup(self, sample_query_state):
        decision = detect_followup("查询 orders 表的明细数据", sample_query_state)
        assert decision.is_followup is False
        assert decision.mode == "new_query"


class TestDetectFollowupEnglish:

    def test_time_only_followup(self, sample_query_state):
        decision = detect_followup("What about yesterday?", sample_query_state)
        assert decision.is_followup is True
        assert decision.mode == "followup_patch"

    def test_bare_time_term(self, sample_query_state):
        decision = detect_followup("Yesterday?", sample_query_state)
        assert decision.is_followup is True
        assert decision.reason == "time_only_term"
        assert decision.patch_hints == {}

    def test_instead_phrase(self, sample_query_state):
        assert detect_followup("Show the number of orders instead", sample_query_state).is_followup is True

    def test_only_filter(self, sample_query_state):
        assert detect_followup("Only paid orders", sample_query_state).is_followup is True

    def test_break_down_by(self, sample_query_state):
        assert detect_followup("Also break it down by channel", sample_query_state).is_followup is True

    def test_top_n(self, sample_query_state):
        assert detect_followup("Top 3 per region", sample_query_state).is_followup is True

    def test_short_new_query_is_not_followup(self, sample_query_state):
        """业务词不构成追问线索：短的新查询必须仍判为新查询"""
        decision = detect_followup("Revenue by region", sample_query_state)
        assert decision.is_followup is False
        assert decision.mode == "new_query"

    def test_long_new_query_is_not_followup(self, sample_query_state):
        decision = detect_followup("Average rating by product category for the last 30 days", sample_query_state)
        assert decision.is_followup is False

    def test_no_previous_state(self):
        assert detect_followup("What about yesterday?", None).reason == "no_previous_state"
