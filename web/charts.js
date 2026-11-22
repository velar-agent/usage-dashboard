// SVG column charts, segmented bars and the tooltip they share. Plain DOM, no libraries.
// SVG 막대 차트, 구간 막대, 그리고 이들이 함께 쓰는 툴팁입니다. 라이브러리 없이 DOM만 씁니다.
//
// Every name and label that reaches the page goes through textContent, never innerHTML: user,
// project and model names come from collectors and are not trusted.
// 화면에 들어가는 이름과 표기는 모두 textContent로 넣고 innerHTML은 쓰지 않습니다. 사용자, 프로젝트,
// 모델 이름은 수집 프로세스가 보낸 값이라 신뢰하지 않습니다.

import { niceScale } from './format.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
/** Surface-colored gap between stacked segments and between neighbouring bars, in px. */
/** 쌓인 구간 사이, 이웃한 막대 사이에 두는 바탕색 간격(px)입니다. */
const GAP = 2;
const MAX_BAR_WIDTH = 24;
const BAR_RADIUS = 4;

/** Create an HTML element. / HTML 요소를 만듭니다. */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child !== undefined && child !== null) node.append(child);
  }
  return node;
}

function svgEl(tag, attrs, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (parent) parent.appendChild(node);
  return node;
}

// ---------------------------------------------------------------------------------------------
// Tooltip
// 툴팁
// ---------------------------------------------------------------------------------------------

let tooltipNode = null;

/**
 * Show the tooltip near a viewport point. `rows` are { cls, value, name }; the value is the
 * strong element because the reader already knows the series and wants the number.
 * 화면 좌표 근처에 툴팁을 보여 줍니다. `rows`는 { cls, value, name }입니다. 읽는 사람은 이미 계열을
 * 알고 숫자를 원하므로 값을 굵게 앞세웁니다.
 */
export function showTooltip({ title, rows }, anchor) {
  if (!tooltipNode) {
    tooltipNode = el('div', { className: 'tooltip', role: 'tooltip' });
    document.body.appendChild(tooltipNode);
  }
  const tip = tooltipNode;
  tip.replaceChildren(el('div', { className: 'tooltip-title', textContent: title }));
  for (const row of rows) {
    tip.appendChild(
      el('div', { className: `tooltip-row${row.total ? ' is-total' : ''}` }, [
        el('span', { className: row.cls ? `tooltip-key bg-${row.cls}` : 'tooltip-key' }),
        el('strong', { textContent: row.value }),
        el('span', { className: 'tooltip-name', textContent: row.name }),
      ]),
    );
  }
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  const margin = 8;
  let left = anchor.x + 14;
  if (left + box.width > window.innerWidth - margin) left = anchor.x - box.width - 14;
  left = Math.max(margin, left);
  let top = anchor.y - box.height - 10;
  if (top < margin) top = anchor.y + 18;
  tip.style.left = `${Math.round(left + window.scrollX)}px`;
  tip.style.top = `${Math.round(top + window.scrollY)}px`;
}

export function hideTooltip() {
  if (tooltipNode) tooltipNode.hidden = true;
}

// ---------------------------------------------------------------------------------------------
// Column chart
// 세로 막대 차트
// ---------------------------------------------------------------------------------------------

const charts = new WeakMap();

/**
 * Draw (or redraw) a column chart in `container`. One series draws plain columns; several series
 * stack. The chart redraws itself when the container changes width.
 * `container`에 세로 막대 차트를 그립니다(다시 부르면 새로 그립니다). 계열이 하나면 단순 막대,
 * 여럿이면 쌓은 막대입니다. 컨테이너 너비가 바뀌면 스스로 다시 그립니다.
 *
 * options:
 *   label        accessible name of the chart / 차트의 접근성 이름
 *   categories   [{ label, title }] x-axis label and tooltip heading / x축 표기와 툴팁 제목
 *   series       [{ name, cls, values }] cls is a palette slot such as "s1" / cls는 "s1" 같은 팔레트 칸
 *   height       total height in px including the x-axis band / x축 영역을 포함한 전체 높이(px)
 *   integer      values are whole numbers / 값이 정수임
 *   formatTicks  (ticks) => string[] / 눈금 표기
 *   formatValue  (value) => string, used in the tooltip / 툴팁에 쓰는 값 표기
 *   tickSteps    allowed gaps between x labels, e.g. [1, 7, 14] / x축 표기 간격 후보
 *   totalName    when set, the tooltip ends with a total row / 지정하면 툴팁 끝에 합계 줄을 붙임
 */
export function columnChart(container, options) {
  let entry = charts.get(container);
  if (!entry) {
    entry = { options, width: 0 };
    entry.observer = new ResizeObserver(() => {
      const width = Math.floor(container.clientWidth);
      if (width > 0 && width !== entry.width) drawColumns(container, entry);
    });
    entry.observer.observe(container);
    charts.set(container, entry);
  }
  entry.options = options;
  drawColumns(container, entry);
}

