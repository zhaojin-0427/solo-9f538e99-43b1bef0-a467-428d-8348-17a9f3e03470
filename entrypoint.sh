#!/bin/sh
set -e

DB_FILE="${DB_PATH:-/data/festival.db}"
SAMPLE_FILE="${SAMPLE_JSON:-/app/data/sample_data.json}"
mkdir -p "$(dirname "$DB_FILE")"

# 首次启动（库中还没有作品）时自动建表并载入内置样例
NEED_SEED=0
if [ ! -s "$DB_FILE" ]; then
  NEED_SEED=1
else
  CNT=$(DB_PATH="$DB_FILE" python - <<'PY'
import os
import sqlite3
from pathlib import Path
path = os.environ.get("DB_PATH", "/data/festival.db")
try:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    print(con.execute("SELECT COUNT(*) FROM works").fetchone()[0])
    con.close()
except Exception:
    print(0)
PY
)
  if [ "${CNT:-0}" = "0" ]; then NEED_SEED=1; fi
fi

if [ "$NEED_SEED" = "1" ]; then
  echo "[entrypoint] 初始化数据库 ..."
  python -m app.cli init
  if [ -f "$SAMPLE_FILE" ]; then
    echo "[entrypoint] 载入样例 $SAMPLE_FILE ..."
    python -m app.cli load "$SAMPLE_FILE" --replace || \
      echo "[entrypoint] 警告：样例载入失败，可稍后在 /admin/load 手动载入"
  else
    echo "[entrypoint] 未找到样例文件 $SAMPLE_FILE，跳过自动载入"
  fi
else
  echo "[entrypoint] 数据库已有数据（作品数=$CNT），跳过自动载入"
fi

exec "$@"
