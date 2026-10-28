"""
KBH2 – BeerJSON + BeerXML Export
Reads input/brauhelfer.sqlite + input/enrichment/{n}.json
Writes web/data/index.json + web/data/{sudnummer}_beerjson.json (BeerJSON 2.06)
Writes web/data/{sudnummer}_beerxml.xml (BeerXML)

Run: python export.py  (from the web/ folder)
Local test: python -m http.server 8080  (then: http://localhost:8080)
"""
import sqlite3, json, math, datetime, os, sys, shutil, re
import xml.etree.ElementTree as ET
from xml.dom import minidom
try:
    from PIL import Image
    _PIL = True
except ImportError:
    _PIL = False

from utils import load_env

ROOT      = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_env         = load_env()
BREWERY_NAME = _env.get('BREWERY_NAME', '')
SITE_URL     = _env.get('SITE_URL', '')
LOGO_PNG     = _env.get('LOGO_PNG', '')
LOGO_SVG     = _env.get('LOGO_SVG', '')

DB_PATH       = os.path.join(ROOT, 'input', 'brauhelfer.sqlite')
ENR_DIR       = os.path.join(ROOT, 'input', 'enrichment')
IMG_DIR       = os.path.join(ROOT, 'input', 'images')
INFLUXDB_DIR  = os.path.join(ROOT, 'input', 'influxdb')
LOGO_SRC      = os.path.join(ROOT, 'input', 'logo', LOGO_PNG)
LOGO_SVG_SRC  = os.path.join(ROOT, 'input', 'logo', LOGO_SVG)
DATA_DIR      = os.path.join(os.path.dirname(__file__), 'data')
IMG_OUT       = os.path.join(os.path.dirname(__file__), 'images')
LOGO_OUT      = os.path.join(os.path.dirname(__file__), 'logo')

STATUS_LABEL = {0: 'Rezept', 1: 'Gebraut', 2: 'Abgefüllt', 3: 'Ausgetrunken', 4: 'Verworfen'}
HOP_USE      = {1: 'add_to_first_wort', 2: 'add_to_boil', 3: 'add_to_boil',
                4: 'add_to_fermentation', 5: 'add_to_whirlpool'}
ZEITPUNKT_USE = {0: 'add_to_boil', 1: 'add_to_fermentation', 2: 'add_to_package'}
DEFAULT_TASTES = [
    {'rating': 0.5, 'notes': 'Bitterkeit'},
    {'rating': 0.5, 'notes': 'Frucht'},
    {'rating': 0.5, 'notes': 'Milde'},
    {'rating': 0.5, 'notes': 'Erde'},
    {'rating': 0.5, 'notes': 'Würze'},
]


def clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def clean_row(row):
    return {k: clean(v) for k, v in dict(row).items()}


def r1(v):
    return round(v, 1) if v is not None else 0.0


def tinseth_ibu(alpha_pct, grams, time_min, og_plato, volume_l):
    """Tinseth IBU for one hop addition (returns 0 for dry hops or missing data)."""
    if time_min <= 0 or volume_l <= 0 or grams <= 0 or alpha_pct <= 0:
        return 0.0
    sg          = 1.0 + og_plato / (258.6 - (og_plato / 258.2) * 227.1)
    bigness     = 1.65 * (0.000125 ** (sg - 1.0))
    time_factor = (1.0 - math.exp(-0.04 * time_min)) / 4.15
    return (alpha_pct / 100.0) * bigness * time_factor * grams * 1000.0 / volume_l


def calc_abv(sud):
    """Return ABV in % vol. Falls back to Vergaerungsgrad estimate when erg_Alkohol < 1.0."""
    abv = sud.get('erg_Alkohol') or 0.0
    if abv >= 1.0:
        return round(abv, 1)
    vg = sud.get('Vergaerungsgrad') or 0
    og = sud.get('SWJungbier') or sud.get('SW') or 0
    if vg >= 50 and og > 0:
        fg = og * (1 - vg / 100)
        abv = (og - fg) / (2.0665 - 0.010665 * og)
        return round(abv, 1)
    return round(abv, 1)


