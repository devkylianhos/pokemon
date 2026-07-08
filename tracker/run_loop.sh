#!/usr/bin/env bash
# Altijd-aan tracker-loop voor een VPS/container. Draait tracker.py elke
# INTERVAL seconden (default 60). Env-variabelen komen uit de omgeving of .env.
set -u
cd "$(dirname "$0")/.."

INTERVAL="${INTERVAL:-60}"
echo "[run_loop] start — interval ${INTERVAL}s"

while true; do
  python tracker/tracker.py || echo "[run_loop] tracker gaf een fout, ga door"
  sleep "$INTERVAL"
done
