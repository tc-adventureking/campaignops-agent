#!/bin/sh
set -eu
if [ ! -f "${DATA_DIR:-data/generated}/campaignops.duckdb" ]; then
    python -m scripts.seed_data
fi
python -m scripts.build_index
exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --workers 1