def _xml_text(tag, parent, text):
    """Create an XML element with text content."""
    el = ET.SubElement(parent, tag)
    el.text = str(text) if text is not None else ''
    return el


def _plato_to_sg(plato):
    """Convert Plato to specific gravity."""
    return 1.0 + plato / (258.6 - (plato / 258.2) * 227.1)


def _sg_to_plato(sg):
    """Convert specific gravity to Plato."""
    return (258.6 * (sg - 1.0)) / (1.0 - (sg - 1.0) / 227.1)


def to_beerxml(sud):
    """Convert one Sud dict into a BeerXML document."""
    root = ET.Element('recipe')
    root.set('xmlns', 'http://www.beerxml.org')
    root.set('version', '2')

    # Recipe header
    _xml_text('NAME', root, sud.get('Sudname', ''))
    _xml_text('VERSION', root, str(sud.get('Sudnummer', '')))
    _xml_text('CREATED', root, sud.get('Erstellt', ''))
    _xml_text('CATEGORY', root, sud.get('Kategorie', ''))
    _xml_text('BREWERY', root, BREWERY_NAME)

    # Batch size
    _xml_text('BATCHSIZE', root, r1(sud.get('Menge') or 0))
    _xml_text('BOILSIZE', root, r1(sud.get('Menge') or 0))
    _xml_text('BOILTIME', root, sud.get('Kochdauer') or 60)
    _xml_text('YEAST', root, 'ale')
    _xml_text('EFFICIENCY', root, sud.get('erg_Sudhausausbeute', 75))

    # Gravity / ABV
    og_plato = sud.get('SW') or 0
    og_sg = _plato_to_sg(og_plato)
    _xml_text('OG', root, f'{og_sg:.4f}')
    abv = calc_abv(sud)
    fg_sg = _plato_to_sg(og_plato - (abv * 2.0665 - abv * 0.010665 * og_plato) if og_plato > 0 else 0)
    _xml_text('FG', root, f'{fg_sg:.4f}')
    _xml_text('OGTEMP', root, '')
    _xml_text('FGTEMP', root, '')
    _xml_text('ATTENUATION', root, sud.get('Vergaerungsgrad', 0))
    _xml_text('IBU', root, round(sud.get('IBU') or 0))
    _xml_text('COLOR', root, round(sud.get('erg_Farbe') or 0))
    _xml_text('EBC', root, round(sud.get('erg_Farbe') or 0))
    _xml_text('ABV', root, abv)
    _xml_text('CARBONATION', root, sud.get('CO2') or 0)
    _xml_text('CARBONATION_TEMP', root, '')
    _xml_text('CARBONATION_METHOD', root, '')

    # Notes
    _xml_text('NOTES', root, sud.get('Kommentar') or '')

    # Ingredients
    ingredients = ET.SubElement(root, 'ingredients')

    # Fermentables
    for m in sud.get('malzschuettung', []):
        if not m.get('Name'):
            continue
        malt = ET.SubElement(ingredients, 'malt')
        _xml_text('NAME', malt, m['Name'])
        _xml_text('TYPE', malt, 'Grain')
        _xml_text('AMOUNT', malt, r1(m.get('erg_Menge') or 0))
        _xml_text('UNIT', malt, 'kg')
        if m.get('Farbe'):
            _xml_text('COLOR', malt, m['Farbe'])
            _xml_text('COLOR_UNIT', malt, 'EBC')
        if m.get('Potential'):
            _xml_text('YIELD', malt, round(m['Potential'] * 100, 1))

    # Hops
    kochdauer = sud.get('Kochdauer') or 60
    for h in sud.get('hopfengaben', []):
        if not h.get('Name'):
            continue
        hop = ET.SubElement(ingredients, 'hop')
        _xml_text('NAME', hop, h['Name'])
        _xml_text('AMOUNT', hop, r1(h.get('erg_Menge') or 0))
        _xml_text('UNIT', hop, 'g')
        _xml_text('TYPE', hop, 'Pellets' if h.get('Pellets') else 'Leaf')
        _xml_text('ALPHA', hop, h.get('Alpha') or 0)
        use = HOP_USE.get(h.get('Vorderwuerze', 3), 'add_to_boil')
        if use == 'add_to_fermentation':
            _xml_text('TIME', hop, 0)
            _xml_text('USE', hop, 'Aroma')
        elif use == 'add_to_first_wort':
            _xml_text('TIME', hop, kochdauer)
            _xml_text('USE', hop, 'Bitterness')
        else:
            _xml_text('TIME', hop, h.get('Zeit') or 0)
            _xml_text('USE', hop, 'Bitterness')

    # Yeast
    for y in sud.get('hefegaben', []):
        if not y.get('Name'):
            continue
        yeast = ET.SubElement(ingredients, 'yeast')
        _xml_text('NAME', yeast, y['Name'])
        _xml_text('AMOUNT', yeast, y.get('Menge') or 1)
        _xml_text('TYPE', yeast, 'Liquid')
        _xml_text('FORM', yeast, 'Powder')
        _xml_text('LAB', yeast, '')
        _xml_text('YEAST_AMOUNT_UNITS', yeast, 'g')

    # Misc
    for z in sud.get('weitereZutatenGaben', []):
        if not z.get('Name') or z.get('Typ') == 100:
            continue
        misc = ET.SubElement(ingredients, 'misc')
        _xml_text('NAME', misc, z['Name'])
        _xml_text('AMOUNT', misc, r1(z.get('Menge') or 0))
        _xml_text('UNIT', misc, 'g')
        _xml_text('TYPE', misc, 'Other')
        _xml_text('USE', misc, 'Boil')

    # Mash
    mash_steps = sud.get('maischplan', [])
    if mash_steps:
        mash = ET.SubElement(root, 'mash')
        _xml_text('NAME', mash, 'Maischplan')
        _xml_text('TYPE', mash, 'Infusion')
        _xml_text('TUN_TEMP', mash, '')
        _xml_text('TUN_EFFICIENCY', mash, '')
        _xml_text('TUN_SPECIFIC_HEAT', mash, '')
        for i, step in enumerate(mash_steps):
            ms = ET.SubElement(mash, 'step')
            _xml_text('NAME', ms, step.get('Name') or f'Schritt {i + 1}')
            _xml_text('TEMP', ms, step.get('TempRast') or 0)
            _xml_text('DURATION', ms, step.get('DauerRast') or 0)
            _xml_text('SPOUT', ms, 0)
            _xml_text('INFUSE', ms, 0)
            _xml_text('RAMP_RATE', ms, '')
            _xml_text('REST_DELTA', ms, '')
            _xml_text('RAMP_DELTA', ms, '')

    return root


