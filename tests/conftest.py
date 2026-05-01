"""共享 fixtures"""
import pytest

from memory.storage.memory_file import SessionStorage, TaskStorage
from memory.long_term_memory import LongTermMemory
from service.session_manager import SessionManager
from service.task_manager import TaskManager
from service.session_models import QueryState
from service.llm_extractions import Extraction, ExtractionsJson


@pytest.fixture
def session_storage(tmp_path):
    return SessionStorage(data_path=str(tmp_path / "sessions"))


@pytest.fixture
def task_storage(tmp_path):
    return TaskStorage(data_path=str(tmp_path / "tasks"))


@pytest.fixture
def long_term_memory(tmp_path):
    return LongTermMemory(data_path=str(tmp_path / "memory"))


@pytest.fixture
def session_manager(tmp_path):
    return SessionManager(data_path=str(tmp_path / "data"))


@pytest.fixture
def task_manager(task_storage):
    return TaskManager(ttl_minutes=30, storage=task_storage)


@pytest.fixture
def sample_query_state():
    return QueryState(
        project_id=55,
        event="app_launch",
        metric="pv",
        time_range={"type": "last_n_days", "n": 7},
        region_filter=["ROW"],
        group_by=["country"],
        filters=[],
    )


@pytest.fixture
def sample_extractions_json():
    return ExtractionsJson(
        metric_extractions=[Extraction(text="PV")],
        time_extractions=[Extraction(text="近7天")],
        event_extractions=[Extraction(text="app_launch")],
        region_filter=["ROW"],
        group_by_extractions=[Extraction(text="country")],
    )


@pytest.fixture
def sample_extractions_no_event():
    return ExtractionsJson(
        metric_extractions=[Extraction(text="PV")],
        event_extractions=[],
        region_filter=["ROW"],
    )
