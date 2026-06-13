from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from server import DB_PATH


backup_dir = Path(__file__).resolve().parent / "backups"
backup_dir.mkdir(exist_ok=True)
target = backup_dir / f"teamflow-{datetime.now():%Y%m%d-%H%M%S}.db"

with sqlite3.connect(DB_PATH) as source, sqlite3.connect(target) as destination:
    source.backup(destination)

print(target)
