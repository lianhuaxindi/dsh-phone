#!/usr/bin/env bash
# Sync app/web static assets into the Android app WebView assets directory.
#
#   source      : app/web/                              (owned by web-ui)
#   destination : app/android/app/src/main/assets/web/  (generated, git-ignored)
#   exclusions  : tests/ directory, node_modules/, any *.md (Markdown docs), package.json (tooling only)
#
# The command is idempotent: it copies added/changed files and removes stale
# files from the destination, so running it twice yields the same tree.
#
# Exit codes: 0 = success / target up to date, 1 = missing inputs or differences.
#
# Usage:
#   bash scripts/sync_web.sh
#   bash scripts/sync_web.sh --verify-only
#   bash scripts/sync_web.sh --source /path/to/web --dest /tmp/assets-web
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

VERIFY_ONLY=0
SRC=""
DST=""
while [ $# -gt 0 ]; do
    case "$1" in
        --verify-only)
            VERIFY_ONLY=1
            shift
            ;;
        --source)
            [ $# -ge 2 ] || { printf '[sync_web][ERROR] --source requires a path\n' >&2; exit 2; }
            SRC=$2
            shift 2
            ;;
        --dest)
            [ $# -ge 2 ] || { printf '[sync_web][ERROR] --dest requires a path\n' >&2; exit 2; }
            DST=$2
            shift 2
            ;;
        -h | --help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            printf '[sync_web][ERROR] unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

USING_DEFAULT_DST=0
if [ -z "$SRC" ]; then
    SRC="$REPO_ROOT/app/web"
fi
if [ -z "$DST" ]; then
    USING_DEFAULT_DST=1
    DST="$REPO_ROOT/app/android/app/src/main/assets/web"
fi
# normalise trailing slashes so relpath() prefix matching works
SRC=${SRC%/}
DST=${DST%/}

log() { printf '[sync_web] %s\n' "$*"; }
fail() { printf '[sync_web][ERROR] %s\n' "$*" >&2; exit 1; }

TMP_BASE=$(mktemp "${TMPDIR:-/tmp}/dsphone-sync-web.XXXXXX")
SRC_LIST="$TMP_BASE.src"   # absolute paths of every file under app/web
REL_LIST="$TMP_BASE.rel"   # relative paths of files that must ship in assets/web
DST_LIST="$TMP_BASE.dst"   # absolute paths of every file under the destination
: > "$SRC_LIST"
: > "$REL_LIST"
: > "$DST_LIST"
cleanup() { rm -f "$SRC_LIST" "$REL_LIST" "$DST_LIST"; }
trap cleanup EXIT

relpath() { # $1 = base (absolute or normalised relative), $2 = file
    case "$2" in
        "$1"/*) printf '%s' "${2#"$1"/}" ;;
        *) printf '[sync_web][ERROR] internal: %s is not under %s\n' "$2" "$1" >&2; exit 1 ;;
    esac
}

is_excluded() {
    case "$1" in
        *.md | *.tmp | package.json | */package.json | tests/* | node_modules/* | */node_modules/*) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------- inputs
if [ ! -d "$SRC" ]; then
    fail "source directory not found: $SRC
  -> app/web is provided by the web-ui module; retry after it lands (or pass --source)."
fi
if [ "$USING_DEFAULT_DST" -eq 1 ] && [ ! -d "$REPO_ROOT/app/android" ]; then
    fail "Android project directory not found: $REPO_ROOT/app/android
  -> the android shell module owns app/android; this script only copies app/web into assets/web."
fi
if [ ! -f "$SRC/index.html" ]; then
    fail "missing entry file: $SRC/index.html
  -> the WebView loads assets/web/index.html; without it the APK has no UI, refusing to sync."
fi

# ---------------------------------------------------------------- collect files
find "$SRC" -type f | LC_ALL=C sort > "$SRC_LIST"
COUNT=0
SKIPPED=0
SKIPPED_MD=0
SKIPPED_PKG=0
SKIPPED_TMP=0
while IFS= read -r file; do
    rel=$(relpath "$SRC" "$file")
    if is_excluded "$rel"; then
        SKIPPED=$((SKIPPED + 1))
        case "$rel" in *.md) SKIPPED_MD=$((SKIPPED_MD + 1)) ;; esac
        case "$rel" in package.json | */package.json) SKIPPED_PKG=$((SKIPPED_PKG + 1)) ;; esac
        case "$rel" in *.tmp) SKIPPED_TMP=$((SKIPPED_TMP + 1)) ;; esac
        continue
    fi
    printf '%s\n' "$rel" >> "$REL_LIST"
    COUNT=$((COUNT + 1))
