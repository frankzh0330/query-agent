from __future__ import annotations

from typing import Any, Dict

from dsl.semantic_models import ALLOWED_REGION_CODES, SemanticDSL


def validate_region(exec_dsl: Dict[str, Any]) -> None:
    actual = exec_dsl.get("content", {}).get("option", {}).get("finder", {}).get("region_filter", [])
    bad = [r for r in actual if r not in ALLOWED_REGION_CODES]
    if bad:
        raise ValueError(f"Invalid region_filter values: {bad}. Allowed: {sorted(ALLOWED_REGION_CODES)}")


def validate_region_consistency(canon: SemanticDSL, exec_dsl: Dict[str, Any]) -> None:
    actual = exec_dsl.get("content", {}).get("option", {}).get("finder", {}).get("region_filter", [])
    if actual != canon.region_filter:
        raise ValueError(f"region_filter mismatch expected={canon.region_filter}, actual={actual}")
