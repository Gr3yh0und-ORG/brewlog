#!/usr/bin/env bash
# deploy.sh – KBH2 Web Frontend → local + FTP publish (Mac/Linux / bash)
# Requires: .env in the project root (see .env.example), curl, python3
# Usage: deploy.sh [local|public|both] [flags]
#   Target defaults to "public" (unchanged historical behavior — FTP only).
#   "local" publishes to WWW_ROOT/brewlog/releases/<VERSION>/ and flips the
#   `current` symlink (WEBAPP_PROJECT_STANDARD.md §14B); only makes sense run
#   from the server, where WWW_ROOT is a real local path.
# Optional flags:
#   --labels        → also run generate_labels.py and upload web/labels/
#                     (public target only — local never serves labels/)
#   --skip-data     → skip export.py and skip uploading web/data/ and web/images/
#   --rollback[=N]  → roll back N releases (default 1) instead of publishing.
#                     public: re-upload a previously snapshotted release
#                     verbatim, no export/upload. local: flip `current` back,
#                     no re-copy. Target "both" isn't valid with --rollback —
#                     the two targets don't share a release history, so pick one.
set -euo pipefail
shopt -s nullglob

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_DIR="$ROOT/web"
# Secrets: $BREWLOG_ENV, else /etc/homelab/brewlog.env if readable, else repo .env
# (same order as web/utils.py env_path()).
if [[ -n "${BREWLOG_ENV:-}" ]]; then
    ENV_FILE="$BREWLOG_ENV"
elif [[ -r /etc/homelab/brewlog.env ]]; then
    ENV_FILE="/etc/homelab/brewlog.env"
else
    ENV_FILE="$ROOT/.env"
fi

TARGET="public"
LABELS=false
SKIP_DATA=false
ROLLBACK=false
ROLLBACK_N=1
for arg in "$@"; do
    case "$arg" in
        local|public|both) TARGET="$arg" ;;
        --labels)        LABELS=true ;;
        --skip-data)     SKIP_DATA=true ;;
        --rollback)      ROLLBACK=true ;;
        --rollback=*)    ROLLBACK=true; ROLLBACK_N="${arg#--rollback=}" ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

if $ROLLBACK && [[ "$TARGET" == "both" ]]; then
    echo "Error: --rollback needs a single target (local or public), not 'both' — the two targets don't share a release history." >&2
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: No env file found: $ENV_FILE" >&2
    echo "Copy .env.example to .env (or /etc/homelab/brewlog.env) and fill in credentials." >&2
    exit 1
fi

while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# || -z "${line// /}" ]] && continue
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
        # Strip a trailing CR: a CRLF .env is common on the Windows path
        # (deploy.ps1), and web/utils.py load_env() strips it too -- without
        # this the two readers of the same file disagree and FTP logins fail
        # on an invisible character.
        v="${BASH_REMATCH[2]}"; v="${v%$'\r'}"
        declare "${BASH_REMATCH[1]}=$v"
    fi
done < "$ENV_FILE"

if $ROLLBACK; then
    if [[ "$TARGET" == "local" ]]; then
        echo "==> Rolling back local release ${ROLLBACK_N} step(s)..."
        python3 "$ROOT/deploy/rollback.py" rollback-local "$ROLLBACK_N"
    else
        echo "==> Rolling back ${ROLLBACK_N} release(s)..."
        python3 "$ROOT/deploy/rollback.py" rollback "$ROLLBACK_N"
    fi
    exit $?
fi

# Fail here, with a named error, rather than with a raw `unbound variable`
# from set -u after export and the snapshot have already run.
for k in FTP_HOST FTP_USER FTP_PASS FTP_DIR LOGO_PNG; do
    if [[ -z "${!k:-}" ]]; then
        echo "Error: Missing $k in $ENV_FILE" >&2
        exit 1
    fi
done

urlencode() {
    python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "$1"
}

send_file() {
    local local_path="$1" remote_url="$2"
    for try in 1 2 3; do
        curl -s -T "$local_path" "$remote_url" --user "${FTP_USER}:${FTP_PASS}" --ftp-create-dirs \
            && return
        if (( try < 3 )); then sleep 3; fi
    done
    echo "Error: Upload failed after 3 attempts: $remote_url" >&2
    exit 1
}

# 1. Export data (skipped with --skip-data)
if $SKIP_DATA; then
    echo "==> Skipping data export (--skip-data)"
else
    echo "==> Exporting BeerJSON data..."
    python3 "$WEB_DIR/export.py"
fi

