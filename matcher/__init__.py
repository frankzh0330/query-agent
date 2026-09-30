from __future__ import annotations

from matcher.base import (
    DEFAULT_STOPWORDS,
    DEFAULT_SYNONYM_MAP,
    BaseMatcher,
    MatchResult,
)
from matcher.column_matcher import ColumnMatcher, build_column_matcher_from_schema
from matcher.matcher_service import MatcherService, ResolvedResult
from matcher.schema_loader import SQLSchema, load_sql_schema
from matcher.sql_metric_matcher import SQLMetricMatcher, build_sql_metric_matcher_from_schema
from matcher.table_matcher import TableMatcher, build_table_matcher_from_schema
from matcher.time_matcher import TimeMatcher, resolve_last_n_days, time_range_from_explain

__all__ = [
    "BaseMatcher",
    "MatchResult",
    "DEFAULT_STOPWORDS",
    "DEFAULT_SYNONYM_MAP",
    "TableMatcher",
    "build_table_matcher_from_schema",
    "ColumnMatcher",
    "build_column_matcher_from_schema",
    "SQLMetricMatcher",
    "build_sql_metric_matcher_from_schema",
    "TimeMatcher",
    "resolve_last_n_days",
    "time_range_from_explain",
    "MatcherService",
    "ResolvedResult",
    "SQLSchema",
    "load_sql_schema",
]
