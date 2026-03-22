from __future__ import annotations

import uuid
from typing import Any, Dict

from dsl.semantic_models import SemanticDSL
from dsl.time_utils import last_n_days_spans_utc
from matcher.catalog_loader import Catalog


def render_exec_dsl(canon: SemanticDSL, catalog: Catalog) -> Dict[str, Any]:
    start_ts, end_ts = last_n_days_spans_utc(canon.time_range.n)

    metric_info = catalog.metrics[canon.metric.metric_id]
    event_info = catalog.events[canon.event.event_name]

    dim_id = canon.group_by[0].dimension_id if canon.group_by else "country"
    dim_info = catalog.dimensions[dim_id]

    show_key = str(uuid.uuid4())
    query_filters = [f.model_dump() for f in canon.filters]

    return {
        "use_app_cloud_id": True,
        "periods": [
            {
                "granularity": "day",
                "align_unit": "day",
                "timezone": canon.timezone,
                "week_start": 1,
                "type": "past_range",
                "spans": [[
                    {"offset": 0, "type": "timestamp", "timestamp": str(start_ts)},
                    {"offset": 86399, "type": "timestamp", "timestamp": str(end_ts)},
                ]],
            },
            {
                "granularity": "all",
                "align_unit": "day",
                "timezone": canon.timezone,
                "week_start": 1,
                "skip_period": True,
                "type": "past_range",
                "spans": [[
                    {"offset": 0, "type": "timestamp", "timestamp": str(start_ts)},
                    {"offset": 86399, "type": "timestamp", "timestamp": str(end_ts)},
                ]],
            },
        ],
        "version": 3,
        "content": {
            "profile_groups_v2": [],
            "profile_filters": [
                {"expression": {"logic": "and", "expressions": [{"logic": "or", "conditions": []}]},
                 "show_name": "All Users", "show_label": "1"}
            ],
            "orders": [],
            "query_type": "event",
            "queries": [
                {
                    "indicator_show_name": metric_info["indicator_show_name"],
                    "measure_info": {},
                    "event_indicator": metric_info["event_indicator"],
                    "show_name": event_info["show_name"],
                    "show_label": "A",
                    "event_name": canon.event.event_name,
                    "event_type": event_info.get("event_type", "origin"),
                    "filters": query_filters,
                    "groups_v2": [
                        {
                            "property_compose_type": dim_info["property_compose_type"],
                            "property_name": dim_info["property_name"],
                            "property_type": dim_info["property_type"],
                        }
                    ],
                    "extra": {},
                    "show_key": show_key,
                    "event_id": None,
                    "page": {"offset": 0, "limit": 50},
                    "option": {
                        "chart_type": "line",
                        "use_sample_data": False,
                        "refresh_cache": False,
                        "skip_cache": True,
                        "fusion": False,
                        "insight": {},
                        "analysis_subject": {},
                        "is_pie": False,
                        "skip_period_restrict": False,
                        "ignored_by_au": False,
                        "query_trigger_type": None,
                        "query_trigger": "modify_condition",
                        "cnch_config_map": {},
                        "source": "finder",
                        "max_execute_time": 300,
                    },
                }
            ],
            "option": {
                "blend": {"status": True, "base": 0, "base_period": True},
                "transpose": False,
                "finder": {
                    "sort_rule": "conf_order",
                    "region_filter": canon.region_filter,  # 只复制 canonical
                    "custom_tag": {},
                },
            },
            "result_id": 0,
            "privacy_extra": {},
            "cross_region_data_ready_date": "20260201",
            "max_execute_time": 300,
            "resources": [{"project_ids": [canon.project_id], "subject_ids": [], "app_ids": []}],
            "app_ids": [],
            "finder_option": {},
            "restriction": {},
        },
    }
