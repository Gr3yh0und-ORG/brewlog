"""
Publish/rollback mechanics for brewlog's two Profile B targets
(WEBAPP_PROJECT_STANDARD.md SS14B): `public` (FTP, snapshot-based) and
`local` (a release-dir tree under WWW_ROOT served by the proxy, symlink-based).

deploy.sh/deploy.ps1 call `snapshot` right after export.py/generate_labels.py
and before any upload begins, so the snapshot captures the exact bytes about
to go live -- not the inputs that produced them. brewlog's site bakes in
exported data, so re-running export.py months later against a changed KBH2
database would not reproduce what was actually live; only a byte snapshot
can. `rollback [N]` re-uploads an earlier snapshot verbatim, no rebuild.

The `public` target has no symlink to flip (WEBAPP_PROJECT_STANDARD.md SS14B
is written for a target with one) -- it's a single directory on a remote FTP
host, overwritten in place, the same shape housebuycomparison's deploy.py
solved this for. releases[0] is what's live right now, assuming nothing was
published outside these two scripts.

The `local` target *does* have a symlink (SS14B's default layout:
WWW_ROOT/brewlog/releases/<version>/, WWW_ROOT/brewlog/current -> one of
them) -- publish_local()/do_local_rollback() mirror
infrastructure/scripts/deploy-common.sh's profile_b_publish_local() and
rollback.sh's local branch, kept here instead of shelling out to the shared
scripts because this repo still runs its own bespoke deploy path
(WEBAPP_PROJECT_STANDARD.md SS9, see infrastructure/compliance/brewlog.md SS9).

Usage:
    python3 deploy/rollback.py snapshot          # called by deploy.sh/.ps1 (public)
    python3 deploy/rollback.py list              # show saved public releases
    python3 deploy/rollback.py rollback [N]      # re-upload releases[N], default 1

    python3 deploy/rollback.py publish-local     # called by deploy.sh/.ps1 (local)
    python3 deploy/rollback.py list-local        # show local releases under WWW_ROOT
    python3 deploy/rollback.py rollback-local [N]  # flip `current` back N releases
"""
import ftplib
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / 'web'
RELEASES_DIR = ROOT / 'deploy' / 'releases'
KEEP_RELEASES = 5

sys.path.insert(0, str(WEB_DIR))
from utils import load_env  # noqa: E402

# WWW_ROOT: an env var takes precedence over .env (matches how every other
# optional setting in this repo is read -- utils.load_env() -- but WWW_ROOT
# is server-path-shaped and more likely to need a per-machine override, e.g.
# a Windows workstation reaching the same tree through a mapped drive).
APP_NAME = 'brewlog'
WWW_ROOT = Path(os.environ.get('WWW_ROOT') or load_env().get('WWW_ROOT', '/samba/server/www'))


class RollbackError(Exception):
    pass


def _release_files():
    """{relative remote path: local Path} for everything a full deploy uploads.

    Fixed regardless of --skip-data/--labels -- a snapshot always captures
    the complete site (brewlog is small; don't over-engineer this), not just
    whatever a partial deploy happened to send. labels/ is excluded: printed
    label sheets are a separate artifact, not part of the served site the
    bottle QR codes point at.
    """
    files = {}

    for name in ('index.html', 'favicon.svg'):
        p = WEB_DIR / name
        if p.is_file():
            files[name] = p

    logo_dir = WEB_DIR / 'logo'
    if logo_dir.is_dir():
        for p in sorted(logo_dir.iterdir()):
            if p.is_file():
                files[f'logo/{p.name}'] = p

    for sub in ('i18n', 'data', 'images'):
        d = WEB_DIR / sub
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.is_file():
                    files[f'{sub}/{p.name}'] = p

    return files


_SEQ_RE = re.compile(r'^(\d{6})-')


def list_releases():
    """Saved releases, newest first. Every release dir is prefixed with a
    zero-padded monotonic sequence number (f"{seq:06d}-..."), so lexicographic
    order always matches creation order -- unlike a bare timestamp, a
    sequence number freed by pruning is never reused, so a later release can
    never sort as older than an earlier one that already got pruned."""
    if not RELEASES_DIR.exists():
        return []
    # .tmp dirs only exist mid-copy (or after a kill -9); they're never releases.
    return sorted((d for d in RELEASES_DIR.iterdir()
                   if d.is_dir() and _SEQ_RE.match(d.name) and not d.name.endswith('.tmp')),
                  reverse=True)


def _next_sequence():
    """max(existing sequence numbers) + 1. No counter file needed -- pruning
    only ever removes the lowest-numbered releases, so the highest surviving
    sequence number is always the true running max."""
    if not RELEASES_DIR.exists():
        return 0
    seqs = [int(m.group(1)) for d in RELEASES_DIR.iterdir()
            if d.is_dir() and (m := _SEQ_RE.match(d.name))]
    return max(seqs, default=-1) + 1


