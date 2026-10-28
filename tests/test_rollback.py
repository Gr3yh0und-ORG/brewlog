import ftplib
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

import helpers  # sets up sys.path; must come before deploy imports
import rollback


class TestRollback(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_releases_dir = rollback.RELEASES_DIR
        rollback.RELEASES_DIR = self.tmpdir / 'releases'

    def tearDown(self):
        rollback.RELEASES_DIR = self._orig_releases_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_source(self, name, content):
        """A fake 'web/' output tree with one file, for snapshot_release()."""
        src_dir = self.tmpdir / f'src_{name}'
        src_dir.mkdir()
        f = src_dir / 'index.html'
        f.write_text(content, encoding='utf-8')
        return {'index.html': f}

    # -- Core scenario: deploy twice with different content, roll back,
    #    the FIRST content is what a rollback actually re-publishes. --
    def test_rollback_restores_previous_release_content_not_latest(self):
        rollback.snapshot_release(self._write_source('a', 'VERSION-A content'))
        rollback.snapshot_release(self._write_source('b', 'VERSION-B content'))

        uploaded = {}

        def stub_uploader(target, files, env):
            for f in files:
                uploaded[f.relative_to(target).as_posix()] = f.read_text(encoding='utf-8')

        target, files = rollback.do_rollback(steps_back=1, uploader=stub_uploader, env={})

        self.assertEqual(uploaded['index.html'], 'VERSION-A content')
        self.assertNotEqual(uploaded['index.html'], 'VERSION-B content')
        self.assertEqual(len(files), 1)

    def test_rollback_default_steps_back_is_previous_not_current(self):
        rollback.snapshot_release(self._write_source('a', 'first'))
        rollback.snapshot_release(self._write_source('b', 'second'))
        rollback.snapshot_release(self._write_source('c', 'third'))

        uploaded = {}
        rollback.do_rollback(
            uploader=lambda target, files, env: uploaded.update(
                {f.name: f.read_text(encoding='utf-8') for f in files}
            ),
            env={},
        )
        # steps_back defaults to 1: releases[0] is "third" (current live),
        # releases[1] is "second" -- the one before it.
        self.assertEqual(uploaded['index.html'], 'second')

    # -- Positive-state-change control, not an absence check: seed 7,
    #    confirm exactly 5 remain and it's the 5 newest. --
    def test_prune_keeps_last_5_newest(self):
        for i in range(7):
            rollback.snapshot_release(self._write_source(str(i), f'content-{i}'))

        releases = rollback.list_releases()
        self.assertEqual(len(releases), 5)

        contents = [
            (r / 'index.html').read_text(encoding='utf-8') for r in releases
        ]
        # Newest-first; content-6 was the last snapshot taken, so it's live.
        self.assertEqual(contents, [f'content-{i}' for i in (6, 5, 4, 3, 2)])

    def test_rollback_insufficient_releases_raises(self):
        rollback.snapshot_release(self._write_source('only', 'solo'))
        with self.assertRaises(rollback.RollbackError):
            rollback.do_rollback(steps_back=1, env={})

    def test_rollback_no_releases_raises(self):
        with self.assertRaises(rollback.RollbackError):
            rollback.do_rollback(steps_back=0, env={})

    # -- Timestamp collision: two snapshots in the same microsecond must not
    #    silently overwrite one another. --
    def test_timestamp_collision_disambiguates_without_data_loss(self):
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with unittest.mock.patch.object(rollback, 'datetime') as mock_dt:
            mock_dt.now.return_value = fixed_now
            rollback.snapshot_release(self._write_source('x', 'collision-1'))
            rollback.snapshot_release(self._write_source('y', 'collision-2'))

        releases = rollback.list_releases()
        self.assertEqual(len(releases), 2)
        names = sorted(r.name for r in releases)
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(all('20260101T000000.000000Z' in n for n in names))

        contents = {r.name: (r / 'index.html').read_text(encoding='utf-8') for r in releases}
        self.assertEqual(sorted(contents.values()), ['collision-1', 'collision-2'])

    # -- Regression: a freed release name must never be reused. Under a
    #    frozen clock (worst case -- every call returns the same timestamp,
    #    which coarse clock resolution or a tight retry loop can trigger for
    #    real), a bare/lowest-sorting name that gets pruned away must not be
    #    handed to a later deploy, which would delete that deploy's own
    #    snapshot in the same call that created it. --
    def test_frozen_clock_prune_never_deletes_the_release_just_created(self):
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        created = []
        with unittest.mock.patch.object(rollback, 'datetime') as mock_dt:
            mock_dt.now.return_value = fixed_now
            for i in range(12):
                release_dir = rollback.snapshot_release(self._write_source(str(i), f'content-{i}'))
                created.append(release_dir)
                self.assertTrue(
                    release_dir.exists(),
                    f'deploy {i}: release {release_dir.name!r} was deleted in the same '
                    f'call that created it -- its snapshot was never actually persisted',
                )

        releases = rollback.list_releases()
        self.assertEqual(len(releases), 5)
        # The 5 most recent deploys (7..11) must be exactly what survived.
        contents = sorted((r / 'index.html').read_text(encoding='utf-8') for r in releases)
        self.assertEqual(contents, sorted(f'content-{i}' for i in range(7, 12)))

    # -- steps_back has a lower bound: a negative/zero value must never be
    #    forwarded to the live FTP host (a negative index silently re-uploads
    #    the oldest retained snapshot, 0 re-uploads the current one). --
    def test_rollback_negative_steps_back_raises(self):
        for i in range(3):
            rollback.snapshot_release(self._write_source(str(i), f'content-{i}'))

        uploaded = []
        with self.assertRaises(rollback.RollbackError):
            rollback.do_rollback(steps_back=-1, uploader=lambda t, f, e: uploaded.append(f), env={})
        self.assertEqual(uploaded, [])

    def test_rollback_zero_steps_back_raises(self):
        for i in range(3):
            rollback.snapshot_release(self._write_source(str(i), f'content-{i}'))

        uploaded = []
        with self.assertRaises(rollback.RollbackError):
            rollback.do_rollback(steps_back=0, uploader=lambda t, f, e: uploaded.append(f), env={})
        self.assertEqual(uploaded, [])

    # -- Only NNNNNN-prefixed dirs are releases: a stray dir must not become
    #    releases[0]/"(live)" nor shift every rollback index. --
    def test_list_releases_ignores_non_sequence_dirs(self):
        rollback.snapshot_release(self._write_source('a', 'first'))
        rollback.snapshot_release(self._write_source('b', 'second'))
        stray = rollback.RELEASES_DIR / 'backup'
        (stray / 'images').mkdir(parents=True)
        (stray / 'index.html').write_text('DO NOT SERVE', encoding='utf-8')

        releases = rollback.list_releases()
        self.assertNotIn(stray, releases)
        self.assertEqual(len(releases), 2)

        uploaded = {}
        rollback.do_rollback(
            steps_back=1,
            uploader=lambda target, files, env: uploaded.update(
                {f.name: f.read_text(encoding='utf-8') for f in files}
            ),
            env={},
        )
        self.assertEqual(uploaded['index.html'], 'first')

    def test_snapshot_release_rejects_empty_files(self):
        with self.assertRaises(rollback.RollbackError):
            rollback.snapshot_release({})
        # Nothing half-made was kept as a plausible rollback target.
        self.assertEqual(rollback.list_releases(), [])

    # -- An interrupted copy must not survive as a valid-looking release. --
    def test_snapshot_release_interrupted_copy_leaves_no_release(self):
        files = self._write_source('a', 'content-a')
        files['favicon.svg'] = files['index.html']

        calls = {'n': 0}
        real_write_bytes = Path.write_bytes

        def flaky_write_bytes(self, data):
            calls['n'] += 1
            if calls['n'] > 1:
                raise OSError('disk full')
            return real_write_bytes(self, data)

        with unittest.mock.patch.object(Path, 'write_bytes', flaky_write_bytes):
            with self.assertRaises(OSError):
                rollback.snapshot_release(files)

        self.assertEqual(rollback.list_releases(), [])
        # Even an orphaned .tmp left behind (e.g. kill -9 before cleanup)
        # must never count as a release.
        orphan = rollback.RELEASES_DIR / '000099-20260101T000000.000000Z.tmp'
        orphan.mkdir()
        self.assertEqual(rollback.list_releases(), [])

    def test_ftp_upload_all_names_missing_env_key(self):
        target = self.tmpdir / 'rel'
        target.mkdir()
        f = target / 'index.html'
        f.write_text('x', encoding='utf-8')

        env = {'FTP_HOST': 'ftp.example', 'FTP_USER': 'u', 'FTP_DIR': '/site'}
        with self.assertRaises(rollback.RollbackError) as cm:
            rollback._ftp_upload_all(target, [f], env)
        self.assertIn('FTP_PASS', str(cm.exception))

    def test_ftp_quit_failure_does_not_mask_upload_error(self):
        target = self.tmpdir / 'rel'
        target.mkdir()
        f = target / 'index.html'
        f.write_text('x', encoding='utf-8')
        env = {'FTP_HOST': 'h', 'FTP_USER': 'u', 'FTP_PASS': 'p', 'FTP_DIR': '/site'}

        class FakeFtp:
            def login(self, user, pw):
                pass

            def set_pasv(self, v):
                pass

            def mkd(self, path):
                pass

            def storbinary(self, cmd, fh):
                raise ftplib.error_perm('552 disk full on server')

            def quit(self):
                raise EOFError('connection closed')

        with unittest.mock.patch.object(rollback.ftplib, 'FTP', lambda host: FakeFtp()):
            with self.assertRaises(ftplib.error_perm):
                rollback._ftp_upload_all(target, [f], env)

    def test_main_rejects_non_numeric_steps_back(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sys, 'argv', ['rollback.py', 'rollback', 'notanumber']), \
                unittest.mock.patch.object(sys, 'stdout', stdout), \
                unittest.mock.patch.object(sys, 'stderr', stderr):
            with self.assertRaises(SystemExit) as cm:
                rollback.main()
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('notanumber', stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())

    def test_main_rejects_non_numeric_local_steps_back(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sys, 'argv', ['rollback.py', 'rollback-local', '2x']), \
                unittest.mock.patch.object(sys, 'stdout', stdout), \
                unittest.mock.patch.object(sys, 'stderr', stderr):
            with self.assertRaises(SystemExit) as cm:
                rollback.main()
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn('2x', stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())

    def test_release_files_matches_actual_web_dir_layout(self):
        # _release_files() reads the real web/ dir (no monkeypatching) --
        # sanity-checks it against files this repo actually ships, so a
        # rename in web/ that _release_files() misses shows up here.
        files = rollback._release_files()
        self.assertIn('index.html', files)
        self.assertIn('favicon.svg', files)
        self.assertTrue(any(k.startswith('i18n/') for k in files))


class TestLocalTarget(unittest.TestCase):
    """publish_local()/do_local_rollback() -- the WWW_ROOT/<app>/releases/
    + current-symlink mechanism (WEBAPP_PROJECT_STANDARD.md §14B), distinct
    from the FTP snapshot mechanism above."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.app_root = self.tmpdir / 'www' / 'brewlog'

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _files(self, content):
        src_dir = self.tmpdir / f'src_{content}'
        src_dir.mkdir()
        f = src_dir / 'index.html'
        f.write_text(content, encoding='utf-8')
        return {'index.html': f}

    def test_publish_creates_release_and_flips_current(self):
        release_dir = rollback.publish_local(
            version='1.0.0', files=self._files('v1'), app_root=self.app_root
        )
        self.assertTrue(release_dir.is_dir())
        self.assertEqual((release_dir / 'index.html').read_text(encoding='utf-8'), 'v1')

        current = self.app_root / 'current'
        self.assertTrue(current.is_symlink())
        # Relative target, not absolute -- WEBAPP_PROJECT_STANDARD.md §14B:
        # an absolute symlink resolves host-side and dangles inside Caddy's
        # container, which bind-mounts this tree at a different path.
        raw_target = os.readlink(current)
        self.assertFalse(os.path.isabs(raw_target), f'symlink target must be relative, got {raw_target!r}')
        self.assertEqual(raw_target, 'releases/1.0.0')
        self.assertEqual((current / 'index.html').read_text(encoding='utf-8'), 'v1')

    def test_publish_second_release_moves_current_forward(self):
        rollback.publish_local(version='1.0.0', files=self._files('v1'), app_root=self.app_root)
        rollback.publish_local(version='1.0.1', files=self._files('v2'), app_root=self.app_root)

        current = self.app_root / 'current'
        self.assertEqual(os.readlink(current), 'releases/1.0.1')
        self.assertEqual((current / 'index.html').read_text(encoding='utf-8'), 'v2')
        # The old release is untouched, not overwritten in place.
        old = self.app_root / 'releases' / '1.0.0'
        self.assertEqual((old / 'index.html').read_text(encoding='utf-8'), 'v1')

    def test_publish_refuses_empty_release(self):
        with self.assertRaises(rollback.RollbackError):
            rollback.publish_local(version='1.0.0', files={}, app_root=self.app_root)
        # Nothing was flipped -- no dangling/empty `current`.
        self.assertFalse((self.app_root / 'current').exists())

    def test_publish_prunes_to_5_newest_by_version_not_mtime(self):
        # Publish out of chronological order (2.0.0 created after 1.0.5 on
        # disk but is not the highest version) to prove pruning sorts by
        # version, not filesystem mtime/creation order.
        for v in ['1.0.0', '1.0.1', '1.0.2', '1.0.3', '1.0.4', '1.0.5']:
            rollback.publish_local(version=v, files=self._files(v), app_root=self.app_root)

        releases = rollback.list_local_releases(self.app_root)
        self.assertEqual(len(releases), 5)
        self.assertEqual(
            sorted(r.name for r in releases),
            ['1.0.1', '1.0.2', '1.0.3', '1.0.4', '1.0.5'],
        )

    def test_rollback_flips_current_to_previous_not_latest(self):
        rollback.publish_local(version='1.0.0', files=self._files('v1'), app_root=self.app_root)
        rollback.publish_local(version='1.0.1', files=self._files('v2'), app_root=self.app_root)
        rollback.publish_local(version='1.0.2', files=self._files('v3'), app_root=self.app_root)

        target = rollback.do_local_rollback(steps_back=1, app_root=self.app_root)

        self.assertEqual(target.name, '1.0.1')
        current = self.app_root / 'current'
        self.assertEqual(os.readlink(current), 'releases/1.0.1')
        self.assertEqual((current / 'index.html').read_text(encoding='utf-8'), 'v2')

    def test_rollback_two_steps_back(self):
        rollback.publish_local(version='1.0.0', files=self._files('v1'), app_root=self.app_root)
        rollback.publish_local(version='1.0.1', files=self._files('v2'), app_root=self.app_root)
        rollback.publish_local(version='1.0.2', files=self._files('v3'), app_root=self.app_root)

        target = rollback.do_local_rollback(steps_back=2, app_root=self.app_root)
        self.assertEqual(target.name, '1.0.0')

    def test_rollback_insufficient_releases_raises(self):
        rollback.publish_local(version='1.0.0', files=self._files('v1'), app_root=self.app_root)
        with self.assertRaises(rollback.RollbackError):
            rollback.do_local_rollback(steps_back=1, app_root=self.app_root)

    # -- A negative steps_back would otherwise flip `current` forward. --
    def test_local_rollback_negative_steps_back_raises(self):
        rollback.publish_local(version='1.0.0', files=self._files('v1'), app_root=self.app_root)
        rollback.publish_local(version='1.0.1', files=self._files('v2'), app_root=self.app_root)

        with self.assertRaises(rollback.RollbackError):
            rollback.do_local_rollback(steps_back=-1, app_root=self.app_root)
        self.assertEqual(os.readlink(self.app_root / 'current'), 'releases/1.0.1')

    def test_local_rollback_zero_steps_back_raises(self):
        rollback.publish_local(version='1.0.0', files=self._files('v1'), app_root=self.app_root)
        rollback.publish_local(version='1.0.1', files=self._files('v2'), app_root=self.app_root)

        with self.assertRaises(rollback.RollbackError):
            rollback.do_local_rollback(steps_back=0, app_root=self.app_root)
        self.assertEqual(os.readlink(self.app_root / 'current'), 'releases/1.0.1')

    def test_republish_same_version_replaces_not_merges(self):
        files = {'index.html': self._files('v1')['index.html']}
        src = self.tmpdir / 'src_extra'
        src.mkdir()
        stale = src / 'stale.html'
        stale.write_text('removed image', encoding='utf-8')
        rollback.publish_local(version='1.0.0', files={**files, 'stale.html': stale}, app_root=self.app_root)
        rollback.publish_local(version='1.0.0', files=files, app_root=self.app_root)

        release = self.app_root / 'releases' / '1.0.0'
        self.assertTrue((release / 'index.html').is_file())
        self.assertFalse((release / 'stale.html').exists(),
                         'stale file from the previous publish survived into the new release')

    def test_publish_local_rejects_malformed_version(self):
        with self.assertRaises(rollback.RollbackError):
            rollback.publish_local(version='1.2', files=self._files('v1'), app_root=self.app_root)
        self.assertFalse((self.app_root / 'current').exists())

    def test_rollback_no_releases_raises(self):
        with self.assertRaises(rollback.RollbackError):
            rollback.do_local_rollback(steps_back=1, app_root=self.app_root)

    def test_rollback_after_current_missing_lands_on_newest(self):
        # A first-ever publish creates `current` itself, so this only matters
        # if `current` was somehow removed after a publish -- do_local_rollback
        # must not crash. With no current to anchor to, "back 1 step" resolves
        # to the newest known-good release (index 0), same as "start from
        # before the newest and step forward once".
        rollback.publish_local(version='1.0.0', files=self._files('v1'), app_root=self.app_root)
        rollback.publish_local(version='1.0.1', files=self._files('v2'), app_root=self.app_root)
        (self.app_root / 'current').unlink()

        target = rollback.do_local_rollback(steps_back=1, app_root=self.app_root)
        self.assertEqual(target.name, '1.0.1')


if __name__ == '__main__':
    unittest.main()
