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


# 2026-09-29 stress test finding F1 (HIGH): get_device_uuid used to shift
# by 2 bits per byte instead of 8 (range(0,2*6,2) instead of
# range(0,8*6,8)), so only ~18 of the 48 MAC bits actually varied the
# result -- confirmed live, two different real MACs collided on the same
# device_uuid. These prove distinct MACs now produce distinct uuids, and
# that the fix still extracts the real byte values (not just "different
# input -> different output" by coincidence).
def test_get_device_uuid_differs_for_different_macs():
    from unittest.mock import patch

    from xparo.database import XP_Database

    with patch('xparo.database.uuid.getnode', return_value=0x001122334455):
        uuid_a = XP_Database.get_device_uuid(None)
    with patch('xparo.database.uuid.getnode', return_value=0x00112233445A):  # last byte differs
        uuid_b = XP_Database.get_device_uuid(None)
    with patch('xparo.database.uuid.getnode', return_value=0x0A1122334455):  # first byte differs
        uuid_c = XP_Database.get_device_uuid(None)

    assert len({uuid_a, uuid_b, uuid_c}) == 3


def test_get_device_uuid_uses_the_full_mac_as_a_colon_hex_string():
    from unittest.mock import patch

    from xparo.database import XP_Database

    with patch('xparo.database.uuid.getnode', return_value=0x001122334455) as _, \
         patch('xparo.database.uuid.uuid5') as mock_uuid5:
        mock_uuid5.return_value = "fake"
        XP_Database.get_device_uuid(None)

    (namespace, mac_string), _kwargs = mock_uuid5.call_args
    assert mac_string == "00:11:22:33:44:55"
