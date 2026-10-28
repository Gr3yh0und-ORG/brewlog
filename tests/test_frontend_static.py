"""Static and functional sanity checks for the web frontend.

Pins the findings of the 2026-10-28 code review:
- doctype, i18n file corruption / key parity (pure Python)
- noteHTML allowlist sanitizer, getStatus, radarVals, stars,
  year grouping, setLang error path (executed with node, skipped if absent)
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB   = os.path.join(REPO, 'web')
I18N  = os.path.join(WEB, 'i18n')
NODE  = shutil.which('node')


def read_index_html():
    with open(os.path.join(WEB, 'index.html'), encoding='utf-8') as fh:
        return fh.read()


def extract_function(html, name):
    """Return the full source of `function name(...) { ... }` (brace-matched)."""
    start = html.index('function %s(' % name)
    open_ = html.index('{', start)
    depth = 0
    for i in range(open_, len(html)):
        if html[i] == '{':
            depth += 1
        elif html[i] == '}':
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError('unbalanced braces extracting %s' % name)


def extract_line(html, pattern):
    m = re.search(pattern, html)
    assert m, 'pattern not found: %s' % pattern
    return m.group(0)


def run_node(script, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    fd, path = tempfile.mkstemp(suffix='.js')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(script)
        proc = subprocess.run([NODE, path], capture_output=True, text=True, env=env)
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise AssertionError('node failed (rc=%d):\n%s\n%s'
                             % (proc.returncode, proc.stdout, proc.stderr))
    return proc.stdout.strip()


# Minimal DOM shim: enough for noteHTML()'s template/fragment walk
# (template.innerHTML parse + innerHTML serialize), no dependencies.
DOM_SHIM = r"""
const Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
const VOID_TAGS = { BR: 1, IMG: 1 };
function _text(v) { return { nodeType: 3, nodeValue: v, cloneNode() { return _text(this.nodeValue); } }; }
function _el(tag) {
  return {
    nodeType: 1, tagName: String(tag).toUpperCase(), attrs: {}, childNodes: [],
    setAttribute(n, v) { this.attrs[n] = v; },
    getAttribute(n) { return (n in this.attrs) ? this.attrs[n] : null; },
    // appendChild of a fragment splices its children, like the real DOM
    appendChild(c) {
      if (c.tagName === '#FRAGMENT') this.childNodes.push(...c.childNodes);
      else this.childNodes.push(c);
      return c;
    },
    get innerHTML() { return _serialize(this); },
  };
}
function _escText(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function _escAttr(s) { return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;'); }
function _serialize(node) {
  return node.childNodes.map(function (c) {
    if (c.nodeType === 3) return _escText(c.nodeValue);
    const name  = c.tagName.toLowerCase();
    const attrs = Object.keys(c.attrs).map(k => ' ' + k + '="' + _escAttr(c.attrs[k]) + '"').join('');
    if (VOID_TAGS[c.tagName]) return '<' + name + attrs + '>';
    return '<' + name + attrs + '>' + _serialize(c) + '</' + name + '>';
  }).join('');
}
function _parse(html) {
  const root = _el('root'); const stack = [root];
  const tagRe = /<(\/)?([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;
  let last = 0, m;
  while ((m = tagRe.exec(html))) {
    if (m.index > last) stack[stack.length - 1].childNodes.push(_text(html.slice(last, m.index)));
    last = tagRe.lastIndex;
    const tag = m[2].toUpperCase();
    if (m[1]) { for (let i = stack.length - 1; i > 0; i--) { if (stack[i].tagName === tag) { stack.length = i; break; } } continue; }
    const el = _el(tag);
    const attrRe = /([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g; let a;
    while ((a = attrRe.exec(m[3] || '')))
      el.attrs[a[1]] = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '';
    stack[stack.length - 1].childNodes.push(el);
    if (!VOID_TAGS[tag]) stack.push(el);
  }
  if (last < html.length) stack[stack.length - 1].childNodes.push(_text(html.slice(last)));
  return root;
}
const document = {
  createElement(tag) {
    if (String(tag).toLowerCase() === 'template') {
      const content = _el('content');
      return {
        nodeType: 1, tagName: 'TEMPLATE', childNodes: [], attrs: {},
        content,
        setAttribute() {}, getAttribute() { return null; },
        appendChild(c) { this.childNodes.push(c); return c; },
        get innerHTML() { return _serialize(content); },
        set innerHTML(v) { content.childNodes = _parse(String(v)).childNodes; },
      };
    }
    return _el(tag);
  },
  createDocumentFragment() { return _el('#fragment'); },
};
"""


class TestIndexHtmlStatic(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = read_index_html()

    def test_doctype_is_first_line(self):
        self.assertTrue(self.html.startswith('<!DOCTYPE html>'),
                        'index.html must start with <!DOCTYPE html>')

    def test_notehtml_regex_sanitizer_removed(self):
        self.assertNotIn('on\\w+=', self.html,
                         'regex-based on* stripping must be gone')
        self.assertIn('NOTE_ALLOWED_TAGS', self.html)
        self.assertIn("createElement('template')", self.html)

    def test_gallery_data_images_escaped(self):
        self.assertIn('data-images="${esc(JSON.stringify(images))}"', self.html)
        self.assertNotIn("data-images='${", self.html)

    def test_discarded_status_filter_button(self):
        self.assertRegex(self.html, r"\{key:'4',\s*label:\s*t\('status_4'\)")

    def test_header_kpis_written_once(self):
        n = self.html.count("getElementById('headerKpis').innerHTML")
        self.assertEqual(n, 1, 'headerKpis must be assigned exactly once in init()')

    def test_detail_fetch_urls_encoded(self):
        self.assertIn('fetch(`data/${encodeURIComponent(file)}`)', self.html)
        self.assertIn('fetch(`data/${encodeURIComponent(String(sudnummer))}_influxdb.json`)', self.html)

    def test_bootstrap_applies_translations_even_if_index_fails(self):
        needle = 'i18n/${currentLang}.json`).then(r => r.json()).then(lang => { window.T = lang;'
        self.assertIn(needle, self.html)


class TestI18nFiles(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.files = sorted(f for f in os.listdir(I18N) if f.endswith('.json'))
        with open(os.path.join(I18N, 'de.json'), encoding='utf-8') as fh:
            cls.de = json.load(fh)

    def test_all_parse_as_utf8_json(self):
        for name in self.files:
            with open(os.path.join(I18N, name), encoding='utf-8') as fh:
                json.load(fh)  # raises on invalid UTF-8 / JSON

    def test_same_keys_as_de(self):
        for name in self.files:
            with open(os.path.join(I18N, name), encoding='utf-8') as fh:
                data = json.load(fh)
            self.assertEqual(set(data), set(self.de),
                             '%s key set differs from de.json' % name)

    def test_en_json_has_no_mojibake_question_runs(self):
        with open(os.path.join(I18N, 'en.json'), encoding='utf-8') as fh:
            data = json.load(fh)
        for key, value in data.items():
            if isinstance(value, str):
                self.assertIsNone(
                    re.search(r'\?\?', value),
                    'en.json[%r] contains a run of ? characters (mojibake): %r'
                    % (key, value))

    def test_en_json_non_ascii_keys_restored(self):
        with open(os.path.join(I18N, 'en.json'), encoding='utf-8') as fh:
            data = json.load(fh)
        self.assertEqual(data['back'], '\u2190 Back')
        self.assertEqual(data['loading'], 'Loading\u2026')
        self.assertEqual(data['loading_recipe'], 'Loading recipe\u2026')
        self.assertEqual(data['stats_sha'], '\u00d8 Brewhouse Eff.')
        self.assertEqual(data['dl_svg'], 'Label 1\u00d7')


@unittest.skipIf(NODE is None, 'node not available')
class TestNoteHTML(unittest.TestCase):
    """Executes the real noteHTML() from index.html against a DOM shim."""

    @classmethod
    def setUpClass(cls):
        html = read_index_html()
        parts = [DOM_SHIM, extract_function(html, 'esc')]
        # On the pre-fix code sanitizeNoteNode/NOTE_ALLOWED_TAGS do not exist;
        # the old regex-sanitizing noteHTML then runs as-is and the tests below fail.
        try:
            parts.append(extract_line(html, r"const NOTE_ALLOWED_TAGS = new Set\(\[[^\]]*\]\);"))
            parts.append(extract_function(html, 'sanitizeNoteNode'))
        except (ValueError, AssertionError):
            pass
        parts.append(extract_function(html, 'noteHTML'))
        cls.src = '\n'.join(parts)

    def run_note(self, input_str):
        script = (self.src
                  + '\nconst __out = noteHTML(%s);\n' % json.dumps(input_str)
                  + 'console.log(JSON.stringify(__out));')
        return json.loads(run_node(script))

    def test_unquoted_event_handler_dropped(self):
        out = self.run_note('<img src=x onerror=alert(1)>')
        self.assertNotIn('onerror', out)
        self.assertNotIn('<img', out)

    def test_iframe_dropped_text_kept(self):
        out = self.run_note('<iframe src="https://evil.example/x"></iframe>hi')
        self.assertNotIn('iframe', out)
        self.assertIn('hi', out)

    def test_event_handler_on_allowed_tag_dropped(self):
        out = self.run_note('<b style="x" onmouseover=evil()>t</b>')
        self.assertEqual(out, '<b>t</b>')

    def test_javascript_href_dropped(self):
        out = self.run_note('<a href="javascript:alert(1)">x</a>')
        self.assertNotIn('javascript', out)
        self.assertIn('x', out)

    def test_http_link_kept_with_rel_noopener(self):
        out = self.run_note('<a href="https://ok.example/p" onclick=evil()>t</a>')
        self.assertIn('href="https://ok.example/p"', out)
        self.assertIn('rel="noopener"', out)
        self.assertNotIn('onclick', out)

    def test_allowed_tags_survive(self):
        out = self.run_note('<p>He<b>llo</b> <i>w</i><strong>o</strong><em>r</em>ld<br></p>')
        self.assertEqual(out, '<p>He<b>llo</b> <i>w</i><strong>o</strong><em>r</em>ld<br></p>')

    def test_script_tag_never_survives(self):
        out = self.run_note('<script>alert(1)</script>')
        self.assertNotIn('<script', out.lower())

    def test_plain_text_branch_unchanged(self):
        out = self.run_note('line1\n\nline2 https://ex.example/a')
        self.assertIn('</p><p>', out)
        self.assertIn('href="https://ex.example/a"', out)
        self.assertIn('rel="noopener"', out)


@unittest.skipIf(NODE is None, 'node not available')
class TestPureHelpers(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = read_index_html()
        with open(os.path.join(I18N, 'en.json'), encoding='utf-8') as fh:
            cls.en = json.dumps(json.load(fh))

    def test_get_status_unknown_falls_back_to_finished(self):
        script = '\n'.join([
            'const window = { T: %s };' % self.en,
            extract_line(self.html, r"function t\(key\)[^\n]*"),
            extract_line(self.html, r"const STATUS_CLS = \{[^}]*\};"),
            extract_function(self.html, 'getStatus'),
            "const r = getStatus(9);",
            "console.log((r.label === window.T.status_3 && String(r.label).indexOf('status_9') < 0) ? 'PASS' : 'FAIL ' + r.label + ' / ' + r.cls);",
        ])
        self.assertEqual(run_node(script), 'PASS')

    def test_get_status_known_status_uses_translation(self):
        script = '\n'.join([
            'const window = { T: %s };' % self.en,
            extract_line(self.html, r"function t\(key\)[^\n]*"),
            extract_line(self.html, r"const STATUS_CLS = \{[^}]*\};"),
            extract_function(self.html, 'getStatus'),
            "const r = getStatus(4);",
            "console.log((r.label === window.T.status_4 && r.cls === 'st-4') ? 'PASS' : 'FAIL ' + r.label + ' / ' + r.cls);",
        ])
        self.assertEqual(run_node(script), 'PASS')

    def test_radar_vals_no_nan_for_non_numeric_rating(self):
        script = '\n'.join([
            extract_function(self.html, 'radarVals'),
            "const v = radarVals([{notes:'Bitterkeit',rating:'gut'}], 12, 5, 20);",
            "const ok = v.every(x => typeof x === 'number' && !Number.isNaN(x) && x >= 0 && x <= 1);",
            "console.log(ok ? 'PASS' : 'FAIL ' + JSON.stringify(v));",
        ])
        self.assertEqual(run_node(script), 'PASS')

    def test_stars_renders_zero_and_clamps_over_five(self):
        script = '\n'.join([
            extract_function(self.html, 'stars'),
            "const errs = [];",
            "if (stars(0).indexOf('\u2605') >= 0) errs.push('0 shows filled star');",
            "if (stars(0).split('\u2606').length - 1 !== 5) errs.push('0 does not show 5 empty stars');",
            "if (stars(6).split('\u2605').length - 1 !== 5) errs.push('6 not clamped to 5');",
            "if (stars(6).indexOf('\u2606') >= 0) errs.push('6 shows empty star');",
            "if (stars(null) !== '\u2013') errs.push('null not dash');",
            "console.log(errs.length ? 'FAIL ' + errs.join('; ') : 'PASS');",
        ])
        self.assertEqual(run_node(script), 'PASS')


@unittest.skipIf(NODE is None, 'node not available')
class TestYearGrouping(unittest.TestCase):

    def test_date_only_string_groups_in_local_timezone(self):
        html = read_index_html()
        line = extract_line(html, r"const key = b\.braudatum [^\n]*")
        script = '\n'.join([
            "const RECIPE_KEY = '__recipes__';",
            "const groups = {};",
            "const beers = [{ braudatum: '2027-01-01' }];",
            "beers.forEach(b => { %s (groups[key] = groups[key] || []).push(b); });" % line,
            "console.log(Object.keys(groups).join(','));",
        ])
        # UTC-negative timezone: new Date('2027-01-01') is 2026-12-31 local time
        self.assertEqual(run_node(script, {'TZ': 'America/New_York'}), '2027')

    def test_fmt_date_renders_date_only_string_without_shift(self):
        html = read_index_html()
        fn = extract_function(html, 'fmtDate')
        script = '\n'.join([
            fn,
            "console.log(fmtDate('2027-01-01'));",
        ])
        # new Date('2027-01-01') is 2026-12-31 19:00 local here: the day must not shift
        self.assertEqual(run_node(script, {'TZ': 'America/New_York'}), '01.01.2027')


@unittest.skipIf(NODE is None, 'node not available')
class TestSetLang(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = extract_function(read_index_html(), 'setLang')

    def _run(self, fetch_stub, check):
        script = '\n'.join([
            "globalThis.window = {};",
            "const store = {};",
            "const localStorage = { setItem(k, v) { store[k] = String(v); } };",
            "const document = { getElementById() { return { classList: { remove() {}, add() {}, toggle() {} }, textContent: '' }; } };",
            "let currentLang = 'de', cachedIndex = null;",
            "function buildLangMenu() {}",
            "function applyStaticTranslations() { globalThis.__static = true; }",
            "function init() { globalThis.__init = true; }",
            "globalThis.fetch = %s;" % fetch_stub,
            self.src,
            "setLang('fr');",
            "setTimeout(() => { %s }, 60);" % check,
        ])
        return run_node(script)

    def test_failed_language_file_is_not_persisted(self):
        out = self._run(
            "() => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) })",
            "console.log((currentLang === 'de' && store.lang === 'de') ? 'PASS' : 'FAIL lang=' + currentLang + ' stored=' + store.lang);")
        self.assertEqual(out, 'PASS')

    def test_malformed_language_file_is_not_persisted(self):
        out = self._run(
            "() => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new SyntaxError('bad json')) })",
            "console.log((currentLang === 'de' && store.lang === 'de') ? 'PASS' : 'FAIL lang=' + currentLang + ' stored=' + store.lang);")
        self.assertEqual(out, 'PASS')

    def test_successful_language_switch_persists(self):
        out = self._run(
            "() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status_3: 'Fini' }) })",
            "console.log((currentLang === 'fr' && store.lang === 'fr' && window.T.status_3 === 'Fini') ? 'PASS' : 'FAIL lang=' + currentLang + ' stored=' + store.lang);")
        self.assertEqual(out, 'PASS')


if __name__ == '__main__':
    unittest.main()
