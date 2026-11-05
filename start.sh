#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
    if command -v uv >/dev/null 2>&1; then
        uv sync --frozen
    else
        echo "Установите uv и повторите запуск: https://docs.astral.sh/uv/getting-started/installation/" >&2
        exit 1
    fi
fi

exec .venv/bin/python scripts/start_local.py "$@"
