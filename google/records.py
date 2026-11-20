"""Worker의 JSON, 문자열 길이, KST 시각 및 기록 형식."""

import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal


KST = timezone(timedelta(hours=9))
MAX_RECORD_BYTES = 4096
KNOWN = {"ts", "service", "metric", "value", "unit", "reset_at", "note", "source", "reason", "hint"}
# ECMAScript의 trim()과 정규식 공백 문자
JS_SPACE = (
    "\t\n\v\f\r \xa0\u1680" + "".join(chr(n) for n in range(0x2000, 0x200b))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]+))?)?)?[" + JS_SPACE + r"]*(Z|[+-][0-9]{2}:?[0-9]{2})?",
    re.IGNORECASE | re.ASCII,
)
LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def now_kst():
    return datetime.now(KST).isoformat(timespec="seconds")


def parse_ts(value):
    """Date.parse와 같이 생략한 시간대는 KST, 24:00과 월말 넘김도 허용해요."""
    if not isinstance(value, str):
        return None
    match = TS_RE.fullmatch(value.strip(JS_SPACE))
    if not match:
        return None
    year, month, day, hour, minute, second = (int(n or 0) for n in match.groups()[:6])
    fraction, zone = match.groups()[6:]
    ms = int(((fraction or "") + "000")[:3])
    if not (1 <= month <= 12 and 1 <= day <= 31 and minute < 60 and second < 60):
        return None
    if hour > 24 or (hour == 24 and (minute or second or ms)):
        return None
    zone = (zone or "+09:00").upper()
    offset = 0
    if zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[-2:])
        if hours > 23 or minutes > 59:
            return None
        offset = (hours * 60 + minutes) * (1 if zone[0] == "+" else -1)
    # 그레고리력의 400년 주기로 datetime 범위 밖인 0000년도 계산해요.
    base_year = 2000 + year % 400
    date = datetime(base_year, month, 1) + timedelta(
        days=day - 1, hours=hour + 9, minutes=minute - offset, seconds=second
    )
    kst_year = year + date.year - base_year
    iso_year = f"{kst_year:04}" if 0 <= kst_year <= 9999 else f"{kst_year:+07}"
    return (iso_year + date.strftime("-%m-%dT%H:%M:%S"))[:19] + "+09:00"


def reject_constant(_):
    raise ValueError("invalid JSON number")


DECODER = json.JSONDecoder(parse_int=float, parse_constant=reject_constant)


def parse_json(text):
    # JSON.parse의 숫자는 정수 입력도 IEEE 754 배정밀도로 읽어요.
    try:
        return DECODER.decode(text)
    except RecursionError:
        # 4KB 안에도 중첩 배열이 2천 개 들어가요. 파이썬 호출 스택 대신 부모를 저장해요.
        return parse_nested_json(text)


def parse_nested_json(text):
    def skip(pos):
        return json.decoder.WHITESPACE.match(text, pos).end()

    def member(pos):
        if text[pos:pos + 1] != '"':
            raise ValueError("invalid JSON key")
        key, end = DECODER.raw_decode(text, pos)
        colon = skip(end)
        if text[colon:colon + 1] != ":":
            raise ValueError("missing JSON colon")
        return key, skip(colon + 1)

    parents = []
    pos = skip(0)
    while True:
        char = text[pos:pos + 1]
        if char in ("[", "{"):
            value = [] if char == "[" else {}
            pos = skip(pos + 1)
            closing = "]" if char == "[" else "}"
            if text[pos:pos + 1] != closing:
                key, pos = member(pos) if char == "{" else (None, pos)
                parents.append((value, key))
                continue
            pos += 1
        else:
            value, pos = DECODER.raw_decode(text, pos)
        while parents:
            parent, key = parents.pop()
            if key is None:
                parent.append(value)
            else:
                parent[key] = value
            pos = skip(pos)
            separator = text[pos:pos + 1]
            if separator == ("]" if key is None else "}"):
                value = parent
                pos += 1
                continue
            if separator != ",":
                raise ValueError("invalid JSON separator")
            pos = skip(pos + 1)
            key, pos = member(pos) if key is not None else (None, pos)
            parents.append((parent, key))
            break
        else:
            if skip(pos) != len(text):
                raise ValueError("extra JSON data")
            return value


def utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def sqlite_text(text):
    # TextEncoder/D1에 바인딩할 때 짝이 없는 서로게이트는 U+FFFD가 돼요.
    return LONE_SURROGATE.sub("\ufffd", text) if text is not None else None


def number_text(value):
    if isinstance(value, int):
        return str(value)
    if value == 0:
        return "0"
    mantissa, sep, exponent = repr(value).partition("e")
    if sep and 1e-6 <= abs(value) < 1e21:
        return format(Decimal(repr(value)), "f")
    if sep:
        return f"{mantissa}e{int(exponent):+d}"
    return mantissa.removesuffix(".0")


