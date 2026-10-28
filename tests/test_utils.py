import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone, timedelta

import helpers  # sets up sys.path; must come before web imports
import utils
import fetch_influxdb as influx_module

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class TestConftest(unittest.TestCase):
    """tests/conftest.py must put the tests dir on sys.path so pytest
    can collect the suite (plain `import helpers` works)."""

    def test_tests_dir_on_sys_path(self):
        self.assertIn(os.path.dirname(helpers.__file__), sys.path)


class TestLoadEnv(unittest.TestCase):

    def _load(self, content):
        with tempfile.NamedTemporaryFile('w', suffix='.env', delete=False,
                                         encoding='utf-8') as f:
            f.write(content)
            path = f.name
        try:
            with unittest.mock.patch.object(utils, 'env_path', return_value=path):
                return utils.load_env()
        finally:
            os.unlink(path)

    def test_basic_and_edge_lines(self):
        env = self._load(
            '# comment line\n'
            '\n'
            'NO_EQUALS_LINE\n'
            'A = 1\n'
            'B=two\n'
        )
        self.assertEqual(env, {'A': '1', 'B': 'two'})

    def test_quoted_values(self):
        env = self._load('FTP_PASS="p@ss"\nSSH_KEY=\'/x/y\'\nMIXED="a#b"\n')
        self.assertEqual(env['FTP_PASS'], 'p@ss')
        self.assertEqual(env['SSH_KEY'], '/x/y')
        self.assertEqual(env['MIXED'], 'a#b')

    def test_inline_comment(self):
        env = self._load('FTP_HOST=x  # comment\nURL=http://a#frag\n')
        self.assertEqual(env['FTP_HOST'], 'x')
        self.assertEqual(env['URL'], 'http://a#frag')


class TestEnvPath(unittest.TestCase):

    def test_brewlog_env_override(self):
        with unittest.mock.patch.dict(os.environ, {'BREWLOG_ENV': '/tmp/custom.env'}):
            self.assertEqual(utils.env_path(), '/tmp/custom.env')


class TestClaudeMdDeployPath(unittest.TestCase):
    """CLAUDE.md must name a deploy script that actually exists."""

    def test_deploy_script_exists(self):
        with open(os.path.join(ROOT, 'CLAUDE.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('deploy/deploy.sh', text)
        self.assertNotIn('infrastructure/scripts/deploy.sh', text)
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'deploy', 'deploy.sh')))


class TestParseDt(unittest.TestCase):

    def test_date_only(self):
        dt = influx_module._parse_dt('2024-03-15')
        self.assertEqual(dt, datetime(2024, 3, 15, tzinfo=timezone.utc))

    def test_iso_utc(self):
        dt = influx_module._parse_dt('2024-03-15T10:00:00Z')
        self.assertEqual(dt, datetime(2024, 3, 15, 10, 0, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(dt.tzinfo)

    def test_garbage_returns_none(self):
        self.assertIsNone(influx_module._parse_dt('garbage'))
        self.assertIsNone(influx_module._parse_dt('01.04.2024'))
        self.assertIsNone(influx_module._parse_dt(''))
        self.assertIsNone(influx_module._parse_dt(None))


def _write_bj(data_dir, snum, sb, name='Test Beer'):
    os.makedirs(data_dir, exist_ok=True)
    bj = {'beerjson': {'recipes': [{'name': name, '_brewery': sb}]}}
    path = os.path.join(data_dir, f'{snum}_beerjson.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(bj, f)
    return path


class TestFetchBrew(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmpdir, 'data')
        self.out_dir = os.path.join(self.tmpdir, 'influx')
        self.patcher = unittest.mock.patch.multiple(
            influx_module, DATA_DIR=self.data_dir, OUT_DIR=self.out_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_malformed_beerjson_returns_false(self):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, '5_beerjson.json'), 'w',
                  encoding='utf-8') as f:
            f.write('{ this is not json')
        self.assertFalse(influx_module.fetch_brew(None, 5))

    def test_unparseable_braudatum_returns_false(self):
        _write_bj(self.data_dir, 6, {'braudatum': '01.04.2024'})
        self.assertFalse(influx_module.fetch_brew(None, 6))

    def test_stop_dt_never_before_start_dt(self):
        # Abfuelldatum before Braudatum (data-entry error) must not produce
        # an inverted query window.
        _write_bj(self.data_dir, 7,
                  {'braudatum': '2024-03-15T10:00:00Z',
                   'abfuelldatum': '2024-03-10'})
        start_dt = datetime(2024, 3, 15, 10, 0, tzinfo=timezone.utc)
        captured = []
        with unittest.mock.patch.object(influx_module, '_query_brew_day',
                                        return_value=[]), \
             unittest.mock.patch.object(influx_module, '_query_ispindel',
                                        side_effect=lambda qa, s, e, r:
                                        (captured.append((s, e)) or [])):
            self.assertTrue(influx_module.fetch_brew(None, 7))
        self.assertTrue(captured, 'no fermentation window was queried')
        for s, e in captured:
            self.assertGreaterEqual(e, s + timedelta(days=1))

    def test_main_rejects_non_numeric_args(self):
        idx = {'beers': [{'sudnummer': 1}]}
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, 'index.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(idx, f)
        argv = ['fetch_influxdb.py', '42x']
        with unittest.mock.patch.object(sys, 'argv', argv), \
             unittest.mock.patch.multiple(
                 influx_module, HAS_INFLUXDB=True,
                 INFLUXDB_URL='http://127.0.0.1:1', INFLUXDB_TOKEN='t'):
            with self.assertRaises(SystemExit):
                influx_module.main()


if __name__ == '__main__':
    unittest.main()
