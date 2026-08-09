"""get_best_ip() returns (ip, source) -- Django (apps.analytics.models
.Robots.IpSource) trusts these exact string literals to tell a Tailscale IP
from a public IP that's probably just a NAT gateway. Each fallback step is
exercised in isolation via mocking, same technique as test_smoke.py's
unbound XP_Database.get_device_uuid(None) call -- get_best_ip doesn't touch
self either, so no XP_Database instance needs constructing.
"""
from unittest.mock import MagicMock, patch

import pytest


def _get_best_ip():
    from xparo.database import XP_Database
    return XP_Database.get_best_ip(None)


class _Snic:
    def __init__(self, family, address):
        self.family = family
        self.address = address


def test_tailscale_ip_wins_when_present():
    import socket as real_socket
    with patch('xparo.database.psutil.net_if_addrs') as mock_addrs:
        mock_addrs.return_value = {
            'tailscale0': [_Snic(real_socket.AF_INET, '100.64.1.2')],
            'eth0': [_Snic(real_socket.AF_INET, '192.168.1.5')],
        }
        ip, source = _get_best_ip()
    assert (ip, source) == ('100.64.1.2', 'tailscale')


def test_falls_back_to_public_ip_when_no_tailscale_interface():
    with patch('xparo.database.psutil.net_if_addrs', return_value={}):
        mock_response = MagicMock()
        mock_response.read.return_value = b'203.0.113.9'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        with patch('xparo.database.urllib.request.urlopen', return_value=mock_cm):
            ip, source = _get_best_ip()
    assert (ip, source) == ('203.0.113.9', 'public_unreachable')


def test_falls_back_to_lan_ip_when_offline():
    with patch('xparo.database.psutil.net_if_addrs', return_value={}), \
         patch('xparo.database.urllib.request.urlopen', side_effect=OSError('no internet')):
        mock_socket = MagicMock()
        mock_socket.getsockname.return_value = ('192.168.1.50', 54321)
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_socket
        with patch('xparo.database.socket.socket', return_value=mock_cm):
            ip, source = _get_best_ip()
    assert (ip, source) == ('192.168.1.50', 'lan')


def test_falls_back_to_localhost_when_fully_isolated():
    with patch('xparo.database.psutil.net_if_addrs', return_value={}), \
         patch('xparo.database.urllib.request.urlopen', side_effect=OSError('no internet')), \
         patch('xparo.database.socket.socket', side_effect=OSError('no network')):
        ip, source = _get_best_ip()
    assert (ip, source) == ('127.0.0.1', 'localhost')


def test_interface_scan_exception_falls_through_to_next_step():
    with patch('xparo.database.psutil.net_if_addrs', side_effect=RuntimeError('boom')):
        mock_response = MagicMock()
        mock_response.read.return_value = b'203.0.113.9'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        with patch('xparo.database.urllib.request.urlopen', return_value=mock_cm):
            ip, source = _get_best_ip()
    assert (ip, source) == ('203.0.113.9', 'public_unreachable')
