#!/usr/bin/env bash
# Verify Apple Mail skill setup: Mail.app, Full Disk Access, dependencies, index.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIL_SH="$SCRIPT_DIR/mail.sh"
SKILL_ROOT="$(dirname "$SCRIPT_DIR")"
ASSETS_DIR="$SKILL_ROOT/assets"

echo "=== Apple Mail Skill Setup Check ===" >&2
echo "" >&2

# 1. Check Mail.app
echo "1. Mail.app..." >&2
if pgrep -x "Mail" > /dev/null 2>&1; then
    echo "   OK — Mail.app is running" >&2
else
    echo "   WARN — Mail.app is not running. Start it before using mail commands." >&2
fi

# 2. Check micromamba
echo "2. micromamba..." >&2
if command -v micromamba &>/dev/null; then
    echo "   OK — $(micromamba --version 2>/dev/null || echo 'installed')" >&2
else
    echo "   FAIL — micromamba not found. Install: https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html" >&2
    exit 1
fi

# 2b. Ensure MAMBA_ROOT_PREFIX is set (non-interactive shells may lack it)
if [ -z "${MAMBA_ROOT_PREFIX:-}" ]; then
    _env_path=$(micromamba env list 2>/dev/null | awk '/\/envs\//{print $NF; exit}')
    if [ -n "$_env_path" ]; then
        export MAMBA_ROOT_PREFIX="$(dirname "$(dirname "$_env_path")")"
    elif [ -d "$HOME/micromamba" ]; then
        export MAMBA_ROOT_PREFIX="$HOME/micromamba"
    fi
    if [ -n "${MAMBA_ROOT_PREFIX:-}" ]; then
        echo "   (auto-detected MAMBA_ROOT_PREFIX=$MAMBA_ROOT_PREFIX)" >&2
    else
        echo "   WARN — MAMBA_ROOT_PREFIX not set and could not auto-detect. Set it in your shell profile." >&2
    fi
fi

# 2c. Check mcp env exists
echo "   Checking 'mcp' env..." >&2
if micromamba env list 2>/dev/null | grep -qw "mcp"; then
    mcp_path=$(micromamba env list 2>/dev/null | grep -w "mcp" | awk '{print $NF}')
    echo "   OK — mcp env at $mcp_path" >&2
else
    echo "   WARN — 'mcp' env not found. It will be created on first run of mail.sh." >&2
fi

# 3. Check env + deps via health check (runs through full bootstrap)
echo "3. Environment + dependencies..." >&2
health_result=$("$MAIL_SH" check-health 2>/dev/null || echo '{"success": false}')
if echo "$health_result" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('success') else 1)" 2>/dev/null; then
    echo "   OK — environment ready, Mail.app responding" >&2
else
    echo "   WARN — health check failed (Mail.app may not be running or env setup incomplete)" >&2
    echo "   Output: $health_result" >&2
fi

# 4. Check Full Disk Access (attempt to list mail directory)
echo "4. Full Disk Access..." >&2
MAIL_DIR="$HOME/Library/Mail/V10"
if [ -d "$MAIL_DIR" ] && ls "$MAIL_DIR" > /dev/null 2>&1; then
    echo "   OK — can access $MAIL_DIR" >&2
else
    echo "   WARN — cannot access $MAIL_DIR. Grant Full Disk Access to Terminal in System Settings." >&2
fi

# 5. Index status
echo "5. Search index..." >&2
DB_PATH="$ASSETS_DIR/index.db"
if [ -f "$DB_PATH" ]; then
    db_size=$(du -h "$DB_PATH" 2>/dev/null | cut -f1)
    echo "   OK — index exists ($db_size)" >&2
else
    echo "   WARN — no index found. Run: $MAIL_SH build-index" >&2
fi

# 6. Spotlight indexing (mds_stores CPU drain prevention)
echo "6. Spotlight exclusion..." >&2
MAIL_LIB_DIR="$HOME/Library/Mail"
if [ -d "$MAIL_LIB_DIR" ]; then
    spotlight_ok=false
    # check .metadata_never_index marker
    if [ -f "$MAIL_LIB_DIR/.metadata_never_index" ]; then
        spotlight_ok=true
    fi
    # check VolumeConfiguration.plist exclusions (authoritative)
    if /usr/libexec/PlistBuddy -c 'Print :Exclusions' /System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist 2>/dev/null | grep -q "$MAIL_LIB_DIR"; then
        spotlight_ok=true
    fi
    if $spotlight_ok; then
        echo "   OK — ~/Library/Mail is excluded from Spotlight" >&2
    else
        echo "   WARN — ~/Library/Mail is NOT excluded from Spotlight." >&2
        echo "   This can cause mds_stores to consume 100%+ CPU indefinitely." >&2
        echo "   Fix: $MAIL_SH fix-spotlight" >&2
    fi
else
    echo "   SKIP — ~/Library/Mail not found" >&2
fi

# 7. Show resolved paths
echo "" >&2
echo "=== Resolved Paths ===" >&2
echo "  Skill root:  $SKILL_ROOT" >&2
echo "  Scripts:     $SCRIPT_DIR" >&2
echo "  Assets:      $ASSETS_DIR" >&2
echo "  Index DB:    $DB_PATH" >&2
echo "  mail.sh:     $MAIL_SH" >&2
echo "" >&2
echo "Setup check complete." >&2
