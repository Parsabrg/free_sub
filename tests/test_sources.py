import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import sources
from collector import collect, links, parse
from main import arguments, pick, resting, run_cycle
from xray_test import ProbeResult


def link(i, host='example.com'):
    return f'trojan://secret{i}@{host}:443?security=tls'


def args_for(*extra):
    version = subprocess.CompletedProcess([], 0, stdout='Xray fixture')
    with patch('main.subprocess.run', return_value=version):
        return arguments(['--xray', sys.executable, '--mode', 'full', *extra])


class ParserCoverageTests(unittest.TestCase):
    def test_a_key_with_no_value_configures_nothing(self):
        base = 'vless://id@example.com:443?security=tls'
        self.assertEqual(parse(base + '&pcs=&ech=&authority=').key, parse(base).key)
        self.assertIsNone(parse(base + '&pcs=abcdef'))
        obj = {'add': 'example.com', 'port': 443, 'id': 'id', 'tls': 'tls', 'net': 'ws'}
        encode = lambda v: 'vmess://' + base64.b64encode(json.dumps(v).encode()).decode()
        self.assertEqual(parse(encode(dict(obj, pcs='', vcn='', ech=None))).key, parse(encode(obj)).key)
        self.assertIsNone(parse(encode(dict(obj, pcs='ab12'))))
        self.assertIsNone(parse(encode(dict(obj, **{'skip-cert-verify': True}))))

    def test_raw_transport_ignores_a_host_it_never_sends(self):
        base = 'vless://id@1.2.3.4:443?type=tcp&security=reality&sni=a.example&pbk=key'
        self.assertEqual(parse(base + '&host=a.example').key, parse(base).key)
        self.assertIsNone(parse(base + '&host=a.example&headerType=http'))

    def test_html_escaped_separators_are_read(self):
        escaped = 'vless://id@example.com:443?security=tls&amp;type=ws&amp;path=%2Fx#n'
        self.assertEqual(links(escaped), ['vless://id@example.com:443?security=tls&type=ws&path=%2Fx#n'])

    def test_a_source_can_publish_several_files_and_survives_one_reset(self):
        calls = []
        def get(url, timeout=20):
            calls.append(url)
            if url.endswith('/a') and calls.count(url) == 1:
                raise ConnectionResetError()
            if url.endswith('/gone'):
                raise OSError()
            return link(1) + '\n' + link(2 if url.endswith('/a') else 3)
        stats = {}
        with patch('collector.get', get), redirect_stdout(io.StringIO()):
            groups = collect({'Two': ['https://x.example/a', 'https://x.example/b'], 'Dead': 'https://x.example/gone'},
                             stats=stats)
        self.assertEqual(len(groups['Two']), 3)
        self.assertEqual(stats['Two'], {'raw': 4, 'supported': 3, 'error': None})
        self.assertEqual(stats['Dead']['error'], 'OSError')