def to_beerxml_string(sud):
    """Convert one Sud dict into a formatted BeerXML string."""
    root = to_beerxml(sud)
    rough = ET.tostring(root, encoding='unicode')
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent='  ', encoding=None)


def to_beerjson(sud, enr):
    """Convert one Sud dict (with sub-lists) into a BeerJSON 2.06 document."""

    # ── Fermentables ─────────────────────────────────────────────────
    fermentables = []
    for m in sud['malzschuettung']:
        if not m.get('Name'):
            continue
        fa = {
            'name': m['Name'],
            'type': 'grain',
            'amount': {'unit': 'kg', 'value': r1(m.get('erg_Menge') or 0)},
        }
        if m.get('Farbe') is not None:
            fa['color'] = {'unit': 'EBC', 'value': m['Farbe']}
        if m.get('Potential'):
            fa['yield'] = {'fine_grind': {'unit': '%', 'value': round(m['Potential'] * 100, 1)}}
        fermentables.append(fa)

    # ── Hop additions ─────────────────────────────────────────────────
    og_plato  = sud.get('SW') or 12.0
    volume_l  = sud.get('Menge') or 20.0
    kochdauer = sud.get('Kochdauer') or 60

    hops = []
    for h in sud['hopfengaben']:
        if not h.get('Name'):
            continue
        use = HOP_USE.get(h.get('Vorderwuerze', 3), 'add_to_boil')
        if use == 'add_to_fermentation':
            hop_min = 0
        elif use == 'add_to_first_wort':
            hop_min = kochdauer
        else:
            hop_min = h.get('Zeit') or 0
        raw_ibu = tinseth_ibu(h.get('Alpha') or 0, h.get('erg_Menge') or 0, hop_min, og_plato, volume_l)
        ha = {
            'name': h['Name'],
            'alpha_acid': {'unit': '%', 'value': h.get('Alpha') or 0},
            'form': 'pellet' if h.get('Pellets') else 'leaf',
            'amount': {'unit': 'g', 'value': r1(h.get('erg_Menge') or 0)},
            'timing': {'use': use},
            '_ibu_raw': raw_ibu,
        }
        if use == 'add_to_fermentation':
            ha['timing']['duration'] = {'unit': 'day', 'value': h.get('Zeit') or 4}
        else:
            ha['timing']['time'] = {'unit': 'min', 'value': h.get('Zeit') or 0}
        hops.append(ha)

    # Scale per-hop IBUs so they sum to the stored total IBU
    total_ibu   = sud.get('IBU') or 0.0
    raw_sum     = sum(h['_ibu_raw'] for h in hops)
    scale       = (total_ibu / raw_sum) if raw_sum > 0 else 0.0
    for h in hops:
        h['ibu'] = round(h.pop('_ibu_raw') * scale, 1)

    # ── Culture (yeast) ───────────────────────────────────────────────
    yeasts = []
    for y in sud['hefegaben']:
        if not y.get('Name'):
            continue
        yeasts.append({
            'name': y['Name'],
            'type': 'ale',
            'form': 'dry',
            'amount': {'unit': 'pkg', 'value': y.get('Menge') or 1},
        })

    # ── Misc ingredients (not Typ=100 which are hop-derived) ─────────
    misc = []
    for z in sud['weitereZutatenGaben']:
        if not z.get('Name') or z.get('Typ') == 100:
            continue
        mi = {
            'name': z['Name'],
            'amount': {'unit': 'g', 'value': r1(z.get('Menge') or 0)},
            'timing': {'use': ZEITPUNKT_USE.get(z.get('Zeitpunkt', 0), 'add_to_boil')},
        }
        if z.get('Bemerkung'):
            mi['notes'] = z['Bemerkung']
        misc.append(mi)

    # ── Mash steps ────────────────────────────────────────────────────
    mash_steps = []
    for i, step in enumerate(sud['maischplan']):
        mash_steps.append({
            'name': step.get('Name') or f'Schritt {i + 1}',
            'type': 'temperature',
            'step_temperature': {'unit': 'C', 'value': step.get('TempRast') or 0},
            'step_time': {'unit': 'min', 'value': step.get('DauerRast') or 0},
        })

    # ── Fermentation log ──────────────────────────────────────────────
    gaerverlauf = [
        {
            'timestamp': g.get('Zeitstempel'),
            'gravity':   g.get('Restextrakt'),
            'temp':      g.get('Temp'),
            'note':      g.get('Bemerkung'),
        }
        for g in (sud.get('hauptgaerverlauf') or [])
    ]

    # ── brewery extension (non-BeerJSON data) ─────────────────────────
    NOTE_KEYS = [
        'Kommentar', 'BemerkungBrauen', 'BemerkungMaischplan',
        'BemerkungWasseraufbereitung', 'BemerkungZutatenMaischen',
        'BemerkungZutatenKochen', 'BemerkungZutatenGaerung',
        'BemerkungGaerung', 'BemerkungAbfuellen',
    ]
    extension = {
        'sudnummer':           sud['Sudnummer'],
        'status':              sud['Status'],
        'status_label':        STATUS_LABEL.get(sud['Status'], ''),
        'braudatum':           sud.get('Braudatum'),
        'abfuelldatum':        sud.get('Abfuelldatum'),
        'category':            sud.get('Kategorie'),
        'kochdauer':           sud.get('Kochdauer'),
        'erg_sudhausausbeute': sud.get('erg_Sudhausausbeute'),
        'erg_farbe':           sud.get('erg_Farbe'),
        'menge':               sud.get('Menge'),
        'notizen':             {k: sud[k] for k in NOTE_KEYS if sud.get(k)},
        'bewertungen':         sud.get('bewertungen') or [],
        'tags':                sud.get('tags') or [],
        'gaerverlauf':         gaerverlauf,
        # from enrichment.json
        'untappd_id':  enr.get('untappd_id', ''),
        'label_color': enr.get('label_color', '#D2CECE'),
        'tastes':      enr.get('tastes', DEFAULT_TASTES),
        'images':      enr.get('images', []),
    }

    return {
        'beerjson': {
            'version': 2.06,
            'recipes': [{
                'name':    sud['Sudname'],
                'id':      str(sud['Sudnummer']),
                'type':    'all grain',
                'author':  BREWERY_NAME,
                'created': sud.get('Erstellt', ''),
                'batch_size':        {'unit': 'l',     'value': r1(sud.get('Menge') or 0)},
                'original_gravity':  {'unit': 'plato', 'value': r1(sud.get('SW') or 0)},
                'alcohol_by_volume': {'unit': '%',     'value': calc_abv(sud)},
                'ibu_estimate':      {'method': 'Tinseth', 'unit': 'IBUs', 'value': round(sud.get('IBU') or 0)},
                'color_estimate':    {'unit': 'EBC',   'value': round(sud.get('erg_Farbe') or 0)},
                'carbonation':       sud.get('CO2') or 0,
                'style':  {'name': sud.get('Kategorie') or '', 'type': 'beer'},
                'notes':  sud.get('Kommentar') or '',
                'ingredients': {
                    'fermentable_additions': fermentables,
                    'hop_additions':         hops,
                    'culture_additions':     yeasts,
                    'miscellaneous_additions': misc,
                },
                'mash': {'name': 'Maischplan', 'mash_steps': mash_steps},
                'boil': {'boil_time': {'unit': 'min', 'value': sud.get('Kochdauer') or 0}},
                '_brewery': extension,
            }],
        }
    }