def snapshot_release(files=None):
    """Save `files` ({relative path: local Path}) as a new release, prune to KEEP_RELEASES."""
    if files is None:
        files = _release_files()
    if not files:
        raise RollbackError('no files to snapshot -- refusing to save an empty release')

    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    seq = _next_sequence()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    release_dir = RELEASES_DIR / f'{seq:06d}-{stamp}'
    while release_dir.exists():
        seq += 1
        release_dir = RELEASES_DIR / f'{seq:06d}-{stamp}'
    # Copy into a .tmp dir and rename into place, so an interrupt mid-copy
    # (Ctrl-C, full disk) can't leave a partial release as a rollback target.
    tmp_dir = release_dir.with_name(release_dir.name + '.tmp')
    tmp_dir.mkdir(parents=True)
    try:
        for rel_path, local_path in files.items():
            dest = tmp_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(Path(local_path).read_bytes())
        os.replace(tmp_dir, release_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    for old in list_releases()[KEEP_RELEASES:]:
        shutil.rmtree(old)

    return release_dir


def _ftp_mkdirs(ftp, remote_dir):
    """mkdir -p equivalent for FTP; 'already exists' is not an error here."""
    path = ''
    for part in remote_dir.strip('/').split('/'):
        if not part:
            continue
        path += f'/{part}'
        try:
            ftp.mkd(path)
        except ftplib.error_perm:
            pass


def _ftp_upload_all(target, files, env):
    for key in ('FTP_HOST', 'FTP_USER', 'FTP_PASS', 'FTP_DIR'):
        if not env.get(key):
            raise RollbackError(f'{key} is not set -- add it to the env file (see .env.example)')
    host = env['FTP_HOST']
    remote_base = env['FTP_DIR'].rstrip('/')
    ftp = ftplib.FTP(host)
    try:
        ftp.login(env['FTP_USER'], env['FTP_PASS'])
        ftp.set_pasv(True)
        for f in files:
            rel_path = f.relative_to(target).as_posix()
            remote_path = f'{remote_base}/{rel_path}'
            _ftp_mkdirs(ftp, os.path.dirname(remote_path))
            with open(f, 'rb') as fh:
                ftp.storbinary(f'STOR {remote_path}', fh)
    finally:
        # The first exception is the actionable one -- a server that closed
        # the connection mid-error makes quit() raise EOFError/error_perm and
        # would replace it.
        try:
            ftp.quit()
        except Exception:
            pass


def do_rollback(steps_back=1, uploader=None, env=None):
    """Re-upload releases[steps_back] verbatim -- no export.py re-run.

    releases[0] is the most recently deployed version; steps_back=1 (the
    default) re-publishes the release before it.
    """
    if steps_back < 1:
        raise RollbackError(f'steps_back must be >= 1, got {steps_back}')
    releases = list_releases()
    if len(releases) <= steps_back:
        raise RollbackError(
            f'Only {len(releases)} release(s) saved locally under {RELEASES_DIR} '
            f'-- cannot go back {steps_back}.'
        )
    target = releases[steps_back]
    files = sorted(p for p in target.rglob('*') if p.is_file())
    if not files:
        raise RollbackError(f'{target} has no saved files -- nothing to roll back to.')

    if env is None:
        env = load_env()
    if uploader is None:
        uploader = _ftp_upload_all

    uploader(target, files, env)
    return target, files


# ---------------------------------------------------------------------------
# local target (WEBAPP_PROJECT_STANDARD.md SS14B: WWW_ROOT/<app>/releases/<version>/,
# WWW_ROOT/<app>/current -> one of them, served directly by the proxy)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')


def _version():
    """Contents of the repo's VERSION file (WEBAPP_PROJECT_STANDARD.md SS2) --
    the single source of truth for what a local publish is named."""
    return (ROOT / 'VERSION').read_text(encoding='utf-8').strip()


def _version_key(name):
    """Sort key for a release dir name. Unrecognized names (nothing in this
    repo's history predates the local target, so this is defensive rather
    than a migration path) sort oldest, so they're never picked as a
    rollback target and are the first pruned."""
    m = _VERSION_RE.match(name)
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)


def _local_app_root(app_root=None):
    return Path(app_root) if app_root is not None else WWW_ROOT / APP_NAME


def list_local_releases(app_root=None):
    """Local release dirs under <app_root>/releases, newest version first."""
    releases_dir = _local_app_root(app_root) / 'releases'
    if not releases_dir.exists():
        return []
    dirs = [d for d in releases_dir.iterdir() if d.is_dir()]
    return sorted(dirs, key=lambda d: _version_key(d.name), reverse=True)


def _flip_current(app_root, release_name):
    """Point <app_root>/current at releases/<release_name>, atomically.

    Relative target ('releases/1.2.1'), never absolute -- Caddy bind-mounts
    this same tree at a different absolute path inside its container
    (docs/network-topology.md), so an absolute symlink resolves fine from a
    host shell and is silently dangling from inside the container. Written
    via a temp symlink + atomic rename rather than unlink-then-symlink, so
    `current` is never briefly missing under a concurrent request.
    """
    app_root = Path(app_root)
    tmp = app_root / 'current.tmp'
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(Path('releases') / release_name)
    tmp.replace(app_root / 'current')


