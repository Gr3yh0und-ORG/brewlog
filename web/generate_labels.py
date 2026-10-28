"""
KBH2 – Label Generator
Reads web/data/{n}_beerjson.json -> writes web/labels/{n}_label.svg + {n}_a4.svg

Usage:
    python generate_labels.py          # all brews
    python generate_labels.py 31       # single brew
    python generate_labels.py 31 32 45 # multiple brews

Optional:
    pip install qrcode   # for real QR codes (recommended)

Print tip:
    Print the A4 SVG at actual size (no "fit to page").
    Each label = 210mm x 33mm, 9 labels per A4.
"""
import json, math, os, re, sys, base64, html as htmllib

# -- Optional: QR code library ------------------------------------------------
try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False

from utils import load_env

ROOT      = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_env         = load_env()
BREWERY_NAME = _env.get('BREWERY_NAME', '')
SITE_URL     = _env.get('SITE_URL', '')
LOGO_SVG     = _env.get('LOGO_SVG', '')

DATA_DIR  = os.path.join(os.path.dirname(__file__), 'data')
OUT_DIR   = os.path.join(os.path.dirname(__file__), 'labels')
LOGO_PATH = os.path.join(ROOT, 'input', 'logo', LOGO_SVG)

# -- Label dimensions (coordinate system) -------------------------------------
# 1152x165 px ~ 210mm x 30mm at 300dpi
LW   = 1152
LH   = 165
CX   = LW // 2   # 576
CY   = LH // 2   # 82

# -- Radar chart --------------------------------------------------------------
R_MAX   = 66
R_WHITE = 79
N_AXES  = 8
AXES    = ['Bitterkeit', 'Frucht', 'Milde', 'Erde', 'Würze',
           'Stammwürze', 'Alkohol', 'Farbe']
ANGLES  = [i * 2 * math.pi / N_AXES - math.pi / 2 for i in range(N_AXES)]

# -- DIN A4 layout ------------------------------------------------------------
N_PER_A4       = 9
LABEL_W_MM     = 210
LABEL_H_MM     = 33
SHEET_H_COORDS = LH * N_PER_A4

# -- EBC -> hex color ---------------------------------------------------------
_SRM = ['#FFE699','#FFD878','#FFCA5A','#FFBF42','#FBB123',
        '#F8A600','#F39C00','#EA8F00','#E58500','#DE7C00',
        '#D77200','#CF6900','#CB6200','#C35900','#BB5100',
        '#B54C00','#B04500','#A63E00','#A13700','#9B3200',
        '#952D00','#8E2900','#882300','#821E00','#7B1A00',
        '#771900','#701400','#6A0E00','#651100','#5E0B00',
        '#5A0A02','#600903','#520907','#4C0505','#470606',
        '#440607','#3F0708','#3B0607','#3A070B','#36080A']

def ebc_to_hex(ebc):
    if not ebc or ebc <= 0: return '#FFE699'
    return _SRM[min(39, max(0, round(ebc / 1.97) - 1))]


# -- Color helpers ------------------------------------------------------------
def hex_to_rgb(h):
    h = h.lstrip('#')
    if len(h) == 3: h = h[0]*2 + h[1]*2 + h[2]*2
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

# -- Lab color interpolation (matches D3's interpolateLab + darker/brighter) --
def _rgb_to_lab(r, g, b):
    def lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    rl, gl, bl = lin(r), lin(g), lin(b)
    x = rl*0.4124564 + gl*0.3575761 + bl*0.1804375
    y = rl*0.2126729 + gl*0.7151522 + bl*0.0721750
    z = rl*0.0193339 + gl*0.1191920 + bl*0.9503041
    def f(t): return t**(1/3) if t > 0.008856 else 7.787*t + 16/116
    L = 116*f(y)       - 16
    a = 500*(f(x/0.95047) - f(y))
    b = 200*(f(y)       - f(z/1.08883))
    return L, a, b

