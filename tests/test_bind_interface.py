import os
import unittest
from unittest import mock

from network import BIND_ENV, pick_physical
from xray_test import make_config


class BindInterfaceTests(unittest.TestCase):
    def test_picks_the_physical_adapter_not_a_vpn(self):
        rows = [('Nebula', 'Wintun Userspace Tunnel', False, 5),
                ('v2rayN-tun', 'v2rayN TUN', True, 1),
                ('Wi-Fi', 'Intel(R) Wi-Fi 6E AX211', True, 35),
                ('Ethernet', 'Realtek PCIe GbE', True, 25)]
        self.assertEqual(pick_physical(rows), 'Ethernet')

    def test_no_physical_adapter_means_default_route(self):
        self.assertIsNone(pick_physical([('Nebula', 'Wintun', False, 5)]))

    def test_candidate_dials_through_the_bound_adapter(self):
        outbound = {'protocol': 'vless', 'settings': {}, 'streamSettings': {'network': 'ws'}}
        with mock.patch.dict(os.environ, {BIND_ENV: 'Wi-Fi'}):
            cfg = make_config(outbound, 1080)
        self.assertEqual(cfg['outbounds'][0]['streamSettings']['sockopt']['interface'], 'Wi-Fi')
        self.assertNotIn('sockopt', outbound['streamSettings'])

    def test_unbound_when_no_adapter_was_chosen(self):
        outbound = {'protocol': 'vless', 'settings': {}}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(BIND_ENV, None)
            cfg = make_config(outbound, 1080)
        self.assertNotIn('streamSettings', cfg['outbounds'][0])

    def test_a_node_on_this_pc_is_never_bound_to_the_adapter(self):
        # The self-test's fixture server is on 127.0.0.1; forced out through
        # Wi-Fi it reset every connection and stopped publishing.
        for address in ('127.0.0.1', 'localhost', '::1'):
            outbound = {'protocol': 'vless', 'settings': {'vnext': [{'address': address, 'port': 1}]}}
            with mock.patch.dict(os.environ, {BIND_ENV: 'Wi-Fi'}):
                cfg = make_config(outbound, 1080)
            self.assertNotIn('streamSettings', cfg['outbounds'][0], address)

    def test_a_remote_node_still_is(self):
        outbound = {'protocol': 'vless', 'settings': {'vnext': [{'address': '203.0.113.7', 'port': 1}]}}
        with mock.patch.dict(os.environ, {BIND_ENV: 'Wi-Fi'}):
            cfg = make_config(outbound, 1080)
        self.assertEqual(cfg['outbounds'][0]['streamSettings']['sockopt']['interface'], 'Wi-Fi')


if __name__ == '__main__':
    unittest.main()
