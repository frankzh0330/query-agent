"""Bus 消息总线测试"""
import os
from unittest import mock

import pytest


class TestBusMessage:
    """bus/message_schema.py"""

    def test_bus_message_defaults(self):
        from bus.message_schema import BusMessage
        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        assert msg.msg_id  # auto-generated
        assert msg.project_id == 55
        assert msg.created_at > 0

    def test_bus_message_serialization_roundtrip(self):
        from bus.message_schema import BusMessage
        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        json_str = msg.model_dump_json()
        restored = BusMessage.model_validate_json(json_str)
        assert restored.msg_id == msg.msg_id
        assert restored.channel == msg.channel
        assert restored.text == msg.text


class TestBusResult:
    """bus/message_schema.py"""

    def test_bus_result_success(self):
        from bus.message_schema import BusMessage, BusResult
        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        result = BusResult(msg=msg, success=True)
        assert result.nl2dsl_result is None
        assert result.query_result is None

    def test_bus_result_failure(self):
        from bus.message_schema import BusMessage, BusResult
        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        result = BusResult(msg=msg, success=False, error="boom")
        assert result.error == "boom"


class TestDirectCallBus:
    """bus/direct_call_bus.py"""

    @pytest.mark.asyncio
    async def test_enqueue_without_worker_raises(self):
        from bus.direct_call_bus import DirectCallBus
        from bus.message_schema import BusMessage
        bus = DirectCallBus()
        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        with pytest.raises(RuntimeError, match="Worker not bound"):
            await bus.enqueue_request(msg)

    @pytest.mark.asyncio
    async def test_enqueue_calls_worker(self):
        from bus.direct_call_bus import DirectCallBus
        from bus.message_schema import BusMessage

        bus = DirectCallBus()
        mock_worker = mock.AsyncMock()
        mock_worker.process.return_value = {"nl2dsl": {"status": "ok"}, "query": {}}
        bus.bind_worker(mock_worker)

        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        await bus.enqueue_request(msg)
        mock_worker.process.assert_called_once_with(msg)

    @pytest.mark.asyncio
    async def test_enqueue_dispatches_result_on_success(self):
        from bus.direct_call_bus import DirectCallBus
        from bus.message_schema import BusMessage

        bus = DirectCallBus()
        mock_worker = mock.AsyncMock()
        mock_worker.process.return_value = {"nl2dsl": {"status": "ok"}, "query": {}}
        mock_dispatcher = mock.AsyncMock()
        bus.bind_worker(mock_worker)
        bus.bind_dispatcher(mock_dispatcher)

        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        await bus.enqueue_request(msg)
        mock_dispatcher.dispatch.assert_called_once()
        bus_result = mock_dispatcher.dispatch.call_args[0][0]
        assert bus_result.success is True

    @pytest.mark.asyncio
    async def test_enqueue_dispatches_error_on_failure(self):
        from bus.direct_call_bus import DirectCallBus
        from bus.message_schema import BusMessage

        bus = DirectCallBus()
        mock_worker = mock.AsyncMock()
        mock_worker.process.side_effect = ValueError("test error")
        mock_dispatcher = mock.AsyncMock()
        bus.bind_worker(mock_worker)
        bus.bind_dispatcher(mock_dispatcher)

        msg = BusMessage(channel="telegram", chat_id="123", text="hello")
        await bus.enqueue_request(msg)
        bus_result = mock_dispatcher.dispatch.call_args[0][0]
        assert bus_result.success is False
        assert "test error" in bus_result.error

    @pytest.mark.asyncio
    async def test_dequeue_raises_not_implemented(self):
        from bus.direct_call_bus import DirectCallBus
        bus = DirectCallBus()
        with pytest.raises(NotImplementedError):
            await bus.dequeue_request()

    @pytest.mark.asyncio
    async def test_dequeue_result_raises_not_implemented(self):
        from bus.direct_call_bus import DirectCallBus
        bus = DirectCallBus()
        with pytest.raises(NotImplementedError):
            await bus.dequeue_result()


class TestCreateBus:
    """bus/__init__.py"""

    def test_create_bus_direct_default(self):
        from bus import create_bus
        from bus.direct_call_bus import DirectCallBus
        bus = create_bus()
        assert isinstance(bus, DirectCallBus)

    def test_create_bus_redis(self):
        from bus import create_bus
        mock_instance = mock.MagicMock()
        with mock.patch.dict(os.environ, {"MESSAGE_BUS_BACKEND": "redis"}):
            with mock.patch("bus.redis_bus.RedisMessageBus", return_value=mock_instance):
                bus = create_bus()
                assert bus is mock_instance