def _lab_to_hex(L, a, b):
    def finv(t): return t**3 if t > 0.206897 else (t - 16/116) / 7.787
    fy = (L + 16) / 116
    x = finv(a/500 + fy) * 0.95047
    y = finv(fy)
    z = finv(fy - b/200) * 1.08883
    def delin(c):
        c = max(0.0, min(1.0, c))
        return 12.92*c if c <= 0.0031308 else 1.055*c**(1/2.4) - 0.055
    rl =  3.2404542*x - 1.5371385*y - 0.4985314*z
    gl = -0.9692660*x + 1.8760108*y + 0.0415560*z
    bl =  0.0556434*x - 0.2040259*y + 1.0572252*z
    r, g, b_ = (int(delin(c)*255 + 0.5) for c in (rl, gl, bl))
    return f'#{r:02x}{g:02x}{b_:02x}'

def d3_darker(hex_col, k=1):
    """D3 rgb.darker(k): multiply each channel by 0.7^k."""
    f = 0.7 ** k
    r, g, b = hex_to_rgb(hex_col)
    return f'#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}'

def d3_brighter(hex_col, k=1):
    """D3 rgb.brighter(k): divide each channel by 0.7^k, clamp to 255."""
    f = (1/0.7) ** k
    r, g, b = hex_to_rgb(hex_col)
    return f'#{min(255,int(r*f)):02x}{min(255,int(g*f)):02x}{min(255,int(b*f)):02x}'

def interpolate_lab(hex1, hex2, t):
    """D3 interpolateLab: interpolate two colors in CIE Lab space."""
    L1, a1, b1 = _rgb_to_lab(*hex_to_rgb(hex1))
    L2, a2, b2 = _rgb_to_lab(*hex_to_rgb(hex2))
    return _lab_to_hex(L1+(L2-L1)*t, a1+(a2-a1)*t, b1+(b2-b1)*t)

def beer_color_fct(beer_hex, d, length):
    """Replicates: d3.interpolateLab(beer.darker(5), beer.brighter(3))(d/length)."""
    dark   = d3_darker(beer_hex, 5)
    bright = d3_brighter(beer_hex, 3)
    return interpolate_lab(dark, bright, d / length)


def text_color(bg_hex):
    r, g, b = hex_to_rgb(bg_hex)
    luma = (0.299*r + 0.587*g + 0.114*b) / 255
    if luma > 0.5:
        f = 0.25
        return f'rgb({int(r*f)},{int(g*f)},{int(b*f)})'
    return 'rgb(240,230,215)'

def accent_color(bg_hex):
    r, g, b = hex_to_rgb(bg_hex)
    luma = (0.299*r + 0.587*g + 0.114*b) / 255
    if luma > 0.5:
        return 'rgb(140,90,10)'
    return 'rgb(220,170,80)'

def esc(s):
    return htmllib.escape(str(s)) if s else ''

def fmt_date(dt_str):
    if not dt_str: return ''
    try:
        from datetime import datetime
        return datetime.fromisoformat(dt_str.replace('Z','')).strftime('%d.%m.%Y')
    except Exception:
        return str(dt_str)[:10]


# -- QR code generation -------------------------------------------------------
def make_qr(url, ox, oy, target_px):
    if not HAS_QR:
        s = target_px
        svg = (f'<rect x="{ox}" y="{oy}" width="{s:.0f}" height="{s:.0f}" '
               f'fill="white" stroke="#bbb" stroke-width="1.5" rx="2"/>'
               f'<text x="{ox+s/2:.0f}" y="{oy+s/2:.0f}" font-size="7" '
               f'font-family="monospace" text-anchor="middle" '
               f'dominant-baseline="middle" fill="#aaa">QR</text>')
        return svg, s

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    cell = target_px / n

    rects = [
        f'<rect x="{ox + c*cell:.2f}" y="{oy + r*cell:.2f}" '
        f'width="{cell:.2f}" height="{cell:.2f}" fill="black"/>'
        for r, row in enumerate(matrix)
        for c, v in enumerate(row) if v
    ]
    return '\n'.join(rects), n * cell