function roundedTop(x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, w / 2, h));
  return `M${x},${y + h}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + w - radius}Q${x + w},${y} ${x + w},${y + radius}V${y + h}Z`;
}

function drawColumns(container, entry) {
  const o = entry.options;
  const width = Math.max(220, Math.floor(container.clientWidth) || 0);
  entry.width = width;
  const height = o.height ?? 220;
  const count = o.categories.length;
  const totals = o.categories.map((_, i) => o.series.reduce((sum, s) => sum + (s.values[i] || 0), 0));
  const scale = niceScale(Math.max(0, ...totals), { integer: o.integer });
  const tickLabels = o.formatTicks(scale.ticks);

  const margin = {
    top: 10,
    right: 10,
    bottom: 24,
    left: Math.max(...tickLabels.map((t) => t.length)) * 6.6 + 14,
  };
  const plotWidth = Math.max(10, width - margin.left - margin.right);
  const plotHeight = height - margin.top - margin.bottom;
  const band = plotWidth / Math.max(1, count);
  const barWidth = Math.max(1, Math.min(MAX_BAR_WIDTH, band - GAP));
  const baseline = margin.top + plotHeight;
  const yOf = (value) => baseline - (value / scale.max) * plotHeight;

  const svg = svgEl('svg', {
    class: 'chart',
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    tabindex: 0,
    'aria-label': o.label,
  });

  // Gridlines sit behind the marks: solid hairlines, with a slightly stronger baseline.
  // 격자선은 막대 뒤에 둡니다. 실선 한 줄 굵기이고 기준선만 조금 진합니다.
  scale.ticks.forEach((tick, i) => {
    const y = Math.round(yOf(tick)) + 0.5;
    svgEl('line', { class: tick === 0 ? 'chart-axis' : 'chart-grid', x1: margin.left, x2: width - margin.right, y1: y, y2: y }, svg);
    const text = svgEl('text', { class: 'chart-tick', x: margin.left - 8, y, 'text-anchor': 'end', 'dominant-baseline': 'central' }, svg);
    text.textContent = tickLabels[i];
  });

  const hoverBand = svgEl('rect', { class: 'chart-hover', x: 0, y: margin.top, width: Math.max(1, band), height: plotHeight, visibility: 'hidden' }, svg);

  for (let i = 0; i < count; i += 1) {
    const x = margin.left + i * band + (band - barWidth) / 2;
    const present = o.series.map((s, k) => ((s.values[i] || 0) > 0 ? k : -1)).filter((k) => k >= 0);
    let stackedValue = 0;
    for (const k of present) {
      const value = o.series[k].values[i];
      let bottom = yOf(stackedValue);
      let top = yOf(stackedValue + value);
      stackedValue += value;
      // Every segment above the first gives up GAP px at its bottom so the surface shows through.
      // 맨 아래를 뺀 구간은 아래쪽을 GAP px만큼 비워 바탕색이 보이게 합니다.
      if (k !== present[0] && bottom - top > GAP + 1) bottom -= GAP;
      // A non-zero value never vanishes: it keeps at least one pixel.
      // 0이 아닌 값은 사라지지 않도록 최소 1픽셀을 유지합니다.
      if (bottom - top < 1) top = bottom - 1;
      const isTop = k === present[present.length - 1];
      svgEl('path', { class: `fill-${o.series[k].cls}`, d: roundedTop(x, top, barWidth, bottom - top, isTop ? BAR_RADIUS : 0) }, svg);
    }
  }

  // X labels: the smallest allowed step at which neighbours do not touch.
  // x축 표기는 이웃끼리 닿지 않는 가장 작은 간격으로 둡니다.
  const longest = Math.max(1, ...o.categories.map((c) => c.label.length));
  const labelWidth = longest * 6.6 + 14;
  const needed = Math.ceil(labelWidth / band);
  const steps = o.tickSteps ?? [1, 2, 5, 10, 20, 50, 100];
  const step = steps.find((s) => s >= needed) ?? steps[steps.length - 1] * Math.ceil(needed / steps[steps.length - 1]);
  for (let i = 0; i < count; i += step) {
    const center = margin.left + (i + 0.5) * band;
    const x = Math.min(Math.max(center, labelWidth / 2), width - labelWidth / 2 + 6);
    const text = svgEl('text', { class: 'chart-tick', x, y: baseline + 15, 'text-anchor': 'middle' }, svg);
    text.textContent = o.categories[i].label;
  }

  // Interaction: the pointer only has to be over a column's band, never on the painted bar.
  // 상호작용: 포인터가 색칠된 막대 위가 아니라 그 열의 구역 안에만 있으면 됩니다.
  let active = -1;
  const activate = (index) => {
    active = index;
    if (index < 0 || index >= count) {
      hoverBand.setAttribute('visibility', 'hidden');
      hideTooltip();
      return;
    }
    hoverBand.setAttribute('x', margin.left + index * band);
    hoverBand.setAttribute('visibility', 'visible');
    const rows = o.series.map((s) => ({ cls: s.cls, value: o.formatValue(s.values[index] || 0), name: s.name }));
    if (o.totalName) rows.push({ value: o.formatValue(totals[index]), name: o.totalName, total: true });
    const box = svg.getBoundingClientRect();
    showTooltip(
      { title: o.categories[index].title, rows },
      { x: box.left + margin.left + (index + 0.5) * band, y: box.top + yOf(totals[index]) },
    );
  };
  const indexAt = (event) => {
    const box = svg.getBoundingClientRect();
    const index = Math.floor((event.clientX - box.left - margin.left) / band);
    return index >= 0 && index < count ? index : -1;
  };
  svg.addEventListener('pointermove', (event) => activate(indexAt(event)));
  svg.addEventListener('pointerdown', (event) => activate(indexAt(event)));
  svg.addEventListener('pointerleave', () => activate(-1));
  svg.addEventListener('blur', () => activate(-1));
  svg.addEventListener('focus', () => {
    // Keyboard focus starts on the most recent column that has data.
    // 키보드 포커스는 데이터가 있는 가장 최근 열에서 시작합니다.
    if (!svg.matches(':focus-visible') || count === 0) return;
    let start = count - 1;
    while (start > 0 && totals[start] === 0) start -= 1;
    activate(start);
  });
  svg.addEventListener('keydown', (event) => {
    if (count === 0) return;
    const current = active < 0 ? count - 1 : active;
    const moves = { ArrowLeft: current - 1, ArrowRight: current + 1, Home: 0, End: count - 1 };
    if (event.key === 'Escape') {
      activate(-1);
    } else if (event.key in moves) {
      event.preventDefault();
      activate(Math.min(count - 1, Math.max(0, moves[event.key])));
    }
  });

  container.replaceChildren(svg);
}

