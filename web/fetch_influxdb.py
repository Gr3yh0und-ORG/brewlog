"""
InfluxDB Brewery Data Fetcher
Queries fermentation and brew-day data from InfluxDB and writes
input/influxdb/{n}.json files for use by export.py.

Usage:
    python fetch_influxdb.py          # all brews
    python fetch_influxdb.py 50       # single brew
    python fetch_influxdb.py 48 50    # multiple brews

.env keys (connection):
    INFLUXDB_URL     e.g. http://192.168.0.51:8086
    INFLUXDB_TOKEN   InfluxDB v2 API token
    INFLUXDB_ORG     Organisation name (often empty string for home setups)
    INFLUXDB_BUCKET  Bucket name (default: brewery)

.env keys (kettle — mqtt_consumer measurement):
    INFLUXDB_KETTLE_MEASUREMENT   measurement name  (default: mqtt_consumer)
    INFLUXDB_KETTLE_TOPIC_TAG     tag holding MQTT topic  (default: topic)
    INFLUXDB_KETTLE_TOPICS        comma-separated topic list
                                  (default: cave/brewery/temperature/0,cave/brewery/temperature/1)
    INFLUXDB_KETTLE_FIELD         field name for temperature  (default: temperature)
    INFLUXDB_KETTLE_SENSOR_LABELS human-readable label per topic suffix
                                  (default: 0=Kettle,1=Ambient)

.env keys (iSpindel — ispindel measurement):
    INFLUXDB_ISPINDEL_MEASUREMENT  measurement name  (default: ispindel)
    INFLUXDB_ISPINDEL_SOURCE_TAG   tag identifying the device  (default: source)
    INFLUXDB_ISPINDEL_TYPE_TAG     tag distinguishing reading types  (default: field)
    INFLUXDB_ISPINDEL_VALUE_FIELD  field holding the numeric value  (default: value)
    INFLUXDB_ISPINDEL_TEMP_VALUE   type-tag value for temperature  (default: temperature)
    INFLUXDB_ISPINDEL_GRAVITY_VALUE type-tag value for gravity  (default: gravity)

Install dependency:
    pip install influxdb-client
"""
import json, os, sys
from datetime import datetime, timezone, timedelta

try:
    from influxdb_client import InfluxDBClient
    HAS_INFLUXDB = True
except ImportError:
    HAS_INFLUXDB = False

from utils import load_env

ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
OUT_DIR  = os.path.join(ROOT, 'input', 'influxdb')

_env = load_env()

# -- Connection ---------------------------------------------------------------
INFLUXDB_URL    = _env.get('INFLUXDB_URL', '')
INFLUXDB_TOKEN  = _env.get('INFLUXDB_TOKEN', '')
INFLUXDB_ORG    = _env.get('INFLUXDB_ORG', '')
INFLUXDB_BUCKET = _env.get('INFLUXDB_BUCKET', 'brewery')

# -- Kettle (brew day) schema -------------------------------------------------
KETTLE_MEAS      = _env.get('INFLUXDB_KETTLE_MEASUREMENT', 'mqtt_consumer')
KETTLE_TOPIC_TAG = _env.get('INFLUXDB_KETTLE_TOPIC_TAG',   'topic')
KETTLE_TOPICS    = [t.strip() for t in _env.get(
    'INFLUXDB_KETTLE_TOPICS',
    'cave/brewery/temperature/0,cave/brewery/temperature/1',
).split(',') if t.strip()]
KETTLE_FIELD     = _env.get('INFLUXDB_KETTLE_FIELD', 'temperature')
# Human-readable labels per topic suffix (last path segment → label)
# e.g. "0=Kettle,1=Ambient" means sensor/0 → "Kettle", sensor/1 → "Ambient"
_sensor_label_raw = _env.get('INFLUXDB_KETTLE_SENSOR_LABELS', '0=Kettle,1=Ambient')
KETTLE_SENSOR_LABELS = dict(
    pair.split('=', 1) for pair in _sensor_label_raw.split(',') if '=' in pair
)

# -- iSpindel (fermentation) schema -------------------------------------------
ISPINDEL_MEAS    = _env.get('INFLUXDB_ISPINDEL_MEASUREMENT',  'ispindel')
ISPINDEL_SOURCE  = _env.get('INFLUXDB_ISPINDEL_SOURCE_TAG',   'source')
ISPINDEL_TYPE    = _env.get('INFLUXDB_ISPINDEL_TYPE_TAG',     'field')
ISPINDEL_VALUE   = _env.get('INFLUXDB_ISPINDEL_VALUE_FIELD',  'value')
ISPINDEL_TEMP    = _env.get('INFLUXDB_ISPINDEL_TEMP_VALUE',   'temperature')
ISPINDEL_GRAVITY = _env.get('INFLUXDB_ISPINDEL_GRAVITY_VALUE','gravity')


