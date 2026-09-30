"""Utilities for merging follow-up patches into QueryState."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from service.session_models import QueryState

_STATE_FIELDS = (
    "tables",
    "metrics",
    "detail_columns",
    "filters",
    "time_range",
    "group_by",
    "order_by",
    "limit",
    "window",
)


def merge_query_state(
    previous: QueryState | None,
    extracted_patch: dict[str, Any],
    project_id: int,
) -> QueryState:
    """将 follow-up patch 合并到上一轮 QueryState。

    规则：
    - patch 中出现的字段视为 explicit
    - previous 中存在且 patch 未覆盖的字段视为 inherited
    - previous 不存在时，返回只包含 explicit 字段的新状态
    """
    patch = deepcopy(extracted_patch or {})
    state = _clone_or_init(previous, project_id)

    explicit_fields: list[str] = []
    inherited_fields: list[str] = []
    field_sources: dict[str, str] = {}

    for field_name in _STATE_FIELDS:
        if field_name in patch:
            value = deepcopy(patch[field_name])
            setattr(state, field_name, value)
            explicit_fields.append(field_name)
            field_sources[field_name] = "explicit"
        else:
            current_value = getattr(state, field_name)
            if _has_value(current_value):
                inherited_fields.append(field_name)
                field_sources[field_name] = "inherited"

    state.project_id = project_id
    state.confidence = patch.get("confidence", state.confidence)
    state.explicit_fields = explicit_fields
    state.inherited_fields = inherited_fields
    state.field_sources = field_sources
    state.turn_type = "followup_patch" if previous is not None else "new_query"
    return state


def _clone_or_init(previous: QueryState | None, project_id: int) -> QueryState:
    if previous is None:
        return QueryState(project_id=project_id)

    data = previous.to_dict()
    data.pop("explicit_fields", None)
    data.pop("inherited_fields", None)
    data.pop("field_sources", None)
    data.pop("turn_type", None)
    return QueryState(**deepcopy(data))


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return bool(value)
    return True