# 2. Generate labels (only with --labels)
if $LABELS; then
    echo "==> Generating SVG labels..."
    python3 "$WEB_DIR/generate_labels.py"
fi

if [[ "$TARGET" == "public" || "$TARGET" == "both" ]]; then
    # 2.5 Snapshot this release before uploading, for --rollback (WEBAPP_PROJECT_STANDARD.md §14B)
    echo "==> Snapshotting release..."
    python3 "$ROOT/deploy/rollback.py" snapshot

    echo "==> Uploading to ftp://${FTP_HOST}${FTP_DIR}/ ..."

    # 3. Upload index.html
    echo "  index.html"
    send_file "$WEB_DIR/index.html" "ftp://${FTP_HOST}${FTP_DIR}/index.html"

    # 3a. Upload health.json (version + build time, WEBAPP_PROJECT_STANDARD §6a). Written by
    # deploy/build.sh into dist/; the home-lab update check reads it from the live site.
    if [ -f "$ROOT/dist/health.json" ]; then
        echo "  health.json"
        send_file "$ROOT/dist/health.json" "ftp://${FTP_HOST}${FTP_DIR}/health.json"
    else
        echo "  health.json missing (run deploy/build.sh) — skipped"
    fi

    # 4. Upload favicon.svg
    echo "  favicon.svg"
    send_file "$WEB_DIR/favicon.svg" "ftp://${FTP_HOST}${FTP_DIR}/favicon.svg"

    # 5. Upload logo/
    echo "  logo/..."
    LOGO_ENCODED="$(urlencode "$LOGO_PNG")"
    send_file "$WEB_DIR/logo/$LOGO_PNG" "ftp://${FTP_HOST}${FTP_DIR}/logo/$LOGO_ENCODED"
    # SVG logo (ASCII filename – no encoding needed); only present if LOGO_SVG resolved at export
    if [[ -f "$WEB_DIR/logo/logo.svg" ]]; then
        send_file "$WEB_DIR/logo/logo.svg" "ftp://${FTP_HOST}${FTP_DIR}/logo/logo.svg"
    fi

    # 6. Upload i18n/
    I18N_FILES=("$WEB_DIR/i18n"/*.json)
    echo "  i18n/ (${#I18N_FILES[@]} files)..."
    for f in "${I18N_FILES[@]}"; do
        send_file "$f" "ftp://${FTP_HOST}${FTP_DIR}/i18n/$(basename "$f")"
    done

    # 7. Upload data/ (skipped with --skip-data)
    if $SKIP_DATA; then
        echo "  data/ skipped (--skip-data)"
    else
        DATA_FILES=("$WEB_DIR/data"/*.json)
        echo "  data/ (${#DATA_FILES[@]} files)..."
        for f in "${DATA_FILES[@]}"; do
            send_file "$f" "ftp://${FTP_HOST}${FTP_DIR}/data/$(basename "$f")"
        done
    fi

    # 8. Upload images/ (skipped with --skip-data)
    if $SKIP_DATA; then
        echo "  images/ skipped (--skip-data)"
    elif [[ -d "$WEB_DIR/images" ]]; then
        IMAGE_FILES=("$WEB_DIR/images"/*)
        echo "  images/ (${#IMAGE_FILES[@]} files)..."
        for f in "${IMAGE_FILES[@]}"; do
            ENCODED="$(urlencode "$(basename "$f")")"
            send_file "$f" "ftp://${FTP_HOST}${FTP_DIR}/images/$ENCODED"
        done
    else
        echo "  images/ not found, skipping"
    fi

    # 9. Upload labels/ (only with --labels)
    if $LABELS; then
        if [[ -d "$WEB_DIR/labels" ]]; then
            LABEL_FILES=("$WEB_DIR/labels"/*.svg)
            echo "  labels/ (${#LABEL_FILES[@]} files)..."
            for f in "${LABEL_FILES[@]}"; do
                send_file "$f" "ftp://${FTP_HOST}${FTP_DIR}/labels/$(basename "$f")"
            done
        fi
    else
        echo "  labels/ skipped (no --labels flag)"
    fi

    echo ""
    echo "Public deploy complete: ${SITE_URL}"
fi

if [[ "$TARGET" == "local" || "$TARGET" == "both" ]]; then
    # local: publish into WWW_ROOT/brewlog/releases/<VERSION>/ and flip
    # `current` (WEBAPP_PROJECT_STANDARD.md §14B). No --labels equivalent —
    # printed label sheets aren't part of the served site either target ships.
    echo "==> Publishing local release $(cat "$ROOT/VERSION")..."
    python3 "$ROOT/deploy/rollback.py" publish-local
fi
