"""followup_resolver.py tests."""

from service.followup_resolver import detect_followup
from service.session_models import QueryState


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
        decision = detect_followup("换成UV", sample_query_state)
        assert decision.is_followup is True
        assert decision.reason == "followup_prefix"

    def test_detects_short_patch_like_region(self, sample_query_state):
        decision = detect_followup("那美国呢", sample_query_state)
        assert decision.is_followup is True
        assert decision.mode == "followup_patch"
        assert "region_token" in decision.matched_signals

    def test_detects_contextual_group_by_patch(self, sample_query_state):
        decision = detect_followup("还是按国家看吧", sample_query_state)
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
        decision = detect_followup("也看看UV", sample_query_state)
        assert decision.is_followup is True
        assert "metric_token" in decision.matched_signals

    def test_confirmation_reply_wins_when_pending_task(self, sample_query_state):
        decision = detect_followup("1", sample_query_state, pending_task=True)
        assert decision.is_followup is False
        assert decision.mode == "confirmation_reply"

    def test_full_query_is_not_followup(self, sample_query_state):
        decision = detect_followup("查询 payment_success 的 UV", sample_query_state)
        assert decision.is_followup is False
        assert decision.mode == "new_query"
