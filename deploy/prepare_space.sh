#!/usr/bin/env bash
# Assemble exactly what the Hugging Face Space needs into build/hf-space/.
# No raw ticket data, no .env secrets, no virtualenv.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=build/hf-space
rm -rf "$OUT" && mkdir -p "$OUT/data/out" "$OUT/data/geo" "$OUT/docs"
cp deploy/Dockerfile deploy/requirements.txt "$OUT/"
cp deploy/README.space.md "$OUT/README.md"
cp config.json "$OUT/"
cp -R server web "$OUT/"
cp data/out/crowding.db "$OUT/data/out/"
cp data/out/city_lines.json "$OUT/data/out/" 2>/dev/null || true  # time-lapse geometry (else built on first visit)
cp data/geo/*.json "$OUT/data/geo/"
cp docs/TECHNICAL_NOTE.md docs/WHATSAPP_SETUP.md docs/TELEGRAM_SETUP.md "$OUT/docs/"
find "$OUT" -name "__pycache__" -type d -prune -exec rm -rf {} +
test ! -e "$OUT/.env" || { echo "refusing: .env would be uploaded"; exit 1; }
du -sh "$OUT"
echo "Ready: $OUT"
