# Brewlog for KBH2 (Web Frontend)

[![CI](https://github.com/gr3yh0und/brewlog/actions/workflows/ci.yml/badge.svg)](https://github.com/gr3yh0und/brewlog/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A static web frontend for the [Kleiner Brauhelfer 2](https://github.com/kleiner-brauhelfer/kleiner-brauhelfer-2) home brewery database. Reads `brauhelfer.sqlite`, exports BeerJSON + BeerXML files, generates SVG bottle labels, and deploys everything to a static web host via FTP.

## What it does

Brewlog turns your KBH2 SQLite database into a beautiful, self-hosted beer catalog. It exports every brew as BeerJSON and BeerXML files, generates printable SVG bottle labels with QR codes, and optionally charts brew-day and fermentation data from InfluxDB sensors. The entire site is a single `index.html` with no build step — deploy it to any static host via FTP.

Key features:
- **Beer catalog** — Filter by status and style, with radar charts, KPIs, and dark mode
- **Detail pages** — Full recipe breakdown: grain bill, hops, mash plan, fermentation timeline
- **Labels** — DIN A4-ready SVG labels with QR codes, radar charts, and your brewery logo
- **Sensor data** — Brew-day kettle temps and fermentation curves from iSpindel + MQTT sensors
- **Multilingual** — 8 languages (DE, EN, FR, IT, ES, NL, DA, CS) with a flag picker

## Screenshots

<!-- Replace with your own screenshots -->

**List view** — Beer cards with radar charts, status filters, and style chips:

![List view](docs/screenshots/list.png)

**Detail view** — Full recipe with mash plan, fermentation charts, and photo gallery:

![Detail view](docs/screenshots/detail.png)

**Labels** — Printable SVG labels with QR codes and radar charts:

![Labels](docs/screenshots/labels.png)

## Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.11 | Core scripts use only stdlib; see `requirements.txt` for optional extras |
| `curl` | Used by the deploy scripts for FTP upload |
| [Kleiner Brauhelfer 2](https://github.com/kleiner-brauhelfer/kleiner-brauhelfer-2) | Source of `brauhelfer.sqlite` |

```
pip install -r requirements.txt   # optional: Pillow + qrcode + influxdb-client
```

## Quick Start

### Try it with sample data (no KBH2 database needed)

Generate 5 example brews to preview the site locally:

```bash
python sample/generate_sample_data.py
```

Then configure `.env`, export, and preview:

```bash
copy .env.example .env   # edit FTP + brewery settings
python web/export.py
cd web && python -m http.server 8080
```

Open **http://localhost:8080** to see the sample catalog.

### Use your own KBH2 database

1. Copy `.env.example` → `.env` and configure your FTP credentials, brewery name, and logo filenames
2. Place your `brauhelfer.sqlite` database in `input/`
3. Run deploy to export data and publish the site:

```powershell
deploy/deploy.ps1          # Windows
bash deploy/deploy.sh      # Mac/Linux
```

That's it — your brew log is live at the URL configured in `FTP_DIR`. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the `local` publish target and rollback for both.

## Project Structure

```
brewlog/
├── input/
│   ├── brauhelfer.sqlite          # KBH2 database (not in repo)
│   ├── enrichment/                # Taste profile, Untappd ID, label color
│   ├── influxdb/                  # Fetched by fetch_influxdb.py (not in repo)
│   ├── images/                    # Brew photos — source of truth (not in repo)
│   └── logo/                      # Logo source files
├── web/
│   ├── export.py                  # SQLite + enrichment + images → web/data/ + web/images/
│   ├── generate_labels.py         # BeerJSON → SVG labels
│   ├── fetch_influxdb.py          # InfluxDB → input/influxdb/{n}.json (optional)
│   ├── index.html                 # SPA (reads data/ via fetch)
│   ├── i18n/                      # Translation files (8 languages)
│   ├── data/                      # Generated (not in repo)
│   ├── images/                    # Generated (not in repo)
│   └── labels/                    # Generated (not in repo)
├── tests/
├── deploy/
│   ├── build.sh                   # export + assemble dist/ (BUILD_CMD for /deploy)
│   ├── deploy.ps1                 # FTP upload – Windows (PowerShell)
│   ├── deploy.sh                  # FTP upload + local publish – Mac/Linux (bash)
│   └── rollback.py                # release snapshots + rollback for both targets
├── .env                           # FTP credentials + brewery config (not in repo)
└── .env.example                   # Template for .env
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests run against a self-contained fixture SQLite (two example brews created in code — no real database needed).

## Documentation

- **[Configuration](docs/CONFIGURATION.md)** — Environment variables, enrichment files, photos, i18n
- **[Deployment & Workflow](docs/DEPLOYMENT.md)** — Deploy commands, local development, label generator
- **[Website Features](docs/WEBSITE.md)** — List view, cards, detail view, radar chart
- **[InfluxDB](docs/INFLUXDB.md)** — Fermentation data fetching, schema, configuration
- **[`homelab.yml`](homelab.yml)** — Classification metadata (profile, route, personal-data flag) for infra tooling — see `infrastructure/WEBAPP_PROJECT_STANDARD.md` §1a
- **Compliance** — this repo is public, so its `COMPLIANCE.md` lives in the infrastructure repo instead: [`compliance/brewlog.md`](https://github.com/Gr3yh0und/infrastructure/blob/main/compliance/brewlog.md) (see `WEBAPP_PROJECT_STANDARD.md` §1b for why)