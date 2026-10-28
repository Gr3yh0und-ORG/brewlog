import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock

import helpers  # sets up sys.path; must come before web imports
import export as export_module


class TestExport(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.db_path  = os.path.join(cls.tmpdir, 'test.sqlite')
        cls.data_dir = os.path.join(cls.tmpdir, 'data')
        cls.img_out  = os.path.join(cls.tmpdir, 'images')
        cls.logo_out = os.path.join(cls.tmpdir, 'logo')

        helpers.create_fixture_db(cls.db_path)

        with unittest.mock.patch.multiple(
            export_module,
            DB_PATH   = cls.db_path,
            ENR_DIR   = helpers.ENRICHMENT_DIR,
            IMG_DIR   = '',
            DATA_DIR  = cls.data_dir,
            IMG_OUT   = cls.img_out,
            LOGO_SRC  = '',
            LOGO_OUT  = cls.logo_out,
            BREWERY_NAME = 'Test Brewery',
            SITE_URL     = 'https://test.example.com',
            LOGO_PNG     = 'test-logo.png',
        ):
            export_module.export()

    # ── index.json ────────────────────────────────────────────────────

    def test_index_json_created(self):
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, 'index.json')))

    def test_index_json_structure(self):
        with open(os.path.join(self.data_dir, 'index.json'), encoding='utf-8') as f:
            idx = json.load(f)
        self.assertIn('config', idx)
        self.assertIn('exportiert', idx)
        self.assertIn('beers', idx)
        self.assertEqual(len(idx['beers']), 2)

    def test_index_config_block(self):
        with open(os.path.join(self.data_dir, 'index.json'), encoding='utf-8') as f:
            cfg = json.load(f)['config']
        self.assertEqual(cfg['brewery_name'], 'Test Brewery')
        self.assertEqual(cfg['site_url'], 'https://test.example.com')
        self.assertEqual(cfg['logo_png'], 'test-logo.png')

    def test_index_beer_fields(self):
        with open(os.path.join(self.data_dir, 'index.json'), encoding='utf-8') as f:
            beers = json.load(f)['beers']
        # Ordered by Sudnummer DESC → brew 2 first
        required = {'sudnummer', 'name', 'category', 'status', 'ebc', 'sw', 'ibu', 'alkohol', 'file'}
        for beer in beers:
            self.assertTrue(required.issubset(beer.keys()), f"Missing keys in {beer}")

    # ── per-brew BeerJSON ─────────────────────────────────────────────

    def test_beerjson_files_created(self):
        for n in (1, 2):
            path = os.path.join(self.data_dir, f'{n}_beerjson.json')
            self.assertTrue(os.path.exists(path), f'Missing: {n}_beerjson.json')

    def test_beerjson_structure(self):
        with open(os.path.join(self.data_dir, '1_beerjson.json'), encoding='utf-8') as f:
            bj = json.load(f)
        self.assertIn('beerjson', bj)
        self.assertAlmostEqual(bj['beerjson']['version'], 2.06)
        recipe = bj['beerjson']['recipes'][0]
        self.assertEqual(recipe['name'], 'Example Pale Ale')
        self.assertEqual(recipe['author'], 'Test Brewery')

    def test_brewery_extension_present(self):
        with open(os.path.join(self.data_dir, '1_beerjson.json'), encoding='utf-8') as f:
            recipe = json.load(f)['beerjson']['recipes'][0]
        self.assertIn('_brewery', recipe)
        sb = recipe['_brewery']
        self.assertEqual(sb['sudnummer'], 1)
        self.assertEqual(sb['status'], 3)
        self.assertIn('label_color', sb)
        self.assertIn('tastes', sb)

    def test_enrichment_applied(self):
        with open(os.path.join(self.data_dir, '1_beerjson.json'), encoding='utf-8') as f:
            sb = json.load(f)['beerjson']['recipes'][0]['_brewery']
        self.assertEqual(sb['untappd_id'], '12345')
        self.assertEqual(sb['label_color'], '#C8A860')
        self.assertEqual(len(sb['tastes']), 5)

    def test_enrichment_stout_values(self):
        with open(os.path.join(self.data_dir, '2_beerjson.json'), encoding='utf-8') as f:
            sb = json.load(f)['beerjson']['recipes'][0]['_brewery']
        self.assertEqual(sb['label_color'], '#1A0A00')
        self.assertEqual(len(sb['tastes']), 5)
        ratings = {t['notes']: t['rating'] for t in sb['tastes']}
        self.assertEqual(ratings['Bitterkeit'], 0.80)
        self.assertEqual(ratings['Frucht'],     0.35)
        self.assertEqual(ratings['Erde'],       0.60)

    def test_ingredients_exported(self):
        with open(os.path.join(self.data_dir, '1_beerjson.json'), encoding='utf-8') as f:
            ing = json.load(f)['beerjson']['recipes'][0]['ingredients']
        self.assertEqual(len(ing['fermentable_additions']), 2)
        self.assertEqual(len(ing['hop_additions']), 2)
        self.assertEqual(len(ing['culture_additions']), 1)

    def test_mash_steps_exported(self):
        with open(os.path.join(self.data_dir, '1_beerjson.json'), encoding='utf-8') as f:
            mash = json.load(f)['beerjson']['recipes'][0]['mash']
        self.assertEqual(len(mash['mash_steps']), 3)