# -- Radar values (8 axes) ----------------------------------------------------
def radar_values(recipe, sb):
    taste_map = {t['notes']: float(t['rating'])
                 for t in (sb.get('tastes') or [])}
    sw  = (recipe.get('original_gravity')  or {}).get('value') or 0
    abv = (recipe.get('alcohol_by_volume') or {}).get('value') or 0
    ebc = sb.get('erg_farbe') or (recipe.get('color_estimate') or {}).get('value') or 0
    return [
        taste_map.get('Bitterkeit', 0.5),
        taste_map.get('Frucht',     0.5),
        taste_map.get('Milde',      0.5),
        taste_map.get('Erde',       0.5),
        taste_map.get('Würze', 0.5),
        min(1.0, sw  / 20.0),
        min(1.0, abv / 8.0),
        min(1.0, ebc / 80.0),
    ]


# -- Catmull-Rom closed spline -> SVG path ------------------------------------
def catmull_rom_path(pts):
    n = len(pts)
    segs = []
    for i in range(n):
        p0 = pts[(i - 1) % n]
        p1 = pts[i]
        p2 = pts[(i + 1) % n]
        p3 = pts[(i + 2) % n]
        cp1x = p1[0] + (p2[0] - p0[0]) / 6
        cp1y = p1[1] + (p2[1] - p0[1]) / 6
        cp2x = p2[0] - (p3[0] - p1[0]) / 6
        cp2y = p2[1] - (p3[1] - p1[1]) / 6
        segs.append(
            f'C {cp1x:.1f} {cp1y:.1f}, {cp2x:.1f} {cp2y:.1f}, '
            f'{p2[0]:.1f} {p2[1]:.1f}'
        )
    return f'M {pts[0][0]:.1f} {pts[0][1]:.1f} {" ".join(segs)} Z'


# -- Label SVG content --------------------------------------------------------
_HEX_COLOR = re.compile(r'#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')

