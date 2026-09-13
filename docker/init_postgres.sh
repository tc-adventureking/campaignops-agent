#!/bin/sh
set -eu
if [ ! -f "${DATA_DIR:-data/generated}/campaignops.duckdb" ]; then
    python -m scripts.seed_data
fi
exec python -m scripts.migrate_postgres
