"""共享 fixtures"""
import pytest

from memory.storage.memory_file import SessionStorage, TaskStorage
from memory.long_term_memory import LongTermMemory
from service.session_manager import SessionManager
from service.task_manager import TaskManager
from service.session_models import QueryState
from service.llm_extractions import Extraction, SQLIntentJson


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
        tables=["orders"],
        metrics=["revenue"],
        time_range={"type": "last_n_days", "n": 7},
        group_by=["users.region"],
        filters=[],
    )


@pytest.fixture
def sample_intent_json():
    return SQLIntentJson(
        metric_extractions=[Extraction(text="销售额")],
        time_extractions=[Extraction(text="近7天")],
        table_extractions=[Extraction(text="订单表")],
        group_by_extractions=[Extraction(text="地区")],
    )


@pytest.fixture
def sample_intent_no_signal():
    return SQLIntentJson()
