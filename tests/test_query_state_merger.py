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
        assert merged.event == "app_launch"
        assert merged.metric == "pv"
        assert "time_range" in merged.explicit_fields
        assert "event" in merged.inherited_fields
        assert merged.field_sources["metric"] == "inherited"

    def test_merge_metric_patch_overrides_metric_only(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={"metric": "uv"},
            project_id=55,
        )
        assert merged.metric == "uv"
        assert merged.event == "app_launch"
        assert merged.field_sources["metric"] == "explicit"

    def test_merge_region_patch_overrides_region_only(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={"region_filter": ["USTTP"]},
            project_id=55,
        )
        assert merged.region_filter == ["USTTP"]
        assert merged.group_by == ["country"]

    def test_merge_multiple_fields(self, sample_query_state):
        merged = merge_query_state(
            previous=sample_query_state,
            extracted_patch={
                "metric": "uv",
                "time_range": {"type": "last_n_days", "n": 30},
                "group_by": ["channel"],
            },
            project_id=55,
        )
        assert merged.metric == "uv"
        assert merged.time_range["n"] == 30
        assert merged.group_by == ["channel"]
        assert sorted(merged.explicit_fields) == ["group_by", "metric", "time_range"]

    def test_merge_without_previous_creates_new_state(self):
        merged = merge_query_state(
            previous=None,
            extracted_patch={
                "event": "payment_success",
                "metric": "uv",
            },
            project_id=60,
        )
        assert merged.project_id == 60
        assert merged.event == "payment_success"
        assert merged.metric == "uv"
        assert merged.inherited_fields == []
        assert merged.turn_type == "new_query"

    def test_merge_does_not_mutate_previous_state(self, sample_query_state):
        original = sample_query_state.to_dict()
        merge_query_state(
            previous=sample_query_state,
            extracted_patch={"metric": "uv"},
            project_id=55,
        )
        assert sample_query_state.to_dict() == original
