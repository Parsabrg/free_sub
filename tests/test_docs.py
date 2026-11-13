"""The documentation must not point at files or headings that do not exist."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / 'README.md', ROOT / 'CHANGELOG.md', ROOT / 'tools' / 'README.md', *sorted((ROOT / 'guide').glob('*.md'))]
LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')


def anchors(page):
    """GitHub's heading anchors: lower case, punctuation dropped, spaces to hyphens."""
    found = set()
    for line in page.read_text(encoding='utf-8').splitlines():
        if line.startswith('#'):
            title = line.lstrip('#').strip().lower()
            found.add(re.sub(r'[^\w\- ]', '', title).replace(' ', '-'))
    return found


class DocumentationTests(unittest.TestCase):
    def test_every_relative_link_leads_somewhere(self):
        self.assertGreaterEqual(len(PAGES), 7)
        for page in PAGES:
            for target in LINK.findall(page.read_text(encoding='utf-8')):
                if target.startswith(('http://', 'https://', 'mailto:')):
                    continue
                path, _, anchor = target.partition('#')
                linked = (page.parent / path).resolve() if path else page
                self.assertTrue(linked.exists(), f'{page.name}: {target}')
                if anchor and linked.suffix == '.md':
                    self.assertIn(anchor, anchors(linked), f'{page.name}: {target}')

    def test_the_readme_lists_every_built_in_source_repository(self):
        from collector import SOURCES
        readme = (ROOT / 'README.md').read_text(encoding='utf-8').lower()
        for name, url in SOURCES.items():
            host = url.split('/')[2]
            owner = url.split('/')[3] if host == 'raw.githubusercontent.com' else host
            self.assertIn(owner.lower(), readme, name)


if __name__ == '__main__':
    unittest.main()