def load_sud(cur, sid, known_tables=None):
    """Load one Sud row plus all sub-tables into a dict."""
    cur.execute("SELECT * FROM Sud WHERE ID=?", (sid,))
    sud = clean_row(cur.fetchone())

    if known_tables is None:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        known_tables = {r['name'].lower() for r in cur.fetchall()}

    for table, key, order in [
        ('Malzschuettung',      'malzschuettung',      'Prozent DESC'),
        ('Hopfengaben',         'hopfengaben',          'Zeit DESC'),
        ('Hefegaben',           'hefegaben',            'ID'),
        ('WeitereZutatenGaben', 'weitereZutatenGaben',  'ID'),
        ('Maischplan',          'maischplan',           'ID'),
        ('Hauptgaerverlauf',    'hauptgaerverlauf',     'Zeitstempel'),
        ('Bewertungen',         'bewertungen',          'Datum'),
        ('Tags',                'tags',                 'ID'),
    ]:
        if table.lower() not in known_tables:
            print(f"Warning: table '{table}' missing from database, "
                  f"exporting '{key}' as empty")
            sud[key] = []
            continue
        cur.execute(f"SELECT * FROM {table} WHERE SudID=? ORDER BY {order}", (sid,))
        sud[key] = [clean_row(r) for r in cur.fetchall()]

    return sud


