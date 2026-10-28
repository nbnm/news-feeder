#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
uv sync --project backend --group packaging
DATABASE_URL=sqlite:///:memory: WORKER_ENABLED=false backend/.venv/bin/python -m PyInstaller \
  --noconfirm --onedir --name NewsFeederService \
  --distpath dist/service --workpath dist/service-build --specpath dist/service-build \
  --paths backend --collect-submodules app --collect-submodules uvicorn \
  --hidden-import sqlalchemy.dialects.sqlite --collect-data certifi \
  --collect-all trafilatura --collect-data courlan --collect-data htmldate --collect-data tld \
  scripts/bundled_backend.py
bash scripts/build-macos.sh release
app_path="$project_root/dist/News Feeder.app"
rm -rf "$app_path/Contents/Resources/Backend"
ditto dist/service/NewsFeederService "$app_path/Contents/Resources/Backend"
codesign --force --sign - "$app_path"
codesign --verify --deep --strict "$app_path"
printf 'Built self-contained local app: %s\n' "$app_path"
