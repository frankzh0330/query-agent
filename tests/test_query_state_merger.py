"""query_state_merger.py tests."""

from service.query_state_merger import merge_query_state
from service.session_models import QueryState


class TestMergeQueryState:

    def test_merge_time_patch_keeps_other_fields(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={"time_range": {"type": "last_n_days", "n": 1}},
            project_id=55,
        )
        assert merged.time_range == {"type": "last_n_days", "n": 1}
        assert merged.tables == ["orders"]
        assert merged.metrics == ["revenue"]
        assert "time_range" in merged.explicit_fields
        assert "tables" in merged.inherited_fields
        assert merged.field_sources["metrics"] == "inherited"

    def test_merge_metric_patch_overrides_metric_only(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={"metrics": ["order_count"]},
            project_id=55,
        )
        assert merged.metrics == ["order_count"]
        assert merged.tables == ["orders"]
        assert merged.field_sources["metrics"] == "explicit"

    def test_merge_tables_patch_overrides_tables_only(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={"tables": ["users"]},
            project_id=55,
        )
        assert merged.tables == ["users"]
        assert merged.group_by == ["users.region"]

    def test_merge_multiple_fields(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={
                "metrics": ["order_count"],
                "time_range": {"type": "last_n_days", "n": 30},
                "group_by": ["orders.channel"],
            },
            project_id=55,
        )
        assert merged.metrics == ["order_count"]
        assert merged.time_range["n"] == 30
        assert merged.group_by == ["orders.channel"]
        assert sorted(merged.explicit_fields) == ["group_by", "metrics", "time_range"]

    def test_merge_window_patch(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={"window": {"group_by": "users.region", "limit": 3}},
            project_id=55,
        )
        assert merged.window == {"group_by": "users.region", "limit": 3}
        assert "window" in merged.explicit_fields
        assert merged.metrics == ["revenue"]

    def test_merge_without_previous_creates_new_state(self):
        merged = merge_query_state(
            previous=None,
            extracted_patch={
                "tables": ["orders"],
                "metrics": ["revenue"],
            },
            project_id=60,
        )
        assert merged.project_id == 60
        assert merged.tables == ["orders"]
        assert merged.metrics == ["revenue"]
        assert merged.inherited_fields == []
        assert merged.turn_type == "new_query"

    def test_merge_does_not_mutate_previous_state(self, sample_query_state):
        original = sample_query_state.to_dict()
        merge_query_state(
            previous=sample_query_state,
            extracted_patch={"metrics": ["order_count"]},
            project_id=55,
        )
        assert sample_query_state.to_dict() == original