// ---------------------------------------------------------------------------------------------
// Tall segmented bar
// 높은 구간 막대
// ---------------------------------------------------------------------------------------------

/**
 * One whole split into parts. Each part prints its share inside when the text fits; names live
 * in the legend, and hover or focus gives the name, amount and share.
 * 전체 하나를 부분으로 나눈 막대입니다. 글자가 들어가면 각 부분 안에 비중을 적습니다. 이름은 범례에
 * 두고, 마우스를 올리거나 포커스를 주면 이름, 양, 비중을 알려 줍니다.
 *
 * parts: [{ name, cls, value, display }] where display is the formatted amount.
 * parts는 [{ name, cls, value, display }]이고 display는 표기용으로 만든 양입니다.
 */
export function segmentedBar(container, { label, parts, formatShare }) {
  const total = parts.reduce((sum, part) => sum + Math.max(0, part.value), 0);
  const bar = el('div', { className: 'segbar', role: 'img' });
  if (total <= 0) {
    bar.classList.add('is-empty');
    bar.setAttribute('aria-label', `${label}: 기록 없음`);
    container.replaceChildren(bar);
    return;
  }
  const described = [];
  const labels = [];
  for (const part of parts) {
    if (!(part.value > 0)) continue;
    const share = formatShare(part.value / total);
    described.push(`${part.name} ${share} (${part.display})`);
    const text = el('span', { className: 'seg-label', textContent: share });
    const segment = el('div', { className: `seg bg-${part.cls}`, tabIndex: 0 }, [text]);
    segment.style.flexGrow = String(part.value / total);
    segment.setAttribute('aria-label', `${part.name} ${share}, ${part.display}`);
    const show = () => {
      const box = segment.getBoundingClientRect();
      showTooltip(
        { title: label, rows: [{ cls: part.cls, value: `${part.display} · ${share}`, name: part.name }] },
        { x: box.left + Math.min(box.width / 2, 40), y: box.top },
      );
    };
    segment.addEventListener('pointerenter', show);
    segment.addEventListener('pointerleave', hideTooltip);
    segment.addEventListener('focus', show);
    segment.addEventListener('blur', hideTooltip);
    labels.push([segment, text]);
    bar.appendChild(segment);
  }
  bar.setAttribute('aria-label', `${label}: ${described.join(', ')}`);
  container.replaceChildren(bar);

  // A share is printed only where it fits with padding; otherwise the block stays blank and the
  // table beside the bar carries the number.
  // 비중은 여백을 두고 들어갈 때만 적습니다. 들어가지 않으면 구간을 비워 두고, 막대 옆의 표가
  // 그 숫자를 보여 줍니다.
  const fit = () => {
    for (const [segment, text] of labels) {
      text.hidden = false;
      text.hidden = text.offsetWidth + 14 > segment.clientWidth;
    }
  };
  fit();
  let entry = charts.get(container);
  if (entry?.observer) entry.observer.disconnect();
  entry = { observer: new ResizeObserver(fit) };
  entry.observer.observe(container);
  charts.set(container, entry);
}