class TestExportEdgeCases(unittest.TestCase):
    """export() must survive dirty data instead of crashing or writing junk."""

    def _run_export(self, db_path, enr_dir, tmpdir):
        with unittest.mock.patch.multiple(
            export_module,
            DB_PATH      = db_path,
            ENR_DIR      = enr_dir,
            IMG_DIR      = '',
            DATA_DIR     = os.path.join(tmpdir, 'data'),
            IMG_OUT      = os.path.join(tmpdir, 'images'),
            LOGO_SRC     = '',
            LOGO_OUT     = os.path.join(tmpdir, 'logo'),
            BREWERY_NAME = 'Test Brewery',
            SITE_URL     = 'https://test.example.com',
            LOGO_PNG     = 'test-logo.png',
        ):
            export_module.export()
        return os.path.join(tmpdir, 'data')

    def test_null_sudnummer_row_is_skipped(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, 'test.sqlite')
        helpers.create_fixture_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO Sud (ID, Sudnummer, Sudname, Kategorie, Status, "
                     "Erstellt, Menge, SW, IBU, erg_Farbe) "
                     "VALUES (3, NULL, 'Ghost Brew', 'Ale', 0, '2024-01-01', "
                     "20, 12, 0, 5)")
        conn.commit()
        conn.close()

        data = self._run_export(db_path, helpers.ENRICHMENT_DIR, tmpdir)

        self.assertFalse(os.path.exists(os.path.join(data, 'None_beerjson.json')),
                         'NULL Sudnummer must not produce None_beerjson.json')
        with open(os.path.join(data, 'index.json'), encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)['beers']), 2)

    def test_missing_sub_tables_do_not_crash(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, 'test.sqlite')
        helpers.create_fixture_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('DROP TABLE Tags')
        conn.execute('DROP TABLE Bewertungen')
        conn.commit()
        cur = conn.cursor()
        try:
            sud = export_module.load_sud(cur, 1)   # must not raise
        finally:
            conn.close()
        self.assertEqual(sud['tags'], [])
        self.assertEqual(sud['bewertungen'], [])

    def test_malformed_enrichment_file_is_ignored(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, 'test.sqlite')
        helpers.create_fixture_db(db_path)
        enr_dir = os.path.join(tmpdir, 'enrichment')
        os.makedirs(enr_dir)
        shutil.copy(os.path.join(helpers.ENRICHMENT_DIR, '2.json'), enr_dir)
        with open(os.path.join(enr_dir, '1.json'), 'w', encoding='utf-8') as f:
            f.write('{ not valid json')

        data = self._run_export(db_path, enr_dir, tmpdir)

        # Export completes; brew #1 falls back to defaults, brew #2 keeps its enrichment
        self.assertTrue(os.path.exists(os.path.join(data, '1_beerjson.json')))
        with open(os.path.join(data, '1_beerjson.json'), encoding='utf-8') as f:
            sb = json.load(f)['beerjson']['recipes'][0]['_brewery']
        self.assertEqual(sb['untappd_id'], '')
        self.assertEqual(sb['label_color'], '#D2CECE')
        with open(os.path.join(data, '2_beerjson.json'), encoding='utf-8') as f:
            sb2 = json.load(f)['beerjson']['recipes'][0]['_brewery']
        self.assertEqual(sb2['label_color'], '#1A0A00')


if __name__ == '__main__':
    unittest.main()