def draw_label(recipe, sb, logo_b64=None):
    bg       = sb.get('label_color') or '#D2CECE'
    if not isinstance(bg, str) or not _HEX_COLOR.fullmatch(bg):
        print(f'  #{sb.get("sudnummer", "?")}: invalid label_color {bg!r}, '
              f"using default '#D2CECE'")
        bg = '#D2CECE'
    mid_bg   = '#F5F1EB'            # parchment for the middle area
    txt_col  = text_color(mid_bg)   # dark text on light middle
    acc_col  = accent_color(bg)     # warm gold accent from label color
    name     = recipe.get('name', '?')
    style    = (recipe.get('style') or {}).get('name', '')
    snum     = sb.get('sudnummer', '?')
    sw       = (recipe.get('original_gravity')  or {}).get('value') or 0
    abv      = (recipe.get('alcohol_by_volume') or {}).get('value') or 0
    ebc      = sb.get('erg_farbe') or (recipe.get('color_estimate') or {}).get('value') or 0
    ibu      = (recipe.get('ibu_estimate') or {}).get('value') or 0
    abv_str  = f"{abv:.1f} %" if abv > 0.5 else '-'
    date_str = fmt_date(sb.get('abfuelldatum') or sb.get('braudatum'))
    beer_hex = ebc_to_hex(ebc)

    # Dynamic font sizes based on name length
    name_fs  = max(18, min(38, int(LW / max(len(name), 10) / 2.4)))
    style_fs = max(12, min(22, int(name_fs * 0.64)))
    sub_fs   = max(9,  min(14, int(name_fs * 0.48)))
    abv_fs   = 21

    # -- Two-series radar (matches Observable notebook visData) ---------------
    # Series 1: taste profile on axes 0-4, zeros on physical axes 5-7
    # Series 2: zeros on taste axes 0-4, physical values on axes 5-7
    # With zero-padding, each series forms a separate shape on its own axes.
    vals = radar_values(recipe, sb)
    taste_vals = vals[:5] + [0.0, 0.0, 0.0]
    phys_vals  = [0.0, 0.0, 0.0, 0.0, 0.0] + vals[5:]

    def make_pts(v_list):
        return [
            (CX + max(0.0, v) * R_MAX * math.cos(a),
             CY + max(0.0, v) * R_MAX * math.sin(a))
            for v, a in zip(v_list, ANGLES)
        ]

    taste_pts = make_pts(taste_vals)
    phys_pts  = make_pts(phys_vals)
    full_pts  = make_pts(vals)

    rings_svg = '\n'.join(
        f'<circle cx="{CX}" cy="{CY}" r="{R_MAX*i/3:.1f}" '
        f'fill="none" stroke="rgba(0,0,0,0.18)" stroke-width="0.7"/>'
        for i in range(3, 0, -1)
    )
    spokes_svg = '\n'.join(
        f'<line x1="{CX}" y1="{CY}" x2="{CX+R_MAX*math.cos(a):.1f}" '
        f'y2="{CY+R_MAX*math.sin(a):.1f}" stroke="rgba(0,0,0,0.12)" stroke-width="0.6"/>'
        for a in ANGLES
    )

    axis_labels = []
    for label, angle in zip(AXES, ANGLES):
        lx = CX + (R_MAX + 10) * math.cos(angle)
        ly = CY + (R_MAX + 10) * math.sin(angle)
        anchor = 'middle'
        if lx > CX + 4:   anchor = 'start'
        elif lx < CX - 4: anchor = 'end'
        axis_labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="7" font-family="KaiTi,serif" '
            f'text-anchor="{anchor}" dominant-baseline="middle" '
            f'fill="{txt_col}" opacity="0.75">{esc(label)}</text>'
        )

    taste_path = catmull_rom_path(taste_pts)
    phys_path  = catmull_rom_path(phys_pts)

    # Colors: Lab interpolation from beer.darker(5) to beer.brighter(3),
    # matching the Observable notebook's colorFct exactly.
    # data.length+2 = 4: series 1 → t=1/4, series 2 → t=2/4
    # axesDomain.length+1 = 9: dot i → t=(i+1)/9
    series1_col = beer_color_fct(beer_hex, 1, 4)
    series2_col = beer_color_fct(beer_hex, 2, 4)
    dot_colors  = [beer_color_fct(beer_hex, i+1, 9) for i in range(8)]

    dots_svg = '\n'.join(
        f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="2.2" '
        f'fill="{dot_colors[i]}" fill-opacity="0.5"/>'
        for i, p in enumerate(full_pts)
    )

    # -- QR codes -------------------------------------------------------------
    qr_size     = LH * 0.62
    untappd_id  = sb.get('untappd_id', '')
    beer_url    = f'{SITE_URL}?beer={snum}'
    untappd_url = (f'https://untappd.com/qr/beer/{untappd_id}'
                   if untappd_id else 'https://untappd.com')

    qr1_x = 68
    qr2_x = int(qr1_x + qr_size + 14)
    qr_y  = CY - qr_size / 2

    qr1_svg, qr1_sz = make_qr(beer_url,    qr1_x, qr_y, qr_size)
    qr2_svg, qr2_sz = make_qr(untappd_url, qr2_x, qr_y, qr_size)

    # -- Logo: large, opaque, right side of parchment area ----------------------
    STRIPE_W = int(LH * 0.32)   # ~53px color stripe on each end
    logo_el  = ''
    if logo_b64:
        logo_w = int(LH * 1.0)    # ~165px
        lx = LW - STRIPE_W - logo_w - 12
        ly = CY - logo_w // 2 + 6
        logo_el = (
            f'<image href="data:image/svg+xml;base64,{logo_b64}" '
            f'x="{lx}" y="{ly}" width="{logo_w}" height="{logo_w}" '
            f'opacity="1.0"/>'
        )

    # -- Text layout: top-to-bottom stacking, no overlap ----------------------
    tx = CX + R_WHITE + 28
    block_h  = name_fs + style_fs + abv_fs + sub_fs + 4 + 12 + 6
    y_start  = max(10, CY - block_h / 2)
    ty_name  = y_start  + name_fs
    ty_style = ty_name  + style_fs + 4
    ty_abv   = ty_style + abv_fs   + 12
    ty_date  = ty_abv   + sub_fs   + 6

    parts = [
        # Parchment middle
        f'<rect width="{LW}" height="{LH}" fill="{mid_bg}"/>',

        # Color stripes on left and right ends (EBC beer color)
        f'<rect x="0" y="0" width="{STRIPE_W}" height="{LH}" fill="{esc(beer_hex)}"/>',
        f'<rect x="{LW-STRIPE_W}" y="0" width="{STRIPE_W}" height="{LH}" fill="{esc(beer_hex)}"/>',

        # White radar background circle
        f'<circle cx="{CX}" cy="{CY}" r="{R_WHITE}" fill="white" opacity="0.90"/>',

        rings_svg,
        spokes_svg,

        # Series 1: taste profile
        f'<path d="{taste_path}" fill="{esc(series1_col)}" fill-opacity="0.5" '
        f'stroke="{esc(series1_col)}" stroke-width="2"/>',

        # Series 2: physical measurements (dashed outline)
        f'<path d="{phys_path}" fill="{esc(series2_col)}" fill-opacity="0.5" '
        f'stroke="{esc(series2_col)}" stroke-width="2" stroke-dasharray="3,2"/>',

        # Inferno-colored dots at each data point
        dots_svg,

        '\n'.join(axis_labels),

        # QR code 1 – beer detail page
        '<g>',
        qr1_svg,
        f'<text x="{qr1_x + qr1_sz/2:.0f}" y="{qr_y - 4:.0f}" '
        f'font-size="6" font-family="monospace" text-anchor="middle" '
        f'fill="{txt_col}" opacity="0.8">beer details</text>',
        '</g>',

        # QR code 2 (Untappd)
        '<g>',
        qr2_svg,
        f'<text x="{qr2_x + qr2_sz/2:.0f}" y="{qr_y - 4:.0f}" '
        f'font-size="6" font-family="monospace" text-anchor="middle" '
        f'fill="{txt_col}" opacity="0.8">untappd</text>',
        '</g>',


        # Beer name
        f'<text x="{tx}" y="{ty_name:.0f}" '
        f'font-size="{name_fs}" font-family="KaiTi,serif" font-weight="bold" '
        f'font-variant="small-caps" fill="{txt_col}">{esc(name)}</text>',

        # Beer style
        f'<text x="{tx}" y="{ty_style:.0f}" '
        f'font-size="{style_fs}" font-family="KaiTi,serif" '
        f'fill="{esc(beer_hex)}">{esc(style)}</text>',

        # Date
        f'<text x="{tx}" y="{ty_date:.0f}" '
        f'font-size="{sub_fs}" font-family="KaiTi,serif" '
        f'fill="{txt_col}" opacity="0.6">{esc(date_str)}</text>',

        # Combined ABV · IBU · EBC line
        f'<text x="{tx}" y="{ty_abv:.0f}" font-family="KaiTi,serif" '
        f'font-size="{abv_fs}" font-weight="bold" font-variant="small-caps" fill="{txt_col}">'
        f'{esc(abv_str)}  ·  {ibu:.0f} IBU  ·  {ebc:.0f} EBC'
        f'</text>',

        # Cut lines
        f'<line x1="0" y1="0" x2="{LW}" y2="0" stroke="#ccc" stroke-width="0.6"/>',
        f'<line x1="0" y1="{LH}" x2="{LW}" y2="{LH}" stroke="#ccc" stroke-width="0.6"/>',

        # Logo: foreground, bottom-right corner
        logo_el,
    ]

    return '\n'.join(p for p in parts if p)


