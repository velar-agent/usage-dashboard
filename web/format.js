// Number, money, date and axis formatting shared by the dashboard. No DOM access here, so the
// functions can be exercised outside a browser.
// 대시보드가 함께 쓰는 숫자, 금액, 날짜, 축 눈금 형식입니다. DOM을 쓰지 않으므로 브라우저 밖에서도
// 함수를 실행해 볼 수 있습니다.

const fullNumber = new Intl.NumberFormat('ko-KR');
const WEEKDAYS = ['일', '월', '화', '수', '목', '금', '토'];

/** A whole number with thousands separators: 1234567 -> "1,234,567". */
/** 천 단위 구분 기호를 넣은 정수입니다. 1234567은 "1,234,567"이 됩니다. */
export function full(n) {
  return fullNumber.format(Math.round(n));
}

function trimZeros(text) {
  return text.includes('.') ? text.replace(/\.?0+$/, '') : text;
}

/**
 * Three significant digits with K/M/B/T, the units token prices are quoted in.
 * 유효 숫자 세 자리와 K/M/B/T 단위로 줄여 씁니다. 토큰 단가를 말할 때 쓰는 단위입니다.
 * 1234 -> "1.23K", 89137896 -> "89.1M"
 */
export function compact(n) {
  const abs = Math.abs(n);
  if (abs < 999.5) return String(Math.round(n));
  const units = [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
  for (const [size, suffix] of units) {
    // 0.9995 keeps 999,950 from printing as "1000K".
    // 0.9995를 곱해 999,950이 "1000K"로 찍히지 않게 합니다.
    if (abs >= size * 0.9995) {
      const value = n / size;
      const digits = Math.abs(value) >= 99.95 ? 0 : Math.abs(value) >= 9.995 ? 1 : 2;
      return trimZeros(value.toFixed(digits)) + suffix;
    }
  }
  return String(Math.round(n));
}

/** US dollars with more decimals for smaller amounts. / 금액이 작을수록 소수 자리를 늘린 미국 달러입니다. */
export function usd(amount) {
  const abs = Math.abs(amount);
  let digits = 2;
  if (abs >= 1000) digits = 0;
  else if (abs > 0 && abs < 0.01) digits = 4;
  else if (abs > 0 && abs < 1) digits = 3;
  const text = abs.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (amount < 0 ? '-$' : '$') + text;
}

/** A ratio as a percentage: 0.9547 -> "95.5%". */
/** 비율을 백분율로 적습니다. 0.9547은 "95.5%"가 됩니다. */
export function percent(ratio, digits = 1) {
  return `${(ratio * 100).toFixed(digits)}%`;
}

/**
 * Axis ticks from zero to a round number at or above `max`.
 * 0부터 `max` 이상인 깔끔한 수까지의 축 눈금입니다.
 */
export function niceScale(max, { count = 4, integer = false } = {}) {
  if (!(max > 0)) return { max: 1, ticks: [0, 1] };
  const rough = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  let step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough * (1 - 1e-9));
  if (integer) step = Math.max(1, Math.ceil(step - 1e-9));
  const steps = Math.max(1, Math.ceil(max / step - 1e-9));
  const ticks = [];
  for (let i = 0; i <= steps; i += 1) ticks.push(Number((i * step).toPrecision(12)));
  return { max: ticks[ticks.length - 1], ticks };
}

/** Dollar tick labels that keep just enough decimals to tell neighbours apart. */
/** 이웃 눈금을 구별할 만큼만 소수 자리를 둔 달러 눈금 표기입니다. */
export function usdTicks(ticks) {
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  // The fewest decimals (up to six) that write the step exactly: 0.25 -> 2, 5 -> 0, 0.002 -> 3.
  // 눈금 간격을 정확히 적을 수 있는 가장 적은 소수 자릿수(최대 6)입니다. 0.25는 2, 5는 0, 0.002는 3입니다.
  let digits = 0;
  while (digits < 6 && Math.abs(step * 10 ** digits - Math.round(step * 10 ** digits)) > 1e-9) digits += 1;
  return ticks.map((t) => {
    if (t >= 1000) return `$${compact(t)}`;
    return `$${t.toLocaleString('en-US', { minimumFractionDigits: t === 0 ? 0 : digits, maximumFractionDigits: digits })}`;
  });
}

// --- Calendar days as "YYYY-MM-DD" strings. Day arithmetic runs in UTC so that daylight saving
// --- changes in the viewer's zone can never skip or repeat a day.
// --- 날짜는 "YYYY-MM-DD" 문자열로 다룹니다. 날짜 계산은 UTC로 하므로, 보는 사람 시간대의 서머타임
// --- 전환 때문에 하루가 빠지거나 겹치지 않습니다.

const pad2 = (n) => String(n).padStart(2, '0');

function toUtcDate(day) {
  const [y, m, d] = day.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

/** Today in the viewer's own time zone. / 보는 사람 시간대의 오늘 날짜입니다. */
export function todayLocal(now = new Date()) {
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
}

export function addDays(day, count) {
  const date = toUtcDate(day);
  date.setUTCDate(date.getUTCDate() + count);
  return date.toISOString().slice(0, 10);
}

/** Inclusive number of days from `from` to `to`. / `from`부터 `to`까지의 날수(양 끝 포함)입니다. */
export function daysBetween(from, to) {
  return Math.round((toUtcDate(to) - toUtcDate(from)) / 86400000) + 1;
}

export function weekdayOf(day) {
  return WEEKDAYS[toUtcDate(day).getUTCDay()];
}

/** Monday of the week that contains `day`. / `day`가 속한 주의 월요일입니다. */
export function weekStart(day) {
  const weekday = toUtcDate(day).getUTCDay();
  return addDays(day, -((weekday + 6) % 7));
}

/** A short day for axis labels: "2026-11-22" -> "11/22". */
/** 축 표기에 쓰는 짧은 날짜입니다. "2026-11-22"는 "11/22"가 됩니다. */
export function shortDay(day) {
  const [, m, d] = day.split('-').map(Number);
  return `${m}/${d}`;
}

/** A full day with its weekday for tooltips and table rows. */
/** 툴팁과 표에 쓰는, 요일이 붙은 전체 날짜입니다. "2026-11-22"는 "2026년 11월 22일 (일)"이 됩니다. */
export function longDay(day) {
  const [y, m, d] = day.split('-').map(Number);
  return `${y}년 ${m}월 ${d}일 (${weekdayOf(day)})`;
}

/** Unix seconds -> "11/22 15:25" in the viewer's zone. / 유닉스 초를 보는 사람 시간대의 "11/22 15:25"로 바꿉니다. */
export function shortDateTime(unixSeconds) {
  const date = new Date(unixSeconds * 1000);
  return `${date.getMonth() + 1}/${date.getDate()} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** A length of time in the largest two units that matter. */
/** 시간 길이를 의미 있는 큰 단위 두 개로 적습니다. 4980초는 "1시간 23분"이 됩니다. */
export function duration(seconds) {
  if (seconds < 60) return '1분 미만';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}분`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours}시간 ${rest}분` : `${hours}시간`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}일 ${restHours}시간` : `${days}일`;
}

/** Minutes east of UTC -> "UTC+09:00". / UTC 기준 동쪽 분을 "UTC+09:00"으로 바꿉니다. */
export function utcOffsetLabel(minutes) {
  const sign = minutes < 0 ? '-' : '+';
  const abs = Math.abs(minutes);
  return `UTC${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`;
}
