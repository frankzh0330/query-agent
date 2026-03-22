from __future__ import annotations

from service.bearer_service import check_bearer_health, execute_query, get_bearer_client
from service.llm_extractions import Extraction, ExtractionsJson, extract_llm


# dspy_extractions is optional, imported only when USE_DSPY=true
# to avoid circular import issues with openai/litellm
def extract_dspy(*args, **kwargs):
    """Lazy import dspy_extractions to avoid circular import"""
    from service.dspy_extractions import extract_dspy as _extract_dspy
    return _extract_dspy(*args, **kwargs)


__all__ = [
    "extract_llm",
    "ExtractionsJson",
    "Extraction",
    "extract_dspy",
    "get_bearer_client",
    "execute_query",
    "check_bearer_health",
]