def svg_wrapper(content, w_mm, h_mm, vb_w, vb_h, comment=''):
    # XML comments may not contain "--"; neutralise it so a beer name can't
    # produce malformed SVG.
    comment = comment.replace('--', '-')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + (f'<!-- {comment} -->\n' if comment else '')
        + f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{w_mm}mm" height="{h_mm}mm" '
        f'viewBox="0 0 {vb_w} {vb_h}">\n'
        + content
        + '\n</svg>'
    )


def generate_label(snum, logo_b64=None):
    fname = os.path.join(DATA_DIR, f'{snum}_beerjson.json')
    if not os.path.exists(fname):
        print(f'  Not found: {fname}')
        return False

    try:
        with open(fname, encoding='utf-8') as f:
            bj = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f'  #{snum}: cannot read {os.path.basename(fname)} ({exc}), skipping')
        return False

    recipe = bj['beerjson']['recipes'][0]
    sb     = recipe.get('_brewery', {})

    label_body = draw_label(recipe, sb, logo_b64)

    svg_single = svg_wrapper(
        label_body,
        LABEL_W_MM, LABEL_H_MM,
        LW, LH,
        f'{BREWERY_NAME} Label - {recipe.get("name")}',
    )

    stacked = '\n'.join(
        f'<g transform="translate(0,{i*LH})">{label_body}</g>'
        for i in range(N_PER_A4)
    )
    svg_a4 = svg_wrapper(
        stacked,
        LABEL_W_MM, LABEL_H_MM * N_PER_A4,
        LW, SHEET_H_COORDS,
        f'{BREWERY_NAME} - {N_PER_A4} labels per A4 - {recipe.get("name")}',
    )

    os.makedirs(OUT_DIR, exist_ok=True)
    out_single = os.path.join(OUT_DIR, f'{snum}_label.svg')
    out_a4     = os.path.join(OUT_DIR, f'{snum}_a4.svg')

    with open(out_single, 'w', encoding='utf-8') as f: f.write(svg_single)
    with open(out_a4,     'w', encoding='utf-8') as f: f.write(svg_a4)

    name_short = (recipe.get('name') or '')[:45]
    print(f'  #{snum:<3} {name_short:<45} -> {os.path.basename(out_single)}')
    return True


