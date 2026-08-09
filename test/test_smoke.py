"""Proves the ros_packages test harness works before later phases rely on
it for real: pure-Python xparo modules import cleanly via pytest's
`pythonpath` ini setting (no colcon build needed), and rclpy is still
importable alongside them.
"""


def test_engine_and_database_import_cleanly():
    from xparo.database import XP_Database  # noqa: F401
    from xparo.engine import Engine  # noqa: F401
    from xparo.blackbox_manager import BlackboxOrchestrator  # noqa: F401


def test_rclpy_is_still_importable_alongside_xparo():
    import rclpy  # noqa: F401


def test_get_device_uuid_is_deterministic():
    from xparo.database import XP_Database

    first = XP_Database.get_device_uuid(None)
    second = XP_Database.get_device_uuid(None)
    assert first == second
    assert len(first) == 36  # canonical UUID string length
