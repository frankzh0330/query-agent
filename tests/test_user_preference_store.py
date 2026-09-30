from memory.user_preference_store import UserPreferenceStore


class TestUserPreferenceStore:
    def test_record_selection_is_scoped_by_project_and_user(self, tmp_path):
        store = UserPreferenceStore(data_path=str(tmp_path / "prefs"))
        store.record_selection(55, "user_a", table="orders", metric="revenue", columns=["users.region"])
        store.record_selection(55, "user_a", table="orders")
        store.record_selection(60, "user_a", table="users")

        bucket_55 = store.get_bucket(55, "user_a", "table")
        bucket_60 = store.get_bucket(60, "user_a", "table")

        assert bucket_55["orders"] == 2
        assert "users" not in bucket_55
        assert bucket_60["users"] == 1

    def test_rerank_candidates_only_applies_post_recall_bias(self, tmp_path):
        store = UserPreferenceStore(data_path=str(tmp_path / "prefs"))
        for _ in range(6):
            store.record_selection(55, "user_a", table="orders")

        candidates = [
            {"value": "users", "score": 55.0},
            {"value": "orders", "score": 54.0},
        ]

        reranked, explain = store.rerank_candidates(55, "user_a", "table", candidates)

        assert explain["applied"] is True
        assert reranked[0]["value"] == "orders"
        assert reranked[0]["score"] > reranked[1]["score"]
        assert reranked[0]["preference_count"] == 6