def _window_str(start_dt, end_dt, target_points=200):
    """Flux duration string yielding ~target_points over the given range."""
    secs = max(60, int((end_dt - start_dt).total_seconds() / target_points))
    if secs < 3600:
        return f'{max(1, secs // 60)}m'
    if secs < 86400:
        return f'{max(1, secs // 3600)}h'
    return f'{max(1, secs // 86400)}d'


def _parse_dt(s):
    """Parse ISO datetime string → UTC-aware datetime. None when unparseable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _rfc3339(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _topic_filter():
    """Flux OR filter expression for configured kettle topics."""
    clauses = ' or\n                       '.join(
        f'r["{KETTLE_TOPIC_TAG}"] == "{t}"' for t in KETTLE_TOPICS
    )
    return clauses


def _query_brew_day(query_api, start_dt):
    """Kettle temperature for the brew day.

    braudatum in KBH2 is recorded when the user saves the brew, often hours
    after brewing finished. Truncate to calendar-day midnight so the query
    always covers the full brew day regardless of save time.
    """
    # Start 1 day before the calendar day: KBH2 records braudatum when the user
    # saves the entry, which can be the day after the actual brew session.
    day_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    stop      = day_start + timedelta(hours=60)   # covers 2.5 days total
    window    = _window_str(day_start, stop, target_points=200)
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {_rfc3339(day_start)}, stop: {_rfc3339(stop)})
  |> filter(fn: (r) => r["_measurement"] == "{KETTLE_MEAS}")
  |> filter(fn: (r) => {_topic_filter()})
  |> filter(fn: (r) => r["_field"] == "{KETTLE_FIELD}")
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value", "{KETTLE_TOPIC_TAG}"])
'''
    rows = []
    for table in query_api.query(flux):
        for rec in table.records:
            topic  = rec.values.get(KETTLE_TOPIC_TAG, '')
            sensor = topic.split('/')[-1]
            rows.append({
                't':      _rfc3339(rec.get_time().astimezone(timezone.utc)),
                'v':      round(float(rec.get_value()), 2),
                'sensor': sensor,
                'label':  KETTLE_SENSOR_LABELS.get(sensor, f'Sensor {sensor}'),
            })
    rows.sort(key=lambda r: (r['t'], r['sensor']))
    return rows


def _query_ispindel(query_api, start_dt, stop_dt, reading):
    """iSpindel readings (temperature or gravity) over the fermentation window."""
    window   = _window_str(start_dt, stop_dt, target_points=300)
    type_val = ISPINDEL_TEMP if reading == 'temperature' else ISPINDEL_GRAVITY
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {_rfc3339(start_dt)}, stop: {_rfc3339(stop_dt)})
  |> filter(fn: (r) => r["_measurement"] == "{ISPINDEL_MEAS}")
  |> filter(fn: (r) => r["{ISPINDEL_TYPE}"] == "{type_val}")
  |> filter(fn: (r) => r["_field"] == "{ISPINDEL_VALUE}")
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value", "{ISPINDEL_SOURCE}"])
'''
    rows = []
    for table in query_api.query(flux):
        for rec in table.records:
            rows.append({
                't':      _rfc3339(rec.get_time().astimezone(timezone.utc)),
                'v':      round(float(rec.get_value()), 4),
                'source': rec.values.get(ISPINDEL_SOURCE, ''),
            })
    rows.sort(key=lambda r: (r['t'], r['source']))
    return rows


def _filter_by_og(rows, start_dt, expected_og, grace_hours=48, tolerance=4.0):
    """Exclude iSpindel sources whose initial gravity doesn't match this brew's OG.

    An iSpindel already mid-ferment on a previous brew shows much lower gravity than
    fresh wort at the start of this window. Sources with no reading in the first
    grace_hours are also excluded (they started too late — a different brew).
    Skipped when OG is unknown or only one source is present.
    """
    sources = {r['source'] for r in rows}
    if not expected_og or expected_og <= 0 or len(sources) <= 1:
        return rows, set()

    cutoff = start_dt + timedelta(hours=grace_hours)
    first_val = {}
    for r in rows:
        src = r['source']
        t   = datetime.fromisoformat(r['t'].replace('Z', '+00:00'))
        if t <= cutoff and src not in first_val:
            first_val[src] = r['v']

    exclude = set()
    for src in sources:
        if src not in first_val:
            exclude.add(src)                          # no early reading → started later
        elif first_val[src] < expected_og - tolerance:
            exclude.add(src)                          # gravity already too low for this brew

    return [r for r in rows if r['source'] not in exclude], exclude


def fetch_brew(query_api, snum):
    bj_path = os.path.join(DATA_DIR, f'{snum}_beerjson.json')
    if not os.path.exists(bj_path):
        print(f'  #{snum}: BeerJSON not found, skipping')
        return False

    try:
        with open(bj_path, encoding='utf-8') as f:
            recipe = json.load(f)['beerjson']['recipes'][0]
    except (json.JSONDecodeError, KeyError, IndexError, OSError) as exc:
        print(f'  #{snum}: cannot read {os.path.basename(bj_path)} ({exc}), skipping')
        return False

    sb           = recipe.get('_brewery', {})
    braudatum    = sb.get('braudatum')
    abfuelldatum = sb.get('abfuelldatum')

    if not braudatum:
        print(f'  #{snum}: no braudatum, skipping')
        return False

    expected_og  = (recipe.get('original_gravity') or {}).get('value') or 0

    start_dt = _parse_dt(braudatum)
    if start_dt is None:
        print(f'  #{snum}: unparseable braudatum {braudatum!r}, skipping')
        return False
    now      = datetime.now(timezone.utc)
    abf_dt   = _parse_dt(abfuelldatum) if abfuelldatum else None
    # Cap at 90 days when no (usable) abfuelldatum — prevents old brews with no end
    # date from capturing years of subsequent iSpindel activity.
    stop_dt  = (min(abf_dt + timedelta(days=2), now) if abf_dt
                else min(start_dt + timedelta(days=90), now))
    # A data-entry error can put Abfuelldatum before Braudatum → empty range.
    stop_dt  = max(stop_dt, start_dt + timedelta(days=1))

    print(f'  #{snum}: {recipe.get("name", "?")} '
          f'({braudatum[:10]} -> {str(stop_dt)[:10]})')

    brew_day     = _query_brew_day(query_api, start_dt)
    ferm_gravity = _query_ispindel(query_api, start_dt, stop_dt, 'gravity')
    ferm_temp    = _query_ispindel(query_api, start_dt, stop_dt, 'temperature')

    ferm_gravity, excl = _filter_by_og(ferm_gravity, start_dt, expected_og)
    if excl:
        print(f'    excluding iSpindels (OG mismatch): {sorted(excl)}')
    ferm_temp = [r for r in ferm_temp if r['source'] not in excl]

    out = {
        'sudnummer':  snum,
        'fetched_at': _rfc3339(now),
        'units': {
            'brew_day':     {'v': '°C'},
            'ferm_temp':    {'v': '°C'},
            'ferm_gravity': {'v': '°P'},
        },
        'brew_day':     brew_day,
        'ferm_temp':    ferm_temp,
        'ferm_gravity': ferm_gravity,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f'{snum}.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))

    print(f'    brew day: {len(brew_day):3d} pts  '
          f'ferm temp: {len(ferm_temp):3d} pts  '
          f'gravity: {len(ferm_gravity):3d} pts  '
          f'-> {os.path.basename(out_path)}')
    return True


def main():
    if not HAS_INFLUXDB:
        print('Error: influxdb-client not installed.')
        print('       Run: pip install influxdb-client')
        sys.exit(1)

    if not INFLUXDB_URL or not INFLUXDB_TOKEN:
        print('Error: INFLUXDB_URL and INFLUXDB_TOKEN must be set in .env')
        sys.exit(1)

    snums = []
    for a in sys.argv[1:]:
        try:
            snums.append(int(a))
        except ValueError:
            print(f'Error: invalid brew number {a!r}. Usage: python fetch_influxdb.py [n ...]')
            sys.exit(1)
    if not snums:
        idx_path = os.path.join(DATA_DIR, 'index.json')
        if not os.path.exists(idx_path):
            print('Error: web/data/index.json missing. Run: python export.py')
            sys.exit(1)
        with open(idx_path, encoding='utf-8') as f:
            snums = [b['sudnummer'] for b in json.load(f)['beers']]

    print(f'Fetching InfluxDB data for {len(snums)} brew(s) from {INFLUXDB_URL}')
    print(f'Bucket: {INFLUXDB_BUCKET}  Org: "{INFLUXDB_ORG}"')
    print(f'Kettle: {KETTLE_MEAS} / topics: {KETTLE_TOPICS}')
    print(f'iSpindel: {ISPINDEL_MEAS} / source tag: {ISPINDEL_SOURCE}')
    print()

    with InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG) as client:
        query_api = client.query_api()
        ok = sum(1 for s in snums if fetch_brew(query_api, s))

    print(f'\nDone: {ok}/{len(snums)} written to input/influxdb/')


if __name__ == '__main__':
    main()
