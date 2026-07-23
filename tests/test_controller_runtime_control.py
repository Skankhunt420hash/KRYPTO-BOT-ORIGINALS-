import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import settings
from src.engine.execution_engine import ExecutionEngine
from src.engine.runtime_control import RuntimeControlState
from src.strategies.signal import EnhancedSignal, Side


class _Connector:
    def __init__(self, before_first_order=None):
        self.before_first_order = before_first_order
        self.order_calls = 0

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def create_market_buy_order(self, symbol, amount):
        self.order_calls += 1
        if self.before_first_order and self.order_calls == 1:
            self.before_first_order()
        return {"id": "order-1", "status": "closed", "average": 100.0}

    def create_market_sell_order(self, symbol, amount):
        return self.create_market_buy_order(symbol, amount)


def _signal():
    return EnhancedSignal(
        strategy_name="test",
        symbol="BTC/USDT",
        timeframe="1h",
        side=Side.LONG,
        confidence=80.0,
        entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        rr=2.0,
        reason="controller-runtime-control-test",
    )


class ControllerRuntimeControlTests(unittest.TestCase):
    def test_control_state_is_shared_between_process_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_control.json"
            controller_state = RuntimeControlState(path)
            bot_state = RuntimeControlState(path)

            self.assertFalse(bot_state.get_snapshot()["paused"])
            self.assertTrue(controller_state.pause_entries())
            self.assertTrue(bot_state.get_snapshot()["paused"])

            self.assertTrue(controller_state.enable_risk_off())
            self.assertTrue(bot_state.get_snapshot()["risk_off"])

            self.assertTrue(controller_state.resume_entries())
            self.assertTrue(controller_state.disable_risk_off())
            snapshot = bot_state.get_snapshot()
            self.assertFalse(snapshot["paused"])
            self.assertFalse(snapshot["risk_off"])

    def test_invalid_persisted_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_control.json"
            path.write_text('{"paused": false', encoding="utf-8")

            snapshot = RuntimeControlState(path).get_snapshot()

            self.assertTrue(snapshot["paused"])
            self.assertTrue(snapshot["risk_off"])

    def test_entry_is_blocked_before_connector_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_control.json"
            controller_state = RuntimeControlState(path)
            bot_state = RuntimeControlState(path)
            connector = _Connector()
            engine = ExecutionEngine(connector)
            controller_state.pause_entries()

            with patch("src.engine.execution_engine.runtime_control", bot_state):
                result = engine.execute_entry(
                    "BTC/USDT", "buy", 1.0, signal=_signal()
                )

            self.assertFalse(result.success)
            self.assertIn("PAUSED", result.reason)
            self.assertEqual(connector.order_calls, 0)

    def test_entry_retry_observes_new_controller_risk_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_control.json"
            controller_state = RuntimeControlState(path)
            bot_state = RuntimeControlState(path)

            def activate_risk_off_and_fail():
                controller_state.enable_risk_off()
                raise ConnectionError("temporary exchange failure")

            connector = _Connector(before_first_order=activate_risk_off_and_fail)
            engine = ExecutionEngine(connector)

            with (
                patch("src.engine.execution_engine.runtime_control", bot_state),
                patch.object(settings, "EXECUTION_MAX_RETRIES", 1),
                patch.object(settings, "EXECUTION_RETRY_BACKOFF_SEC", 0.0),
            ):
                result = engine.execute_entry(
                    "BTC/USDT", "buy", 1.0, signal=_signal()
                )

            self.assertFalse(result.success)
            self.assertIn("RISK OFF", result.reason)
            self.assertEqual(connector.order_calls, 1)

    def test_exit_remains_allowed_while_entries_are_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_control.json"
            bot_state = RuntimeControlState(path)
            bot_state.pause_entries()
            connector = _Connector()
            engine = ExecutionEngine(connector)

            with patch("src.engine.execution_engine.runtime_control", bot_state):
                result = engine.execute_exit("BTC/USDT", "sell", 1.0)

            self.assertTrue(result.success)
            self.assertEqual(connector.order_calls, 1)


if __name__ == "__main__":
    unittest.main()
