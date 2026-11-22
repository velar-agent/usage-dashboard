//! Time helpers: RFC 3339 parsing, UTC offsets and local-day boundaries.
//! 시간 도우미입니다. RFC 3339 해석, UTC 오프셋, 현지 날짜 경계를 다룹니다.

use chrono::{DateTime, NaiveDate, Utc};

/// Smallest and largest UTC offsets in use anywhere, in minutes (-12:00 and +14:00).
/// 실제로 쓰이는 UTC 오프셋의 최솟값과 최댓값(분)입니다. -12:00과 +14:00입니다.
pub const MIN_OFFSET_MIN: i32 = -12 * 60;
pub const MAX_OFFSET_MIN: i32 = 14 * 60;

/// Current time as unix seconds.
/// 현재 시각을 유닉스 초로 돌려줍니다.
pub fn now_unix() -> i64 {
    Utc::now().timestamp()
}

/// Parse an RFC 3339 timestamp into unix seconds. Fractional seconds are dropped.
/// RFC 3339 시각을 유닉스 초로 바꿉니다. 소수 초는 버립니다.
pub fn parse_rfc3339(s: &str) -> Option<i64> {
    DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|dt| dt.timestamp())
}

/// Format unix seconds as an RFC 3339 UTC timestamp.
/// 유닉스 초를 RFC 3339 UTC 시각 문자열로 만듭니다.
pub fn format_rfc3339_utc(unix: i64) -> String {
    match DateTime::<Utc>::from_timestamp(unix, 0) {
        Some(dt) => dt.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        None => String::from("1970-01-01T00:00:00Z"),
    }
}

/// Parse a UTC offset. Accepts "+09:00", "-0530", "+9", "Z", "UTC" or plain minutes such as "540".
/// UTC 오프셋을 해석합니다. "+09:00", "-0530", "+9", "Z", "UTC" 또는 "540" 같은 분 단위 숫자를 받습니다.
pub fn parse_utc_offset(s: &str) -> Result<i32, String> {
    let t = s.trim();
    let bad = || format!("invalid UTC offset '{s}' (expected e.g. +09:00)");
    if t.eq_ignore_ascii_case("z") || t.eq_ignore_ascii_case("utc") {
        return Ok(0);
    }
    let (sign, rest) = match t.as_bytes().first() {
        Some(b'+') => (1, &t[1..]),
        Some(b'-') => (-1, &t[1..]),
        _ => {
            // A bare number means minutes, the unit browsers report.
            // 부호 없는 숫자는 분 단위로 봅니다. 브라우저가 알려 주는 단위입니다.
            let minutes: i32 = t.parse().map_err(|_| bad())?;
            return check_offset(minutes).ok_or_else(bad);
        }
    };
    let (hours, minutes) = if let Some((h, m)) = rest.split_once(':') {
        (h, m)
    } else if rest.len() == 4 {
        rest.split_at(2)
    } else if rest.len() > 2 {
        // A signed number longer than two digits is minutes, e.g. "-300".
        // 부호가 있고 두 자리보다 긴 숫자는 분입니다. 예: "-300".
        let minutes: i32 = rest.parse().map_err(|_| bad())?;
        return check_offset(sign * minutes).ok_or_else(bad);
    } else {
        (rest, "0")
    };
    let h: i32 = hours.parse().map_err(|_| bad())?;
    let m: i32 = minutes.parse().map_err(|_| bad())?;
    if !(0..=14).contains(&h) || !(0..60).contains(&m) {
        return Err(bad());
    }
    check_offset(sign * (h * 60 + m)).ok_or_else(bad)
}

fn check_offset(minutes: i32) -> Option<i32> {
    (MIN_OFFSET_MIN..=MAX_OFFSET_MIN)
        .contains(&minutes)
        .then_some(minutes)
}

/// Format offset minutes as "+09:00".
/// 오프셋(분)을 "+09:00" 꼴로 만듭니다.
pub fn format_utc_offset(minutes: i32) -> String {
    let sign = if minutes < 0 { '-' } else { '+' };
    let abs = minutes.abs();
    format!("{sign}{:02}:{:02}", abs / 60, abs % 60)
}

/// Parse a calendar day written as YYYY-MM-DD.
/// YYYY-MM-DD로 쓴 날짜를 해석합니다.
pub fn parse_day(s: &str) -> Option<NaiveDate> {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").ok()
}

/// Unix seconds at which the given local day starts, for a fixed UTC offset.
/// 고정 UTC 오프셋에서 해당 현지 날짜가 시작하는 유닉스 초입니다.
pub fn day_start(day: NaiveDate, offset_min: i32) -> i64 {
    let midnight = day.and_hms_opt(0, 0, 0).expect("midnight is always valid");
    midnight.and_utc().timestamp() - i64::from(offset_min) * 60
}

/// Local calendar day that contains the given instant, for a fixed UTC offset.
/// 고정 UTC 오프셋에서 해당 시각이 속하는 현지 날짜입니다.
pub fn local_day(unix: i64, offset_min: i32) -> NaiveDate {
    DateTime::<Utc>::from_timestamp(unix + i64::from(offset_min) * 60, 0)
        .map(|dt| dt.date_naive())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_rfc3339_with_fraction_and_offset() {
        assert_eq!(
            parse_rfc3339("2026-11-22T06:25:28.553Z"),
            Some(1_795_328_728)
        );
        assert_eq!(
            parse_rfc3339("2026-11-22T15:25:28+09:00"),
            Some(1_795_328_728)
        );
        assert_eq!(parse_rfc3339("2026-11-22 06:25:28"), None);
        assert_eq!(parse_rfc3339(""), None);
    }

    #[test]
    fn formats_rfc3339_utc() {
        assert_eq!(format_rfc3339_utc(1_795_328_728), "2026-11-22T06:25:28Z");
    }

    #[test]
    fn parses_utc_offsets() {
        assert_eq!(parse_utc_offset("+09:00"), Ok(540));
        assert_eq!(parse_utc_offset("-05:30"), Ok(-330));
        assert_eq!(parse_utc_offset("+0900"), Ok(540));
        assert_eq!(parse_utc_offset("+9"), Ok(540));
        assert_eq!(parse_utc_offset("Z"), Ok(0));
        assert_eq!(parse_utc_offset("utc"), Ok(0));
        assert_eq!(parse_utc_offset("540"), Ok(540));
        assert_eq!(parse_utc_offset("-300"), Ok(-300));
        assert_eq!(parse_utc_offset("0"), Ok(0));
        assert!(parse_utc_offset("+15:00").is_err());
        assert!(parse_utc_offset("+09:60").is_err());
        assert!(parse_utc_offset("9999").is_err());
        assert!(parse_utc_offset("seoul").is_err());
    }

    #[test]
    fn formats_utc_offsets() {
        assert_eq!(format_utc_offset(540), "+09:00");
        assert_eq!(format_utc_offset(-330), "-05:30");
        assert_eq!(format_utc_offset(0), "+00:00");
    }

    #[test]
    fn local_day_boundaries_follow_the_offset() {
        let day = parse_day("2026-11-22").unwrap();
        // 2026-11-22 00:00 KST is 2026-11-21 15:00 UTC.
        // 2026-11-22 00:00 KST는 2026-11-21 15:00 UTC입니다.
        let start = day_start(day, 540);
        assert_eq!(format_rfc3339_utc(start), "2026-11-21T15:00:00Z");
        assert_eq!(local_day(start, 540), day);
        assert_eq!(local_day(start - 1, 540), parse_day("2026-11-21").unwrap());
        assert_eq!(local_day(start, 0), parse_day("2026-11-21").unwrap());
    }
}
