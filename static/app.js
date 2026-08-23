const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat('fa-IR');
const pct = new Intl.NumberFormat('fa-IR', { maximumFractionDigits: 4 });
let selectedKarat = 18;
let selectedModelType = 'short';
let loading = false;

function money(value) { return `${fmt.format(value)} تومان`; }
function localTime(iso) {
  return new Intl.DateTimeFormat('fa-IR', { dateStyle: 'short', timeStyle: 'medium', timeZone: 'Asia/Tehran' }).format(new Date(iso));
}
const shortHorizons = [['15','۱۵ دقیقه'],['30','۳۰ دقیقه'],['60','۱ ساعت'],['240','۴ ساعت']];
const longHorizons = [['daily','روزانه'],['weekly','هفتگی'],['monthly','ماهانه']];
function setHorizons() {
  const options = selectedModelType === 'short' ? shortHorizons : longHorizons;
  $('horizon').innerHTML = options.map(([value,label], index) => `<option value="${value}" ${index === (selectedModelType === 'short' ? 2 : 0) ? 'selected' : ''}>${label}</option>`).join('');
}

function chartTimeLabel(point, compact = false) {
  if (typeof point.timestamp === 'number') {
    return new Intl.DateTimeFormat('fa-IR', compact
      ? { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tehran' }
      : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Tehran' }
    ).format(new Date(point.timestamp));
  }
  return String(point.timestamp || point.date || '').replaceAll('/', '⁄');
}

function drawChart(points) {
  const svg = $('chart');
  const width = 960, height = 330, px = 32, py = 24, chartBottom = 282;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const values = points.map(p => p.price);
  let min = Math.min(...values), max = Math.max(...values);
  const padding = (max - min || max * .03) * .12;
  min -= padding; max += padding;
  const x = i => px + i * (width - 2 * px) / Math.max(1, points.length - 1);
  const y = v => py + (max - v) * (chartBottom - py) / (max - min);
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.price).toFixed(1)}`).join(' ');
  const area = `${line} L${x(points.length - 1)},${chartBottom} L${x(0)},${chartBottom} Z`;
  const split = points.findIndex(p => p.mode === 'theoretical');
  const grid = [0, .25, .5, .75, 1].map(t => {
    const gy = py + t * (chartBottom - py), value = max - t * (max - min);
    return `<line x1="${px}" y1="${gy}" x2="${width-px}" y2="${gy}" class="grid"/><text x="${width-px}" y="${gy-7}" class="axis">${fmt.format(Math.round(value/1000))}هزار</text>`;
  }).join('');
  const divider = split > 0 ? `<line x1="${x(split)}" y1="${py}" x2="${x(split)}" y2="${chartBottom}" class="divider"/><text x="${x(split)+8}" y="${py+14}" class="axis">شروع نرخ نظری</text>` : '';
  const labelIndexes = [...new Set([0, Math.round((points.length-1)*.25), Math.round((points.length-1)*.5), Math.round((points.length-1)*.75), points.length-1])];
  const timeAxis = labelIndexes.map((index, position) => `<text x="${x(index)}" y="${chartBottom+25}" class="time-axis" text-anchor="${position === 0 ? 'start' : position === labelIndexes.length-1 ? 'end' : 'middle'}">${chartTimeLabel(points[index], true)}</text>`).join('');
  svg.innerHTML = `<defs><linearGradient id="goldFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d4a72c" stop-opacity=".34"/><stop offset="1" stop-color="#d4a72c" stop-opacity="0"/></linearGradient></defs>${grid}<path d="${area}" fill="url(#goldFill)"/><path d="${line}" class="line"/>${divider}${timeAxis}<circle cx="${x(points.length-1)}" cy="${y(values.at(-1))}" r="5" class="dot"/><g id="chartCrosshair" class="crosshair" visibility="hidden"><line id="crossV" y1="${py}" y2="${chartBottom}"/><line id="crossH" x1="${px}" x2="${width-px}"/><circle id="crossDot" r="6"/><g id="chartTooltip" class="chart-tooltip"><rect width="190" height="55" rx="9"/><text id="tooltipPrice" x="178" y="22" text-anchor="end"></text><text id="tooltipTime" x="178" y="42" text-anchor="end"></text></g></g><rect id="chartOverlay" x="${px}" y="${py}" width="${width-2*px}" height="${chartBottom-py}" fill="transparent"/>`;
  const crosshair = svg.querySelector('#chartCrosshair');
  const crossV = svg.querySelector('#crossV'), crossH = svg.querySelector('#crossH'), crossDot = svg.querySelector('#crossDot');
  const tooltip = svg.querySelector('#chartTooltip'), tooltipPrice = svg.querySelector('#tooltipPrice'), tooltipTime = svg.querySelector('#tooltipTime');
  const overlay = svg.querySelector('#chartOverlay');
  overlay.addEventListener('pointermove', event => {
    const rect = svg.getBoundingClientRect();
    const svgX = (event.clientX - rect.left) / rect.width * width;
    const index = Math.max(0, Math.min(points.length - 1, Math.round((svgX - px) / (width - 2*px) * (points.length - 1))));
    const cx = x(index), cy = y(points[index].price);
    crosshair.setAttribute('visibility', 'visible');
    crossV.setAttribute('x1', cx); crossV.setAttribute('x2', cx);
    crossH.setAttribute('y1', cy); crossH.setAttribute('y2', cy);
    crossDot.setAttribute('cx', cx); crossDot.setAttribute('cy', cy);
    const tipX = cx > width - 215 ? cx - 200 : cx + 10;
    const tipY = Math.max(py + 5, Math.min(chartBottom - 60, cy - 62));
    tooltip.setAttribute('transform', `translate(${tipX},${tipY})`);
    tooltipPrice.textContent = money(points[index].price);
    tooltipTime.textContent = chartTimeLabel(points[index]);
  });
  overlay.addEventListener('pointerleave', () => crosshair.setAttribute('visibility', 'hidden'));
}

function renderMarketAnalysis(analysis) {
  const bubble = analysis.bubble, stance = analysis.stance;
  $('marketAnalysis').classList.remove('hidden');
  $('bubblePercent').className = `analysis-value ${bubble.type}`;
  $('bubblePercent').textContent = `${bubble.percent >= 0 ? '+' : ''}${pct.format(bubble.percent)}٪`;
  $('bubbleAmount').textContent = `${bubble.type === 'positive' ? 'حباب مثبت' : bubble.type === 'negative' ? 'حباب منفی' : 'بدون حباب'}: ${money(Math.abs(bubble.amount))}`;
  $('theoreticalPrice').textContent = money(bubble.theoreticalPrice);
  $('stanceTitle').className = `analysis-value stance-${stance.id}`;
  $('stanceTitle').textContent = stance.title;
  $('stanceConfidence').textContent = `قدرت جمع‌بندی: ${fmt.format(stance.confidence)}٪ · امتیاز ${pct.format(stance.score)}`;
  $('stanceReasons').innerHTML = stance.reasons.slice(0, 5).map(reason => `<li>${reason}</li>`).join('');
  $('resistanceLevels').innerHTML = analysis.resistances.map(level => `<div><span>R${fmt.format(level.level)}</span><b>${money(level.price)}</b><small>+${pct.format(level.distancePercent)}٪</small></div>`).join('');
  $('upsideBreak').textContent = `${money(analysis.breakLevels.upside)} (+${pct.format(analysis.breakLevels.upsideDistancePercent)}٪)`;
  $('downsideBreak').textContent = `${money(analysis.breakLevels.downside)} (${pct.format(analysis.breakLevels.downsideDistancePercent)}٪)`;
  $('lowerResistanceLevels').innerHTML = analysis.lowerResistances.map(level => `<div><span>LR${fmt.format(level.level)}</span><b>${money(level.price)}</b><small>${pct.format(level.distancePercent)}٪</small></div>`).join('');
  renderTradePlan(analysis.tradePlan);
}

function renderTradePlan(plan) {
  $('tradePlan').classList.remove('hidden');
  $('tradePlanSummary').textContent = plan.summary;
  const rows = (levels, prefix) => levels.map(level => `<div><span>${prefix}${fmt.format(level.level)}</span><b>${money(level.price)}</b><small>${pct.format(level.distancePercent)}٪</small><em>${fmt.format(level.allocationPercent)}٪ سرمایه</em></div>`).join('');
  $('entryLevels').innerHTML = rows(plan.entries, 'B');
  $('exitLevels').innerHTML = rows(plan.exits, 'S');
  $('stopLoss').textContent = money(plan.stopLoss);
  $('stopLossDistance').textContent = `${pct.format(plan.stopLossDistancePercent)}٪ نسبت به قیمت جاری`;
}

async function load() {
  if (loading) return;
  loading = true;
  $('status').className = 'status';
  $('status').textContent = selectedModelType === 'short' ? 'در حال دریافت تیک‌ها، ساخت کندل‌های ۵ دقیقه‌ای و اجرای آزمون…' : 'در حال همگام‌سازی تاریخچه چندساله طلا، دلار و اونس…';
  $('refresh').disabled = true;
  try {
    const horizon = $('horizon').value;
    const endpoint = selectedModelType === 'short' ? `/api/intraday?karat=${selectedKarat}&horizon=${horizon}` : `/api/longterm?karat=${selectedKarat}&horizon=${horizon}`;
    const response = await fetch(endpoint);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'خطای ناشناخته');
    $('currentPrice').textContent = money(data.latest.price);
    $('latestDate').textContent = selectedModelType === 'short' ? `آخرین تیک: ${localTime(data.latest.timestamp)}` : `آخرین روز داده: ${data.latest.timestamp}`;
    const quality = data.dataQuality || {};
    if (selectedModelType === 'short') {
      $('directTime').textContent = quality.sourceMode && quality.sourceMode !== 'live'
        ? `⚠ اتصال زنده ناقص؛ آخرین داده معتبر ذخیره‌شده · سن داده: ${pct.format(quality.ageMinutes || 0)} دقیقه`
        : quality.isFresh
          ? 'نرخ مستقیم و تازه بازار ایران'
          : `آخرین داده منتشرشده منبع · سن داده: ${pct.format(quality.ageMinutes || 0)} دقیقه`;
    } else {
      const freshness = quality.isFresh ? 'تازه' : quality.validForForecast ? 'آخرین داده معتبر بازار' : '⚠ داده منقضی';
      $('directTime').textContent = `${freshness} · آخرین تیک ادغام‌شده: ${quality.latestQuoteAt ? localTime(quality.latestQuoteAt) : data.latest.timestamp} · سن: ${pct.format(quality.ageHours || 0)} ساعت`;
    }
    const consensus = quality.sourceConsensus || {};
    if (consensus.available) {
      const consensusState = consensus.valid ? (consensus.warning ? 'هشدار اختلاف' : 'تطبیق معتبر') : 'اختلاف نامعتبر';
      $('directTime').textContent += ` · ${consensusState} دلار: ${pct.format(consensus.usdDisagreementPercent)}٪`;
    }
    $('marketMode').className = `mode ${data.mode}`;
    $('marketMode').textContent = quality.sourceMode && quality.sourceMode !== 'live'
      ? 'داده ذخیره‌شده'
      : selectedModelType === 'long' ? 'چندساله + آخرین تیک' : (data.mode === 'direct' ? 'مستقیم' : 'نظری ۲۴ساعته');
    $('forecastTitle').textContent = selectedModelType === 'short' ? `برآورد ${fmt.format(data.horizonMinutes)} دقیقه آینده` : `برآورد ${data.horizonLabel}`;
    $('prediction').textContent = money(data.prediction);
    const up = data.change >= 0;
    $('direction').className = `direction ${up ? 'up' : 'down'}`;
    $('direction').textContent = `${up ? '▲' : '▼'} ${pct.format(Math.abs(data.changePercent))}٪ · ${money(Math.abs(data.change))}`;
    $('interval').textContent = `بازه احتمالی ۸۰٪: ${money(data.rangeLow)} تا ${money(data.rangeHigh)}`;
    $('mae').textContent = `${pct.format(data.backtest.maePercent)}٪`;
    $('accuracy').textContent = data.backtest.directionAccuracy == null ? '—' : `${pct.format(data.backtest.directionAccuracy)}٪`;
    $('testDays').textContent = `${fmt.format(data.backtest.samples)} نمونه نگه‌داشته‌شده`;
    $('observations').textContent = fmt.format(data.observations);
    $('observationUnit').textContent = selectedModelType === 'short' ? 'کندل ۵ دقیقه‌ای' : 'روز معاملاتی ذخیره‌شده';
    $('modelName').textContent = data.model.name;
    $('karatBadge').textContent = `${fmt.format(data.karat)} عیار · ${data.modeDescription}`;
    $('chartEyebrow').textContent = selectedModelType === 'short' ? 'کندل‌های ۵ دقیقه‌ای جلسه جاری' : '۲۶۰ روز معاملاتی اخیر از تاریخچه ذخیره‌شده';
    $('chartTitle').textContent = selectedModelType === 'short' ? 'قیمت هر گرم' : 'روند بلندمدت قیمت هر گرم';
    $('noticeText').innerHTML = selectedModelType === 'short' ? '<b>نکته مهم:</b> خارج ساعت بازار، نرخ داخلی ۲۴ساعته واقعی نیست و مقدار نظری بر اساس حرکت اونس ساخته می‌شود. پیش‌بینی تضمین یا سیگنال معامله نیست.' : '<b>نکته مهم:</b> مدل بلندمدت از قیمت، دلار، اونس و شاخص ریسک داخلی استفاده می‌کند. داده خبری تنها وقتی وارد آموزش می‌شود که پوشش معتبر موجود باشد؛ تعداد روزهای پوشش بالا نمایش داده شده است.';
    $('riskInfo').textContent = selectedModelType === 'long' ? `شاخص ریسک داخلی (فاصله قیمت بازار از ارزش اونس×دلار): ${pct.format(data.risk.domesticPremium)} · پوشش فعلی خبر تاریخی: ${fmt.format(data.risk.newsCoverageDays)} روز` : '';
    const edge = data.benchmark;
    $('edgeInfo').className = `edge-info ${edge.hasEdge ? 'positive' : 'warning'}`;
    $('edgeInfo').textContent = edge.hasEdge
      ? `✓ مدل فعال در بک‌تست ${pct.format(edge.improvementPercent)}٪ بهتر از معیار «بدون تغییر» بوده است. خطای فعال: ${pct.format(edge.activeMaePercent)}٪ · خطای معیار: ${pct.format(edge.neutralMaePercent)}٪`
      : `⚠ پیش‌بینی فعال نمایش داده می‌شود، اما هنوز از معیار «بدون تغییر» بهتر نیست. خطای فعال: ${pct.format(edge.activeMaePercent)}٪ · خطای معیار: ${pct.format(edge.neutralMaePercent)}٪`;
    if (data.marketAnalysis) renderMarketAnalysis(data.marketAnalysis);
    drawChart(data.chart);
    $('status').className = 'status hidden';
    $('dashboard').classList.remove('hidden');
  } catch (error) {
    $('status').className = 'status error';
    $('status').textContent = error.message;
  } finally {
    $('refresh').disabled = false;
    loading = false;
  }
}

document.querySelectorAll('[data-karat]').forEach(button => button.addEventListener('click', () => {
  selectedKarat = Number(button.dataset.karat);
  document.querySelectorAll('[data-karat]').forEach(b => b.classList.toggle('active', b === button));
  load();
}));
document.querySelectorAll('[data-model]').forEach(button => button.addEventListener('click', () => {
  selectedModelType = button.dataset.model;
  document.querySelectorAll('[data-model]').forEach(b => b.classList.toggle('active', b === button));
  setHorizons();
  load();
}));
$('refresh').addEventListener('click', load);
$('horizon').addEventListener('change', load);
setHorizons();
load();
setInterval(load, 60_000);
