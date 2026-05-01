from memory.user_preference_store import UserPreferenceStore


class TestUserPreferenceStore:
    def test_record_selection_is_scoped_by_project_and_user(self, tmp_path):
        store = UserPreferenceStore(data_path=str(tmp_path / "prefs"))
        store.record_selection(55, "user_a", event="payment_success", metric="uv", group_by=["channel"])
        store.record_selection(55, "user_a", event="payment_success")
        store.record_selection(60, "user_a", event="app_launch")

        bucket_55 = store.get_bucket(55, "user_a", "event")
        bucket_60 = store.get_bucket(60, "user_a", "event")

        assert bucket_55["payment_success"] == 2
        assert "app_launch" not in bucket_55
        assert bucket_60["app_launch"] == 1

    def test_rerank_candidates_only_applies_post_recall_bias(self, tmp_path):
        store = UserPreferenceStore(data_path=str(tmp_path / "prefs"))
        for _ in range(6):
            store.record_selection(55, "user_a", event="payment_submit")

        candidates = [
            {"value": "purchase_success", "score": 55.0},
            {"value": "payment_submit", "score": 54.0},
        ]

        reranked, explain = store.rerank_candidates(55, "user_a", "event", candidates)

        assert explain["applied"] is True
        assert reranked[0]["value"] == "payment_submit"
        assert reranked[0]["score"] > reranked[1]["score"]
        assert reranked[0]["preference_count"] == 6