def export():
    if not os.path.exists(DB_PATH):
        print(f"Error: database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    # Load enrichment data (one file per Sudnummer in input/enrichment/)
    def load_enr(sudnummer):
        path = os.path.join(ENR_DIR, f'{sudnummer}.json')
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                print(f"Warning: cannot read enrichment file {path} ({exc}), ignoring")
        return {}

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(IMG_OUT, exist_ok=True)
    os.makedirs(LOGO_OUT, exist_ok=True)
    if os.path.isfile(LOGO_SRC):
        shutil.copy2(LOGO_SRC, os.path.join(LOGO_OUT, os.path.basename(LOGO_SRC)))
    # Copy SVG as ASCII-only filename to avoid URL encoding issues
    if LOGO_SVG_SRC and os.path.isfile(LOGO_SVG_SRC):
        shutil.copy2(LOGO_SVG_SRC, os.path.join(LOGO_OUT, 'logo.svg'))

    def _photo_sort_key(path, fname):
        """Sort key: EXIF DateTimeOriginal → numeric prefix in name → mtime."""
        if _PIL:
            try:
                with Image.open(path) as im:
                    exif = im._getexif() or {}
                    raw = exif.get(36867)  # DateTimeOriginal
                    if raw:
                        dt = datetime.datetime.strptime(raw, '%Y:%m:%d %H:%M:%S')
                        return (0, dt.timestamp(), 0)
            except Exception:
                pass
        # e.g. "18_01_Brauen.jpg" → numeric prefix 1
        m = re.match(r'^\d+_(\d+)_', fname)
        if m:
            return (1, 0.0, int(m.group(1)))
        return (2, os.path.getmtime(path), 0)

    # Scan input/images/ and build a map: sudnummer -> chronologically sorted filenames
    img_map = {}
    if os.path.isdir(IMG_DIR):
        entries = []
        for fname in os.listdir(IMG_DIR):
            m = re.match(r'^(\d+)_.+\.(jpe?g|png|webp|gif)$', fname, re.IGNORECASE)
            if m:
                full = os.path.join(IMG_DIR, fname)
                entries.append((int(m.group(1)), _photo_sort_key(full, fname), fname))
                shutil.copy2(full, os.path.join(IMG_OUT, fname))
        for num, _, fname in sorted(entries, key=lambda e: (e[0], e[1])):
            img_map.setdefault(num, []).append(fname)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    known_tables = {r['name'].lower() for r in cur.fetchall()}

    cur.execute("SELECT ID, Sudnummer, Sudname, Kategorie, Status, Braudatum, Abfuelldatum, "
                "Menge, SW, IBU, erg_Farbe, erg_Alkohol, erg_Sudhausausbeute "
                "FROM Sud ORDER BY Sudnummer DESC")
    rows = [clean_row(r) for r in cur.fetchall()]

    index_entries = []
    for row in rows:
        sid  = row['ID']
        snum = row['Sudnummer']
        if snum is None:
            print(f"Warning: Sud ID {sid} has no Sudnummer, skipping")
            continue

        sud = load_sud(cur, sid, known_tables)
        enr = load_enr(snum)
        enr.setdefault('images', img_map.get(snum, []))
        bj  = to_beerjson(sud, enr)

        # BeerJSON
        fname = f"{snum}_beerjson.json"
        with open(os.path.join(DATA_DIR, fname), 'w', encoding='utf-8') as f:
            json.dump(bj, f, ensure_ascii=False, indent=2)

        # BeerXML
        xml_fname = f"{snum}_beerxml.xml"
        with open(os.path.join(DATA_DIR, xml_fname), 'w', encoding='utf-8') as f:
            f.write(to_beerxml_string(sud))

        influxdb_src = os.path.join(INFLUXDB_DIR, f'{snum}.json')
        if os.path.exists(influxdb_src):
            shutil.copy2(influxdb_src, os.path.join(DATA_DIR, f'{snum}_influxdb.json'))

        index_entries.append({
            'sudnummer':   snum,
            'name':        row['Sudname'],
            'category':    row['Kategorie'],
            'status':      row['Status'],
            'braudatum':   row['Braudatum'],
            'abfuelldatum': row['Abfuelldatum'],
            'menge':       row['Menge'],
            'sw':          row['SW'],
            'ibu':         row['IBU'],
            'ebc':         row['erg_Farbe'],
            'alkohol':     calc_abv(sud),
            'sha':         round(row['erg_Sudhausausbeute'], 1) if row.get('erg_Sudhausausbeute') else None,
            'tastes':      enr.get('tastes', DEFAULT_TASTES),
            'file':        fname,
        })

    conn.close()

    index = {
    'config': {
        'brewery_name': BREWERY_NAME,
        'site_url':     SITE_URL,
        'logo_png':     LOGO_PNG,
        'logo_svg':     'logo.svg',
    },
        'exportiert': datetime.datetime.now().strftime('%d.%m.%Y %H:%M'),
        'beers': index_entries,
    }
    with open(os.path.join(DATA_DIR, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"OK: {len(rows)} brews exported -> web/data/")


if __name__ == '__main__':
    export()
