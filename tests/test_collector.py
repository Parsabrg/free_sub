import base64
import json
import unittest
from collector import parse,links
from xray_test import make_config

class ParserTests(unittest.TestCase):
    def test_vless_reality(self):
        s='vless://a22c18ad-1111-4444-9999-d41e8cd98f00@1.2.3.4:443?type=tcp&security=reality&sni=www.example.org&pbk=abcdefghijklmnopqrstuvwxyz&sid=07&fp=chrome&flow=xtls-rprx-vision#label'
        c=parse(s,'Barry')
        self.assertIsNotNone(c)
        self.assertEqual(c.outbound['streamSettings']['realitySettings']['publicKey'],'abcdefghijklmnopqrstuvwxyz')
        self.assertEqual(c.outbound['settings']['vnext'][0]['users'][0]['flow'],'xtls-rprx-vision')
        self.assertEqual(c.key,parse(s.rsplit('#',1)[0]+'#different','EbraSha').key)
    def test_tls_ws(self):
        c=parse('trojan://password@example.com:443?security=tls&type=ws&host=cdn.example.net&path=%2Fws&sni=example.com')
        self.assertEqual(c.outbound['streamSettings']['wsSettings']['headers']['Host'],'cdn.example.net')
        self.assertEqual(c.outbound['streamSettings']['wsSettings']['path'],'/ws')
    def test_insecure_rejected(self):
        self.assertIsNone(parse('vless://some-id@a.net:80?security=none'))
        self.assertIsNone(parse('vless://some-id@a.net:443?security=tls&allowInsecure=1'))
    def test_ss(self):
        user=base64.urlsafe_b64encode(b'aes-256-gcm:secret').decode().rstrip('=')
        c=parse(f'ss://{user}@8.8.8.8:2233#hello','Shadowmere')
        self.assertEqual(c.outbound['settings']['servers'][0]['method'],'aes-256-gcm')
        self.assertIsNone(parse(f'ss://{user}@8.8.8.8:2233/?plugin=v2ray-plugin'))
    def test_vmess(self):
        obj={'add':'example.com','port':'443','id':'14a2c062-1234-5678-abcd-0123456789ab','net':'ws','tls':'tls','host':'host.net','path':'/x'}
        link='vmess://'+base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')
        c=parse(link)
        self.assertEqual(c.outbound['protocol'],'vmess')
        self.assertEqual(c.outbound['streamSettings']['wsSettings']['path'],'/x')
    def test_base64_feed(self):
        v='vless://id@host.example:443?security=tls#node\n'
        encoded=base64.b64encode(v.encode()).decode()
        self.assertEqual(links(encoded),[v.strip()])
    def test_no_direct_outbound(self):
        c=parse('vless://id@host.example:443?security=tls')
        config=make_config(c.outbound,1234)
        self.assertEqual([o['protocol'] for o in config['outbounds']],['vless','blackhole'])
        self.assertEqual(config['routing']['rules'][0]['outboundTag'],'candidate')

if __name__=='__main__': unittest.main()
