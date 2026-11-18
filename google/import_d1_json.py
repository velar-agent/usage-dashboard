#!/usr/bin/env python3
"""D1의 SELECT 결과를 id와 원래 열 값 그대로 가져와요."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from storage import Database, RAW_COLUMNS, RECORD_COLUMNS, db_path


TABLES = {"records": (("id", *RECORD_COLUMNS), "dedupe_key"), "raw_files": (RAW_COLUMNS, "name")}
# SQL 식별자는 스키마의 고정 목록에서만 가져와요. 행의 값은 모두 바인딩해요.
INSERT_SQL = {
    table: (f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(':' + col for col in columns)}) "
            f"ON CONFLICT({unique_key}) DO NOTHING")
    for table, (columns, unique_key) in TABLES.items()
}


class ExportReader:
    """작은 VM에서 덤프 전체를 메모리에 올리지 않고 한 행씩 읽어요."""

    def __init__(self, file):
        self.file = file
        self.decoder = json.JSONDecoder()
        self.buffer = ""
        self.pos = 0

    def refill(self):
        tail = self.buffer[self.pos:]
        chunk = self.file.read(max(64 * 1024, len(tail)))
        if not chunk:
            return False
        self.buffer = tail + chunk
        self.pos = 0
        return True

    def peek(self):
        while True:
            self.pos = json.decoder.WHITESPACE.match(self.buffer, self.pos).end()
            if self.pos < len(self.buffer):
                return self.buffer[self.pos]
            if not self.refill():
                return ""

    def take(self, char):
        if self.peek() == char:
            self.pos += 1
            return True
        return False

    def expect(self, char):
        if not self.take(char):
            raise ValueError("JSON 덤프 형식이 올바르지 않아요.")

    def value(self, opening):
        if self.peek() != opening:
            raise ValueError("표 이름이나 행이 올바르지 않아요.")
        while True:
            try:
                value, self.pos = self.decoder.raw_decode(self.buffer, self.pos)
                return value
            except json.JSONDecodeError as exc:
                # 미완성 문자열·리터럴·유니코드 이스케이프는 다음 블록에서 이어져요.
                if not exc.msg.startswith("Unterminated string") and exc.pos + 6 < len(self.buffer):
                    raise
                if not self.refill():
                    raise

    def rows(self):
        self.expect("[")
        if self.take("]"):
            return
        while True:
            yield self.value("{")
            if self.take("]"):
                return
            self.expect(",")


def import_dump(source, destination):
    counts = {}
    with source.open(encoding="utf-8-sig") as file, Database(destination).transaction(write=True) as db:
        reader = ExportReader(file)
        reader.expect("{")
        while True:
            name = reader.value('"')
            if name not in TABLES or name in counts:
                raise ValueError("records와 raw_files 배열이 각각 하나씩 필요해요.")
            reader.expect(":")
            keys = set(TABLES[name][0])
            inserted = received = 0
            for row in reader.rows():
                if row.keys() != keys or (name == "records" and type(row["id"]) is not int):
                    raise ValueError("원래 id를 포함한 SELECT * 결과가 필요해요.")
                inserted += db.execute(INSERT_SQL[name], row).rowcount
                received += 1
            counts[name] = (inserted, received - inserted)
            if reader.take("}"):
                break
            reader.expect(",")
        if counts.keys() != TABLES.keys() or reader.peek():
            raise ValueError("두 표를 담은 JSON 객체 하나가 필요해요.")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=Path)
    parser.add_argument("--db", "--db-path", type=Path, default=db_path(), help="대상 SQLite 파일 (기본 DB_PATH)")
    args = parser.parse_args()
    try:
        counts = import_dump(args.json_file, args.db)
    except (OSError, ValueError, OverflowError, sqlite3.Error) as exc:
        sys.exit(f"가져오기 실패 ({type(exc).__name__}). JSON의 열, id 충돌, DB 경로를 확인해 주세요.")
    for table in TABLES:
        inserted, ignored = counts[table]
        print(f"{table} inserted={inserted} ignored={ignored}")


if __name__ == "__main__":
    main()
