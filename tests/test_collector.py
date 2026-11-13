import base64
import json
import unittest
from collector import import_candidates,parse,links
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
    def test_hysteria2_ipv6_ports_and_certificate_pin(self):
        pin='a1'*32
        c=parse(f'Hy2://user%3Apass@[2001:db8::1]:443,5000-5002/?sni=example.com&pinSHA256={pin}')
        self.assertEqual((c.protocol,c.host,c.port),('hysteria2','2001:db8::1',443))
        stream=c.outbound['streamSettings']
        self.assertEqual(stream['hysteriaSettings']['auth'],'user:pass')
        self.assertEqual(stream['tlsSettings']['pinnedPeerCertSha256'],pin)
        self.assertEqual(stream['finalmask']['quicParams']['udpHop']['ports'],'443,5000-5002')
        for suffix in (':0',':444-443',':443,99999','?obfs=gecko','?insecure=1'):
            with self.subTest(suffix=suffix):
                self.assertIsNone(parse(f'hy2://password@example.com{suffix}'))

    def test_no_direct_outbound(self):
        c=parse('vless://id@host.example:443?security=tls')
        config=make_config(c.outbound,1234)
        self.assertEqual([o['protocol'] for o in config['outbounds']],['vless','blackhole'])
        self.assertEqual(config['routing']['rules'][0]['outboundTag'],'candidate')

    def test_private_json_rejects_unsafe_nested_settings(self):
        outbound={'protocol':'http','settings':{'address':'example.com','port':443},
                  'streamSettings':{'network':'tcp','security':'tls','tlsSettings':{}}}
        def read(value):
            return import_candidates(json.dumps(value))
        candidate,=read(outbound)
        self.assertTrue(candidate.raw)
        self.assertEqual(candidate.url,'')
        for field,value in (('allowInsecure',True),('certificates',[{'certificateFile':'secret'}]),
                            ('masterKeyLog','secret')):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'position 1') as exc:
                read(dict(outbound,streamSettings={'security':'tls','tlsSettings':{field:value}}))
            self.assertNotIn('secret',str(exc.exception))

if __name__=='__main__': unittest.main()
