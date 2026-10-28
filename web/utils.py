import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

_HOMELAB_ENV = '/etc/homelab/brewlog.env'


def env_path():
    """Where the secrets live: $BREWLOG_ENV, else /etc/homelab/brewlog.env
    (WEBAPP_PROJECT_STANDARD.md §8) when readable, else .env in the project root."""
    override = os.environ.get('BREWLOG_ENV')
    if override:
        return override
    if os.access(_HOMELAB_ENV, os.R_OK):
        return _HOMELAB_ENV
    return os.path.join(_ROOT, '.env')


def load_env():
    """Parse the env file (see env_path()); return dict of key→value."""
    env = {}
    env_path_ = env_path()
    if os.path.isfile(env_path_):
        with open(env_path_, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]                       # quoted value
                    else:
                        v = v.split(' #', 1)[0].strip()   # trailing comment
                    env[k.strip()] = v
    return env
