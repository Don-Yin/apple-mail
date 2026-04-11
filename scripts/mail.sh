#!/usr/bin/env bash
# Apple Mail CLI launcher — bootstraps micromamba env and runs mail.py
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(dirname "$SCRIPT_DIR")"
ASSETS_DIR="$SKILL_ROOT/assets"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
DEPS_HASH_FILE="$ASSETS_DIR/.deps.hash"
ENV_NAME="mcp"
PYTHON_VERSION="3.11"

# Ensure assets directory exists and is writable (Dropbox may reset permissions)
mkdir -p "$ASSETS_DIR"
chmod -R u+w "$ASSETS_DIR" 2>/dev/null || true

# ------------------------------------------------------------------
# Locate micromamba
# ------------------------------------------------------------------
if ! command -v micromamba &>/dev/null; then
    echo '{"success": false, "data": null, "error": {"code": "MICROMAMBA_NOT_FOUND", "message": "micromamba is not installed or not on PATH. Install it: https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html", "details": {}}, "warnings": [], "meta": {"command": "bootstrap", "execution_time_ms": 0, "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}' >&2
    exit 1
fi

# ------------------------------------------------------------------
# Ensure MAMBA_ROOT_PREFIX is set (non-interactive shells may lack it)
# ------------------------------------------------------------------
if [ -z "${MAMBA_ROOT_PREFIX:-}" ]; then
    # Derive root from an existing env path: /root/envs/name → /root
    _env_path=$(micromamba env list 2>/dev/null | awk '/\/envs\//{print $NF; exit}')
    if [ -n "$_env_path" ]; then
        export MAMBA_ROOT_PREFIX="$(dirname "$(dirname "$_env_path")")"
    elif [ -d "$HOME/micromamba" ]; then
        export MAMBA_ROOT_PREFIX="$HOME/micromamba"
    else
        echo '{"success": false, "data": null, "error": {"code": "MAMBA_ROOT_UNKNOWN", "message": "Could not detect MAMBA_ROOT_PREFIX. Set it in your shell profile.", "details": {}}, "warnings": [], "meta": {"command": "bootstrap", "execution_time_ms": 0, "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}' >&2
        exit 1
    fi
fi

# ------------------------------------------------------------------
# Auto-create env if missing
# ------------------------------------------------------------------
if ! micromamba env list 2>/dev/null | grep -qw "$ENV_NAME"; then
    echo "Creating micromamba environment '$ENV_NAME' with Python $PYTHON_VERSION..." >&2
    micromamba create -y -n "$ENV_NAME" "python=$PYTHON_VERSION" >&2
fi

# ------------------------------------------------------------------
# Install/update dependencies if requirements changed
# ------------------------------------------------------------------
_current_hash=""
if [ -f "$REQUIREMENTS" ]; then
    _current_hash=$(shasum -a 256 "$REQUIREMENTS" 2>/dev/null | cut -d' ' -f1)
fi

_cached_hash=""
if [ -f "$DEPS_HASH_FILE" ]; then
    _cached_hash=$(cat "$DEPS_HASH_FILE" 2>/dev/null)
fi

if [ -n "$_current_hash" ] && [ "$_current_hash" != "$_cached_hash" ]; then
    echo "Installing/updating dependencies from requirements.txt..." >&2
    "$MAMBA_ROOT_PREFIX/envs/$ENV_NAME/bin/pip" install -q -r "$REQUIREMENTS" >&2
    echo "$_current_hash" > "$DEPS_HASH_FILE"
fi

# ------------------------------------------------------------------
# Run mail.py — use env python directly (micromamba run fails in non-interactive shells)
# ------------------------------------------------------------------
ENV_PYTHON="$MAMBA_ROOT_PREFIX/envs/$ENV_NAME/bin/python"
if [ ! -x "$ENV_PYTHON" ]; then
    echo '{"success": false, "data": null, "error": {"code": "PYTHON_NOT_FOUND", "message": "python not found at '"$ENV_PYTHON"'. Run: micromamba create -y -n '"$ENV_NAME"' python='"$PYTHON_VERSION"'", "details": {}}, "warnings": [], "meta": {"command": "bootstrap", "execution_time_ms": 0, "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}}' >&2
    exit 1
fi
exec "$ENV_PYTHON" "$SCRIPT_DIR/mail.py" "$@"