class RegistryTests(unittest.TestCase):
    def test_manual_sources_are_validated_and_never_named_after_their_address(self):
        registry = sources.empty()
        self.assertEqual(sources.add_manual(registry, ' https://private.example/sub?token=abc '), 'Custom 1')
        self.assertEqual(sources.add_manual(registry, 'https://other.example/sub', 'My  feed'), 'My feed')
        for bad in ('http://plain.example/sub', 'file:///etc/passwd', 'https://', 'https://a.example/x y',
                    'https://private.example/sub?token=abc'):
            with self.assertRaises(ValueError):
                sources.add_manual(registry, bad)
        for name in ('Barry', 'My feed', 'gh:owner/repo', '<script>'):
            with self.assertRaises(ValueError):
                sources.add_manual(registry, 'https://new.example/' + str(len(name)), name)
        sources.set_enabled(registry, 'Barry', False)
        sources.set_enabled(registry, 'Custom 1', False)
        self.assertEqual(registry['off'], ['Barry', 'Custom 1'])
        sources.remove(registry, 'Custom 1')
        self.assertEqual((list(registry['manual']), registry['off']), (['My feed'], ['Barry']))
        with self.assertRaises(ValueError):
            sources.set_enabled(registry, 'Nobody', False)

    def test_readme_links_stay_inside_their_own_repository(self):
        readme = '''[sub](https://raw.githubusercontent.com/Owner/Repo/main/all.txt) and
        https://github.com/owner/repo/blob/main/split/vless.txt, logo
        https://raw.githubusercontent.com/Owner/Repo/main/logo.png
        credit https://raw.githubusercontent.com/someone/else/main/all.txt
        https://evil.example/raw.githubusercontent.com/Owner/Repo/main/x.txt
        https://raw.githubusercontent.com/Owner/Repo/main/../../other/repo/x'''
        self.assertEqual(sources.feed_urls(readme, 'Owner/Repo'),
                         ['https://raw.githubusercontent.com/Owner/Repo/main/all.txt',
                          'https://raw.githubusercontent.com/Owner/Repo/main/split/vless.txt'])

    def test_split_copies_of_one_list_are_not_chosen_twice(self):
        everything = set(range(100))
        feeds = {'all': everything, 'vless': set(range(60)), 'trojan': set(range(60, 100)),
                 'other': set(range(100, 140)), 'tiny': {900, 901}}
        self.assertEqual(sources.choose(feeds), (['all', 'other'], 140))
        self.assertEqual(sources.choose({'tiny': {1, 2}}), ([], 0))

    def discovery(self, folder, feeds):
        def search(query):
            return [{'full_name': 'good/feed', 'default_branch': 'main', 'stargazers_count': 5},
                    {'full_name': 'dud/feed', 'default_branch': 'main', 'stargazers_count': 9},
                    {'full_name': 'some/fork', 'default_branch': 'main', 'fork': True},
                    {'full_name': 'barry-far/V2ray-Config', 'default_branch': 'main'},
                    {'full_name': '../../etc', 'default_branch': 'main'}]
        def fetch(url, timeout=20):
            if url.endswith('/README.md'):
                repository = '/'.join(url.split('/')[3:5])
                return f'https://raw.githubusercontent.com/{repository}/main/sub.txt'
            return feeds['/'.join(url.split('/')[3:5])]
        return sources.Registry(Path(folder) / '.local/sources.json', fetch=fetch, search=search), fetch

    def test_found_feeds_are_sampled_until_one_works_then_tested_wider(self):
        feeds = {'good/feed': '\n'.join(link(i, 'good.example') for i in range(400)),
                 'dud/feed': '\n'.join(link(i, 'dud.example') for i in range(400)) + '\n' + link(0, 'listed.example')}
        listed = [parse(link(0, 'listed.example'), 'Listed')]
        probe = lambda c, xray, timeout, **kw: ProbeResult(c.host != 'dud.example', endpoint_ip='8.8.8.8', body_bytes=16384)
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            registry, fetch = self.discovery(folder, feeds)
            args = args_for('--recheck', '400')
            now = 1_800_000_000
            with patch('collector.get', fetch):
                report = run_cycle(args, folder, probe, lambda **kw: {'Listed': listed}, lambda *a, **kw: {}, now,
                                   registry=registry)
            status = report['source_status']
            self.assertEqual(set(status), {'Listed', 'gh:good/feed', 'gh:dud/feed'})
            # 150 of each unknown feed, and never what a listed source already carries.
            self.assertEqual((status['gh:good/feed']['tested'], status['gh:dud/feed']['tested']), (150, 150))
            self.assertEqual((status['gh:dud/feed']['supported'], status['gh:dud/feed']['new']), (401, 400))
            self.assertEqual(report['unique_selected_for_test'], 301)
            self.assertEqual(status['gh:dud/feed']['working'], 0)
            self.assertGreater(status['gh:good/feed']['working'], 0)
            found = registry.data['found']
            self.assertEqual(set(found), {'good/feed', 'dud/feed'})
            self.assertEqual((found['good/feed']['dry'], found['good/feed']['next']), (0, 0))
            self.assertEqual((found['dud/feed']['dry'], found['dud/feed']['next']), (1, now + 8 * 3600))
            self.assertEqual(json.loads((Path(folder) / '.local/sources.json').read_text())['found'], found)
            # Four hours on: the feed that worked gets the wide quota, the dud is left alone.
            later = now + 4 * 3600
            with patch('collector.get', fetch):
                report = run_cycle(args, folder, probe, lambda **kw: {'Listed': listed}, lambda *a, **kw: {}, later,
                                   registry=registry)
            self.assertNotIn('gh:dud/feed', report['source_status'])
            self.assertEqual(report['source_status']['gh:good/feed']['tested'], 400)
            self.assertEqual(registry.quota('gh:good/feed', later), sources.PROVEN)
            self.assertEqual(registry.quota('gh:dud/feed', later), sources.SAMPLE)

    def test_no_search_answer_changes_nothing_and_discovery_can_be_off(self):
        def offline(query):
            raise OSError('filtered')
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            registry = sources.Registry(Path(folder) / 'sources.json', search=offline)
            self.assertEqual(registry.collect(1_800_000_000, {}), ({}, {}))
            self.assertEqual(registry.data['refreshed'], 0)
            quiet = sources.Registry(Path(folder) / 'sources.json', discover=0, search=self.fail)
            self.assertEqual(quiet.collect(1_800_000_000, {}), ({}, {}))

    def test_manual_and_disabled_sources_reach_the_run(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            registry = sources.Registry(Path(folder) / 'sources.json', discover=0)
            sources.add_manual(registry.data, 'https://mine.example/sub', 'Mine')
            sources.add_manual(registry.data, 'https://mine.example/off', 'Paused')
            sources.set_enabled(registry.data, 'Paused', False)
            sources.set_enabled(registry.data, 'Barry', False)
            self.assertNotIn('Barry', registry.builtin())
            self.assertIn('Shadowmere', registry.builtin())
            stats = {}
            with patch('collector.get', lambda url, timeout=20: link(7, 'mine.example')):
                own, found = registry.collect(1_800_000_000, stats)
            self.assertEqual(([c.host for c in own['Mine']], list(own), found), (['mine.example'], ['Mine'], {}))
            self.assertEqual(stats['Mine']['kind'], 'manual')


class BudgetTests(unittest.TestCase):
    def test_a_config_that_keeps_failing_rests_for_a_day(self):
        now = 1_800_000_000
        fail = lambda at: {'at': at, 'ok': False, 'latency_ms': 0}
        row = {'last_success': 0, 'last_attempt': now - 3600, 'history': [fail(now - 9 * 3600), fail(now - 5 * 3600), fail(now - 3600)]}
        self.assertTrue(resting(row, now, 24))
        self.assertFalse(resting(row, now, 0))
        self.assertFalse(resting(row, now + 24 * 3600, 24))
        self.assertFalse(resting(dict(row, history=row['history'][:2]), now, 24))
        self.assertFalse(resting(dict(row, last_success=now - 86400), now, 24))
        self.assertTrue(resting(dict(row, last_success=now - 4 * 86400), now, 24))
        self.assertFalse(resting(dict(row, history=row['history'] + [{'at': now, 'ok': True, 'latency_ms': 9}]), now, 24))
        self.assertFalse(resting(None, now, 24))

    def test_resting_configs_are_skipped_but_published_ones_never_are(self):
        candidates = [parse(link(i)) for i in range(6)]
        probe = lambda c, xray, timeout, **kw: ProbeResult(c.key == candidates[0].key, endpoint_ip='8.8.8.8', body_bytes=16384)
        collect_fn = lambda **kw: {'Fixture': candidates}
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            args = args_for()
            now = 1_800_000_000
            for run in range(3):
                report = run_cycle(args, folder, probe, collect_fn, lambda *a, **kw: {}, now + run * 4 * 3600)
                self.assertEqual((report['unique_selected_for_test'], report['resting_skipped']), (6, 0))
            report = run_cycle(args, folder, probe, collect_fn, lambda *a, **kw: {}, now + 12 * 3600)
            self.assertEqual((report['unique_selected_for_test'], report['resting_skipped'], report['published']), (1, 5, 1))
            self.assertEqual(report['source_status']['Fixture'], {'tested': 1, 'working': 1})
            report = run_cycle(args, folder, probe, collect_fn, lambda *a, **kw: {}, now + 34 * 3600)
            self.assertEqual((report['unique_selected_for_test'], report['resting_skipped']), (6, 0))

    def test_sampling_prefers_configs_never_tested(self):
        import random
        candidates = [parse(link(i)) for i in range(10)]
        state = {'nodes': {c.key: {'last_attempt': 100 + i} for i, c in enumerate(candidates[:6])}}
        chosen = pick(candidates, 5, state, random.Random('seed'))
        self.assertEqual({c.key for c in chosen[:4]}, {c.key for c in candidates[6:]})
        self.assertEqual(chosen[4].key, candidates[0].key)
        self.assertEqual(pick(candidates[:3], 5, state, random.Random('seed')), candidates[:3])


if __name__ == '__main__':
    unittest.main()
