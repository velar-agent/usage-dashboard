"""Worker와 같은 스키마를 쓰는 로컬 SQLite 저장소."""

import os
import sqlite3
import threading
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path


GOOGLE_DIR = Path(__file__).resolve().parent
WORKER_DIR = GOOGLE_DIR.parent / "worker"
DEFAULT_DB_PATH = GOOGLE_DIR / "data" / "usage.sqlite3"
RECORD_COLUMNS = (
    "ts_kst", "service", "metric", "value", "unit", "reset_at", "note",
    "source", "data", "search", "dedupe_key", "ingested_at",
)
RAW_COLUMNS = ("name", "content", "size", "created_at")


def db_path():
    return Path(os.environ.get("DB_PATH") or DEFAULT_DB_PATH).expanduser()


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.write_lock = threading.Lock()
        schema = (WORKER_DIR / "schema.sql").read_text(encoding="utf-8")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("BEGIN IMMEDIATE;\n" + schema)

    def connect(self):
        # timeout은 초 단위이므로 DB 잠금이 풀릴 때까지 최대 10초 기다려요.
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self, *, write=False):
        # 읽기는 같은 시점의 스냅샷을 보고, 쓰기는 한 번에 하나씩 커밋해요.
        with self.write_lock if write else nullcontext():
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
