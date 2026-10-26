"""Fully offline rename and country-flag tests; do not contact public proxy nodes."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from collector import SOURCES, parse
from naming import name_for_position, rename_share_link
from geolocation import CountryInfo, country_flag, public_ip, geolocate_hosts, resolve_endpoint


class RenameTests(unittest.TestCase):
    def test_vless_preserves_connection(self):
        src = 'vless://12345@example.net:443?type=ws&security=tls&path=%2Fhi#Old%20Name'
        changed = rename_share_link(src, name_for_position('🇩🇪', 1))
        self.assertEqual(unquote(urlsplit(changed).fragment), 'server 🇩🇪 1')
        self.assertEqual(changed.split('#', 1)[0], src.split('#', 1)[0])
        self.assertEqual(parse(changed).key, parse(src).key)

    def test_trojan_preserves_connection(self):
        src = 'trojan://secret@example.net:443?security=tls&type=ws&host=www.example.net#Old%20Name'
        changed = rename_share_link(src, 'server 🇺🇸 2')
        self.assertEqual(unquote(urlsplit(changed).fragment), 'server 🇺🇸 2')
        self.assertEqual(parse(changed).key, parse(src).key)

    def test_trojan_escapes_reserved_userinfo_for_dotnet_clients(self):
        src='trojan://fixture@tag+pass@example.net:443?security=tls&sni=example.net#old'
        changed=rename_share_link(src,'server 🌐 2')
        self.assertIn('fixture%40tag%2Bpass@',changed)
        self.assertEqual(parse(changed).key,parse(src).key)
        self.assertEqual(rename_share_link(changed,'server 🌐 2'),changed)

    def test_ss_preserves_credentials(self):
        payload = base64.urlsafe_b64encode(b'aes-256-gcm:longpassword').decode().rstrip('=')
        src = f'ss://{payload}@8.8.4.4:443#Unwanted'
        changed = rename_share_link(src, 'server 🇳🇱 3')
        self.assertEqual(unquote(urlsplit(changed).fragment), 'server 🇳🇱 3')
        self.assertEqual(parse(changed).key, parse(src).key)

    def test_vmess_renames_ps_in_json(self):
        config = {'v': '2', 'ps': 'Original Label', 'add': 'example.com', 'port': '443',
                  'id': '14a2c062-1234-5678-abcd-0123456789ab', 'aid': 0,
                  'tls': 'tls', 'net': 'ws', 'path': '/x'}
        src = 'vmess://' + base64.b64encode(json.dumps(config).encode()).decode()
        changed = rename_share_link(src, 'server 🇸🇪 4')
        self.assertNotIn('#', changed)
        body = base64.b64decode(changed.split('://', 1)[1])
        decoded = json.loads(body)
        self.assertEqual(decoded['ps'], 'server 🇸🇪 4')
        self.assertEqual(decoded['id'], config['id'])
        self.assertEqual(parse(changed).key, parse(src).key)

    def test_all_sources_configured(self):
        # EbraSha's repository was deleted (404, 2026-10-26); three live feeds replaced it.
        self.assertEqual(len(SOURCES), 23)
        for name in ['RadikalAll', 'Bahemmat', 'Hamedcode443', 'MorpheusIran']:
            self.assertTrue(SOURCES[name].startswith('https://raw.githubusercontent.com/'), name)
        # Epodonios' extra feeds, missing from its combined file.
        for name in [f'EpodoniosSub{n}' for n in range(21, 27)] + ['EpodoniosVless']:
            self.assertIn('Epodonios/v2ray-configs/main/', SOURCES[name], name)
        self.assertNotIn('EbraSha', SOURCES)
        for name in ('ALIILAPRO', 'Ermaozi', 'MatinGhanbari'):
            self.assertTrue(SOURCES[name].startswith('https://raw.githubusercontent.com/'), name)
        self.assertIn('Shadowmere', SOURCES)
        self.assertIn('Pawdroid', SOURCES)
        self.assertIn('Radikal', SOURCES)
        self.assertIn('MahdiblandPublic', SOURCES)
        self.assertEqual(SOURCES['Patterniha'], 'https://raw.githubusercontent.com/patterniha/Free-Configs/main/configs.txt')
        self.assertEqual(SOURCES['Whitedns'], 'https://raw.githubusercontent.com/iampedii/whitedns-sub/refs/heads/main/base64.txt')
        self.assertEqual(SOURCES['Kahrobas'], 'https://bot.kahrobas.shop/sub.txt')


class GeolocationTests(unittest.TestCase):
    def test_country_flags(self):
        self.assertEqual(country_flag('DE'), '🇩🇪')
        self.assertEqual(country_flag('nl'), '🇳🇱')
        self.assertEqual(country_flag(None), '🌐')
        self.assertEqual(name_for_position(country_flag('US'), 12), 'server 🇺🇸 12')

    def test_private_ip_rejected(self):
        self.assertIsNone(public_ip('127.0.0.1'))
        self.assertIsNone(public_ip('10.0.0.1'))
        self.assertIsNone(public_ip('192.168.1.1'))
        self.assertEqual(public_ip('8.8.8.8'), '8.8.8.8')

    def test_local_dns_is_used_for_hostname(self):
        fake = [(2, 1, 6, '', ('8.8.8.8', 0)), (2, 1, 6, '', ('192.168.1.1', 0))]
        with patch('geolocation.socket.getaddrinfo', return_value=fake):
            self.assertEqual(resolve_endpoint('endpoint.example'), '8.8.8.8')

    def test_only_geolocates_selected_hosts_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / 'local_geo_cache.json'
            with patch('geolocation.resolve_endpoint', side_effect=['8.8.8.8', '8.8.8.8']), \
                 patch('geolocation.lookup_country', return_value='US') as lookup:
                countries = geolocate_hosts(['server1.example', 'server2.example'], cache_path, workers=1)
                self.assertEqual(lookup.call_count, 1)
            self.assertEqual(countries['server1.example'], CountryInfo('8.8.8.8', 'US', '🇺🇸'))
            self.assertEqual(countries['server2.example'].flag, '🇺🇸')
            with patch('geolocation.resolve_endpoint', return_value='8.8.8.8'), \
                 patch('geolocation.lookup_country', side_effect=AssertionError('Cache was not used')):
                cached = geolocate_hosts(['server1.example'], cache_path, workers=1)
                self.assertEqual(cached['server1.example'].country_code, 'US')

    def test_failed_geo_returns_globe_not_fake_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('geolocation.resolve_endpoint', return_value='8.8.8.8'), \
                 patch('geolocation.lookup_country', return_value=None):
                country = geolocate_hosts(['myserver.example'], Path(tmp) / 'cache.json', workers=1)['myserver.example']
                self.assertIsNone(country.country_code)
                self.assertEqual(country.flag, '🌐')


if __name__ == '__main__':
    unittest.main()
