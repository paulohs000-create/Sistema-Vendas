#!/usr/bin/env bash
set -euo pipefail

# Falha cedo se algum arquivo Python tiver marcador de conflito de merge.
if rg -n '^(<<<<<<< |=======|>>>>>>> )' --glob '*.py' . >/tmp/conflict_markers.txt; then
  echo '[BOOT] ERRO: marcadores de conflito detectados em arquivos .py:' >&2
  cat /tmp/conflict_markers.txt >&2
  exit 1
fi

# Sanidade mínima de sintaxe antes de subir o worker
python -m py_compile server.py

exec gunicorn server:app --bind 0.0.0.0:"${PORT}"
