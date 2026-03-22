from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict

import yaml


@dataclass(frozen=True)
class Catalog:
    metrics: Dict
    events: Dict
    dimensions: Dict
    metric_alias_lookup: Dict[str, str]
    event_alias_lookup: Dict[str, str]
    dimension_alias_lookup: Dict[str, str]


def _load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def load_catalog(base_dir: str) -> Catalog:
    metrics = _load_yaml(os.path.join(base_dir, "metrics.yaml")).get("metrics", {})
    events = _load_yaml(os.path.join(base_dir, "events.yaml")).get("events", {})
    dimensions = _load_yaml(os.path.join(base_dir, "dimensions.yaml")).get("dimensions", {})

    # 构建别名查找表
    metric_alias_lookup: Dict[str, str] = {}
    for metric_id, info in metrics.items():
        for alias in info.get("aliases", []):
            nk = _norm(alias)
            if nk:
                metric_alias_lookup[nk] = metric_id

    print("metric_alias_lookup:{}".format(metric_alias_lookup))

    # 构建事件别名查找表
    event_alias_lookup: Dict[str, str] = {}
    for event_id, info in events.items():
        for alias in info.get("aliases", []):
            nk = _norm(alias)
            if nk:
                event_alias_lookup[nk] = event_id

    print("event_alias_lookup:{}".format(event_alias_lookup))

    # 构建维度别名查找表
    dimension_alias_lookup: Dict[str, str] = {}
    for dim_id, info in dimensions.items():
        for alias in info.get("aliases", []):
            nk = _norm(alias)
            if nk:
                dimension_alias_lookup[nk] = dim_id

    print("dimension_alias_lookup:{}".format(dimension_alias_lookup))

    return Catalog(
        metrics=metrics,
        events=events,
        dimensions=dimensions,
        metric_alias_lookup=metric_alias_lookup,
        event_alias_lookup=event_alias_lookup,
        dimension_alias_lookup=dimension_alias_lookup,
    )