def publish_local(version=None, files=None, app_root=None, keep=KEEP_RELEASES):
    """Publish `files` to <app_root>/releases/<version>/ and flip `current`
    to it. Mirrors infrastructure/scripts/deploy-common.sh's
    profile_b_publish_local(): refuse to flip onto an empty release, prune
    everything past `keep` newest versions afterwards.
    """
    if version is None:
        version = _version()
    if not _VERSION_RE.match(version):
        raise RollbackError(
            f'VERSION {version!r} is not X.Y.Z -- refusing to publish a release '
            f'that can never be picked as a rollback target'
        )
    if files is None:
        files = _release_files()
    if not files:
        raise RollbackError('no files to publish -- refusing to create an empty release')
    app_root = _local_app_root(app_root)

    release_dir = app_root / 'releases' / version
    # Build into <version>.tmp and swap into place, so re-publishing a version
    # replaces the release instead of merging into it (a file deleted since the
    # last publish must not keep being served under the "new" release).
    tmp_dir = release_dir.with_name(version + '.tmp')
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        for rel_path, local_path in files.items():
            dest = tmp_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(Path(local_path).read_bytes())

        if not any(p.is_file() for p in tmp_dir.rglob('*')):
            raise RollbackError(
                f'{release_dir} has no files after publish -- refusing to point current at it, '
                f'previous release (if any) still live'
            )

        if release_dir.exists():
            shutil.rmtree(release_dir)
        os.replace(tmp_dir, release_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    _flip_current(app_root, version)

    for old in list_local_releases(app_root)[keep:]:
        shutil.rmtree(old)

    return release_dir


def do_local_rollback(steps_back=1, app_root=None):
    """Flip `current` back to an earlier local release -- no re-copy, no rebuild.

    Locates the release `current` points at inside the version-sorted list
    (rather than just excluding it by name, which is all rollback.sh's local
    branch does) so steps_back > 1 and a `current` that points somewhere
    already pruned both resolve correctly instead of silently going back
    only one step or crashing on a missing directory.
    """
    if steps_back < 1:
        raise RollbackError(f'steps_back must be >= 1, got {steps_back}')
    app_root = _local_app_root(app_root)
    current_link = app_root / 'current'
    current_name = None
    if current_link.is_symlink():
        current_name = Path(os.readlink(current_link)).name

    releases = list_local_releases(app_root)
    if not releases:
        raise RollbackError(f'no releases under {app_root / "releases"} to roll back to')

    names = [d.name for d in releases]
    start = names.index(current_name) if current_name in names else -1
    target_idx = start + steps_back

    if target_idx < 0 or target_idx >= len(releases):
        raise RollbackError(
            f'only {len(releases)} local release(s) under {app_root / "releases"} '
            f'-- cannot go back {steps_back} from {current_name or "(no current set)"}'
        )

    target = releases[target_idx]
    _flip_current(app_root, target.name)
    return target


def _parse_steps_back(default=1):
    """int(sys.argv[2]) with a friendly error instead of a ValueError traceback."""
    if len(sys.argv) <= 2:
        return default
    try:
        return int(sys.argv[2])
    except ValueError:
        print(f'Error: N must be a whole number of releases, got {sys.argv[2]!r}', file=sys.stderr)
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'snapshot':
        release_dir = snapshot_release()
        n = sum(1 for _ in release_dir.rglob('*') if _.is_file())
        print(f'Snapshot saved: {release_dir.name} ({n} files)')
    elif cmd == 'list':
        releases = list_releases()
        if not releases:
            print('No releases saved yet.')
        for i, r in enumerate(releases):
            print(f'  [{i}] {r.name}' + (' (live)' if i == 0 else ''))
    elif cmd == 'rollback':
        steps_back = _parse_steps_back()
        try:
            target, files = do_rollback(steps_back)
        except RollbackError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        print(f'Rolled back to release {target.name} ({len(files)} file(s)).')
        print('Live site now serving this release.')
    elif cmd == 'publish-local':
        try:
            release_dir = publish_local()
        except RollbackError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        n = sum(1 for _ in release_dir.rglob('*') if _.is_file())
        print(f'Published local release: {release_dir} ({n} files)')
        print(f'{release_dir.parent.parent / "current"} -> releases/{release_dir.name}')
    elif cmd == 'list-local':
        app_root = _local_app_root()
        releases = list_local_releases()
        current_name = None
        current_link = app_root / 'current'
        if current_link.is_symlink():
            current_name = Path(os.readlink(current_link)).name
        if not releases:
            print(f'No local releases saved yet under {app_root / "releases"}.')
        for r in releases:
            print(f'  {r.name}' + (' (live)' if r.name == current_name else ''))
    elif cmd == 'rollback-local':
        steps_back = _parse_steps_back()
        try:
            target = do_local_rollback(steps_back)
        except RollbackError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        print(f'Rolled back to local release {target.name}.')
        print(f'{target.parent.parent / "current"} -> releases/{target.name}')
    else:
        print(f'Unknown command: {cmd}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