done < "$SRC_LIST"
if [ "$COUNT" -eq 0 ]; then
    fail "no static assets to sync under: $SRC
  -> app/web looks empty (web-ui may not have produced index.html/js/css yet)."
fi

# ---------------------------------------------------------------- verify only
if [ "$VERIFY_ONLY" -eq 1 ]; then
    DIFFS=0
    if [ ! -d "$DST" ]; then
        while IFS= read -r rel; do
            log "missing: $rel"
            DIFFS=$((DIFFS + 1))
        done < "$REL_LIST"
    else
        while IFS= read -r rel; do
            file="$SRC/$rel"
            target="$DST/$rel"
            if [ ! -f "$target" ]; then
                log "missing: $rel"
                DIFFS=$((DIFFS + 1))
            elif ! cmp -s "$file" "$target"; then
                log "content differs: $rel"
                DIFFS=$((DIFFS + 1))
            fi
        done < "$REL_LIST"
        find "$DST" -type f > "$DST_LIST"
        while IFS= read -r target; do
            rel=$(relpath "$DST" "$target")
            if ! grep -F -x -q -- "$rel" "$REL_LIST"; then
                log "stale: $rel"
                DIFFS=$((DIFFS + 1))
            fi
        done < "$DST_LIST"
    fi
    if [ "$DIFFS" -gt 0 ]; then
        fail "target is out of sync with app/web: $DST ($DIFFS differences)"
    fi
    log "source      : $SRC"
    log "destination : $DST"
    log "excluded    : tests/, node_modules/, *.md, package.json, *.tmp (skipped $SKIPPED, *.md=$SKIPPED_MD, package.json=$SKIPPED_PKG, *.tmp=$SKIPPED_TMP)"
    log "verify OK: target is up to date ($COUNT files): $DST"
    exit 0
fi

# ---------------------------------------------------------------- sync
mkdir -p "$DST"
COPIED=0
UNCHANGED=0
VANISHED=0
while IFS= read -r rel; do
    file="$SRC/$rel"
    target="$DST/$rel"
    mkdir -p "$(dirname -- "$target")"
    if [ -f "$target" ] && cmp -s "$file" "$target"; then
        UNCHANGED=$((UNCHANGED + 1))
    elif [ ! -f "$file" ]; then
        log "WARN: source vanished during sync, skipped: $rel"
        VANISHED=$((VANISHED + 1))
    else
        cp -p "$file" "$target"
        COPIED=$((COPIED + 1))
    fi
done < "$REL_LIST"

# remove destination files that are no longer produced by app/web
REMOVED=0
find "$DST" -type f > "$DST_LIST"
while IFS= read -r target; do
    rel=$(relpath "$DST" "$target")
    if ! grep -F -x -q -- "$rel" "$REL_LIST"; then
        rm -f -- "$target"
        REMOVED=$((REMOVED + 1))
    fi
done < "$DST_LIST"

# prune directories that became empty because of removed stale files
find "$DST" -depth -type d -empty -delete 2>/dev/null || true

if [ ! -f "$DST/index.html" ]; then
    fail "missing destination entry file after sync: $DST/index.html"
fi

log "source      : $SRC"
log "destination : $DST"
log "excluded    : tests/, node_modules/, *.md, package.json (skipped $SKIPPED, *.md=$SKIPPED_MD, package.json=$SKIPPED_PKG)"
log "synced      : $COUNT files total (copied $COPIED, unchanged $UNCHANGED, removed $REMOVED, vanished $VANISHED)"
log "OK: $DST/index.html"
exit 0
