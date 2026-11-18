#!/usr/bin/env python3
"""수집 키와 원본 열람 비밀번호를 전용 디렉터리에 만들어요."""

import argparse
import fcntl
import hashlib
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

from storage import DEFAULT_DB_PATH


DEFAULT_ENV = Path.home() / ".config" / "usage-dashboard-google-dev" / "env"
ITERATIONS = 10000


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        os.fchmod(fd, 0o600)
        file.write(text)
        file.flush()
        os.fsync(file.fileno())


def generate(path, force=False, db_path=None, port=None):
    path = path.expanduser().absolute()
    password_file = path.with_name("raw-password.txt")
    if path == password_file:
        raise ValueError("env 파일과 비밀번호 파일은 이름이 달라야 해요.")
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(dir_fd, fcntl.LOCK_EX)
        for target in (path, password_file):
            if os.path.lexists(target):
                if not force:
                    raise FileExistsError("기존 파일이 있어요. 두 파일을 교체하려면 --force를 지정하세요.")
                if not stat.S_ISREG(target.lstat().st_mode):
                    raise ValueError("일반 파일만 교체할 수 있어요.")
        if os.fstat(dir_fd).st_mode & stat.S_ISVTX or parent == Path("/"):
            raise ValueError("설정 파일을 보관할 전용 디렉터리를 지정하세요.")
        os.fchmod(dir_fd, 0o700)
        password = secrets.token_urlsafe(24)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
        values = {
            "INGEST_KEY": secrets.token_urlsafe(32), "SESSION_SECRET": secrets.token_urlsafe(48),
            "RAW_SALT": salt.hex(), "RAW_HASH": digest.hex(), "RAW_ITERATIONS": ITERATIONS,
            "DB_PATH": db_path if db_path is not None else DEFAULT_DB_PATH,
            "HOST": "127.0.0.1", "PORT": port if port is not None else 8787,
        }
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            stage = Path(temp)
            write_private(stage / "env", "".join(f"{key}={value}\n" for key, value in values.items()))
            write_private(stage / "raw-password.txt", password + "\n")
            if force:
                previous = stage / "previous-env"
                if path.exists():
                    os.link(path, previous)
                os.replace(stage / "env", path)
                try:
                    os.replace(stage / "raw-password.txt", password_file)
                except OSError:
                    if previous.exists():
                        os.replace(previous, path)
                    else:
                        path.unlink()
                    raise
            else:
                # link는 생성 중 다른 파일이 생겨도 덮어쓰지 않아요. env를 마지막에 공개해요.
                os.link(stage / "raw-password.txt", password_file)
                try:
                    os.link(stage / "env", path)
                except OSError:
                    password_file.unlink()
                    raise
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_ENV, help="전용 디렉터리 안의 env 경로")
    parser.add_argument("--force", action="store_true", help="기존 키와 비밀번호를 모두 교체")
    parser.add_argument("--db-path", type=Path, default=None, help="env에 기록할 DB_PATH (기본값은 google/data/usage.sqlite3)")
    parser.add_argument("--port", type=int, default=None, help="env에 기록할 PORT (기본값 8787)")
    args = parser.parse_args()
    try:
        generate(args.path, args.force, args.db_path, args.port)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
    print("env와 raw-password.txt를 권한 600으로 저장했어요.")


if __name__ == "__main__":
    main()