def property_order(key):
    # JSON.stringify는 배열 인덱스에 해당하는 키를 숫자순으로 먼저 내보내요.
    if len(key) <= 10 and key.isascii() and key.isdigit() and (key == "0" or key[0] != "0"):
        return min(int(key), 0xffffffff)
    return 0xffffffff


def json_tokens(value):
    if isinstance(value, str):
        yield json.encoder.encode_basestring(value)
    elif value is None:
        yield "null"
    elif isinstance(value, bool):
        yield "true" if value else "false"
    elif isinstance(value, (int, float)):
        yield number_text(value) if math.isfinite(value) else "null"
    elif isinstance(value, list):
        yield "["
        for i, item in enumerate(value):
            if i:
                yield ","
            yield json_tokens(item)
        yield "]"
    else:
        yield "{"
        for i, key in enumerate(sorted(value, key=property_order)):
            if i:
                yield ","
            yield json.encoder.encode_basestring(key) + ":"
            yield json_tokens(value[key])
        yield "}"


def json_bytes(value):
    """4KB 판정, 저장한 data와 응답에 같은 JSON.stringify 표기를 써요."""
    tokens = []
    stack = [json_tokens(value)]
    while stack:
        token = next(stack[-1], None)
        if token is None:
            stack.pop()
        elif isinstance(token, str):
            tokens.append(token)
        else:
            stack.append(token)
    return "".join(tokens).encode("utf-8", "backslashreplace")


def string_field(raw, key, max_length, *, required=False):
    value = raw.get(key)
    if value is None:
        if required:
            raise ValueError(f"`{key}` 값이 필요해요.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"`{key}`는 문자열이어야 해요.")
    value = value.strip(JS_SPACE)
    if required and not value:
        raise ValueError(f"`{key}` 값이 비어 있어요.")
    if utf16_length(value) > max_length:
        raise ValueError(f"`{key}`는 {max_length}자 이하여야 해요.")
    return value or None


def normalize_record(raw):
    if not isinstance(raw, dict):
        raise ValueError("기록은 JSON 객체여야 해요.")
    for key in raw:
        if key.startswith("_"):
            raise ValueError(f"`{key}`처럼 밑줄(_)로 시작하는 필드는 쓸 수 없어요.")
    service = string_field(raw, "service", 64, required=True)
    metric = string_field(raw, "metric", 64, required=True)
    unit = string_field(raw, "unit", 20)
    note = string_field(raw, "note", 200)
    source = string_field(raw, "source", 100)
    reason = string_field(raw, "reason", 100)
    hint = string_field(raw, "hint", 200)
    value = raw.get("value")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("`value`는 숫자여야 해요. (예: 14 또는 7.5)")
    ts = raw.get("ts")
    if ts is None or ts == "":
        ts = ts_kst = now_kst()
    else:
        if not isinstance(ts, str):
            raise ValueError("`ts`는 ISO 8601 문자열이어야 해요. (예: 2026-11-15T15:20:00+09:00)")
        ts = ts.strip(JS_SPACE)
        ts_kst = parse_ts(ts)
        if ts_kst is None:
            raise ValueError("`ts` 형식이 올바르지 않아요. ISO 8601로 적어 주세요. (예: 2026-11-15T15:20:00+09:00)")
    reset_at = raw.get("reset_at")
    if reset_at is None or reset_at == "":
        reset_at = None
    else:
        if parse_ts(reset_at) is None:
            raise ValueError("`reset_at` 형식이 올바르지 않아요. ISO 8601로 적어 주세요. (예: 2026-11-17T15:42:00+09:00)")
        reset_at = reset_at.strip(JS_SPACE)
    data = dict(ts=ts, service=service, metric=metric, value=value)
    optional = dict(unit=unit, reset_at=reset_at, note=note, source=source, reason=reason, hint=hint)
    data.update((k, v) for k, v in optional.items() if v is not None)
    data.update((k, v) for k, v in raw.items() if k not in KNOWN)
    encoded = json_bytes(data)
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError(f"기록 하나는 {MAX_RECORD_BYTES}바이트 이하여야 해요.")
    parts = [service, metric, note or "", source or "", unit or "", number_text(value)]
    if reason:
        parts.insert(-1, reason)
    if hint:
        parts.insert(-1, hint)
    search = " ".join(parts).lower()
    dedupe_key = "|".join((ts_kst, service, metric, note or ""))
    return (
        ts_kst, sqlite_text(service), sqlite_text(metric), value, sqlite_text(unit),
        reset_at, sqlite_text(note), sqlite_text(source), encoded.decode("utf-8"),
        sqlite_text(search), sqlite_text(dedupe_key),
    )


def public_record(row):
    try:
        data = parse_json(row["data"])
    except ValueError:
        data = {}
    if isinstance(data, str):
        units = data.encode("utf-16-le", "surrogatepass")
        data = [units[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(units), 2)]
    if isinstance(data, list):
        data = {str(i): v for i, v in enumerate(data)}
    if isinstance(data, (float, bool)):
        data = {}
    item = {k: v for k, v in data.items() if not k.startswith("_")}
    if item.get("ts") is None:
        item["ts"] = row["ts_kst"]
    return item