def main():
    snums = []
    for a in sys.argv[1:]:
        try:
            snums.append(int(a))
        except ValueError:
            print(f'Error: invalid brew number {a!r}. Usage: python generate_labels.py [n ...]')
            sys.exit(1)
    if not snums:
        idx_path = os.path.join(DATA_DIR, 'index.json')
        if not os.path.exists(idx_path):
            print('Error: web/data/index.json missing. Run: python export.py')
            sys.exit(1)
        with open(idx_path, encoding='utf-8') as f:
            idx = json.load(f)
        snums = [b['sudnummer'] for b in idx['beers']]

    logo_b64 = None
    if os.path.exists(LOGO_PATH):
        with open(LOGO_PATH, 'rb') as f:
            logo_b64 = base64.b64encode(f.read()).decode()
    else:
        print(f'Note: logo not found ({LOGO_PATH})')

    print(f'Generating labels for {len(snums)} brews...')
    if not HAS_QR:
        print('  Note: install "pip install qrcode" for real QR codes.')

    ok = sum(1 for s in snums if generate_label(s, logo_b64))
    print(f'\nDone: {ok}/{len(snums)} labels in web/labels/')
    print(f'Print tip: print *_a4.svg at actual size (no "fit to page").')


if __name__ == '__main__':
    main()
