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


@pytest.fixture(autouse=True)
def _no_background_memory_llm(monkeypatch):
    """orchestrator 每次成功查询都会后台触发 memory judge（真实 LLM 调用）。

    测试/eval 必须离线、确定，且不能往 data/memory 写运行产物 → 对全局 orchestrator 的
    MemoryWriter 实例打桩（只 patch 实例，不影响 test_memory_writer 直接构造的对象）。
    """
    import sys

    app_module = sys.modules.get("app")
    if app_module is None:
        try:
            import app as app_module  # noqa: F811
        except Exception:
            yield
            return

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(app_module.orchestrator.memory, "maybe_save", _noop)
    yield
