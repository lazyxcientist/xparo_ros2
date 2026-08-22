"""Regression test for a real bug: when record_bags=True, XP_Database's own
SIGINT/SIGTERM handler used to call exit(0), which raises SystemExit -- a
sibling of KeyboardInterrupt, not the same exception -- so xparo_ros.py's
`except KeyboardInterrupt: pass` around rclpy.spin() never caught it. The
process exited straight from the signal handler, before main() ever reached
stop_logging_session(), the only thing that sends a session's
"session_end"/session_end_time update to Django. That made session_end_time
missing from Logs_history for the vast majority of real stops (any graceful
Ctrl+C/`ros2 launch` shutdown or SIGTERM while bag recording was on), matching
the reported symptom of "most" (not all) log rows lacking it -- a
record_bags=False process, with no handler override at all, still reached
stop_logging_session() via the ordinary KeyboardInterrupt.
"""
import signal
from unittest.mock import MagicMock, patch

import pytest


def _installed_handler(mock_signal, sig):
    for call in mock_signal.call_args_list:
        if call.args[0] == sig:
            return call.args[1]
    raise AssertionError(f"no handler registered for {sig}")


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_handle_exit_raises_keyboard_interrupt_not_system_exit(sig):
    with patch('xparo.database.BlackboxOrchestrator') as mock_orchestrator_cls, \
         patch('xparo.database.signal.signal') as mock_signal, \
         patch('xparo.database.Thread'):
        from xparo.database import XP_Database
        db = XP_Database(
            xparo_database_size=100,
            xparo_database_path='/tmp/xparo-test-db',
            xparo_website_url='http://example.invalid',
            BAG_DIR='/tmp/bags',
            record_bags=True,
        )
        handler = _installed_handler(mock_signal, sig)
        mock_orchestrator = mock_orchestrator_cls.return_value

        with pytest.raises(KeyboardInterrupt):
            handler(sig, None)

        assert mock_orchestrator.running is False
        mock_orchestrator.stop_recording.assert_called_once()
