/* ── Formatter Helpers ─────────────────────────────────────────────────── */
const fmt = {
  usd: v => v == null ? '—' : '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}),
  pct: v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%',
  num: v => v == null ? '—' : Number(v).toLocaleString('en-US'),
  date: v => v || '—',
};

function scoreClass(s) {
  if (s >= 70) return 'score-high';
  if (s >= 50) return 'score-medium';
  return 'score-low';
}

/* ── Load Data & Render ────────────────────────────────────────────────── */
async function init() {
  let data;
  try {
    const resp = await fetch('data.json');
    data = await resp.json();
  } catch(e) {
    console.error('Failed to load data.json', e);
    return;
  }

  const m = data.metrics;

  // Header
  document.getElementById('updatedAt').textContent = 'Last updated: ' + data.updated_at;

  // Metrics
  const valEl = document.getElementById('metricValue');
  valEl.textContent = fmt.usd(m.current_value);

  const retEl = document.getElementById('metricReturn');
  retEl.textContent = 'Total Return: ' + fmt.pct(m.total_return_pct);
  retEl.className = 'metric-sub ' + (m.total_return_pct >= 0 ? 'pnl-positive' : 'pnl-negative');

  const ytdEl = document.getElementById('metricYTD');
  ytdEl.textContent = fmt.pct(m.ytd_return_pct);
  ytdEl.classList.add(m.ytd_return_pct >= 0 ? 'positive' : 'negative');

  document.getElementById('metricSharpe').textContent = m.sharpe_ratio.toFixed(2);
  const ddEl = document.getElementById('metricDrawdown');
  ddEl.textContent = '-' + m.max_drawdown_pct.toFixed(2) + '%';
  ddEl.classList.add('negative');

  document.getElementById('metricTrades').textContent = m.total_trades;
  const wrEl = document.getElementById('metricWinRate');
  wrEl.textContent = m.win_rate.toFixed(1) + '%';

  // Chart
  renderChart(data.chart, data.starting_capital);

  // Positions
  renderPositions(data.positions);

  // Transactions
  renderTransactions(data.transactions);

  // Trades
  renderTrades(data.trades);
}

/* ── Chart ─────────────────────────────────────────────────────────────── */
function renderChart(chart, startCap) {
  const ctx = document.getElementById('portfolioChart').getContext('2d');

  if (!chart.labels.length) {
    ctx.font = '14px Inter';
    ctx.fillStyle = '#5a6178';
    ctx.textAlign = 'center';
    ctx.fillText('No history data yet – chart will appear after first run', ctx.canvas.width/2, ctx.canvas.height/2);
    return;
  }

  new Chart(ctx, {
    type: 'line',
    data: {
      labels: chart.labels,
      datasets: [
        {
          label: 'Portfolio',
          data: chart.portfolio,
          borderColor: '#6366f1',
          backgroundColor: 'rgba(99,102,241,.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          pointHoverRadius: 5,
          borderWidth: 2.5,
        },
        {
          label: 'S&P 500 (SPY)',
          data: chart.benchmark,
          borderColor: '#64748b',
          backgroundColor: 'transparent',
          borderDash: [6, 4],
          tension: 0.3,
          pointRadius: 0,
          pointHoverRadius: 5,
          borderWidth: 1.5,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: {
          labels: { color: '#8b92a5', font: { family: 'Inter', size: 12 }, usePointStyle: true, pointStyle: 'line' },
        },
        tooltip: {
          backgroundColor: '#1a1f2e',
          titleColor: '#e8eaf0',
          bodyColor: '#8b92a5',
          borderColor: '#2a3042',
          borderWidth: 1,
          padding: 12,
          displayColors: true,
          callbacks: {
            label: ctx2 => ctx2.dataset.label + ': ' + fmt.usd(ctx2.raw),
          },
        },
      },
      scales: {
        x: {
          ticks: { color: '#5a6178', maxTicksLimit: 12, font: { size: 11 } },
          grid: { color: 'rgba(42,48,66,.5)' },
        },
        y: {
          ticks: {
            color: '#5a6178',
            font: { size: 11 },
            callback: v => '$' + (v/1000).toFixed(0) + 'k',
          },
          grid: { color: 'rgba(42,48,66,.5)' },
        },
      },
    },
  });
}

/* ── Tables ────────────────────────────────────────────────────────────── */
function renderPositions(positions) {
  const tbody = document.getElementById('positionsBody');
  document.getElementById('positionCount').textContent = positions.length + ' position' + (positions.length !== 1 ? 's' : '');

  if (!positions.length) return;

  tbody.innerHTML = positions.map(p => {
    const pnl = p.pnl_pct;
    const pnlStr = pnl != null ? fmt.pct(pnl) : '—';
    const pnlCls = pnl != null ? (pnl >= 0 ? 'pnl-positive' : 'pnl-negative') : '';
    return `<tr>
      <td class="ticker">${p.ticker}</td>
      <td>${fmt.num(p.shares)}</td>
      <td>${fmt.usd(p.avg_cost)}</td>
      <td>${p.current_price ? fmt.usd(p.current_price) : '—'}</td>
      <td class="${pnlCls}">${pnlStr}</td>
      <td>${fmt.date(p.opened_at)}</td>
      <td>${p.triggering_insider}</td>
    </tr>`;
  }).join('');
}

function renderTransactions(txs) {
  const tbody = document.getElementById('transactionsBody');
  document.getElementById('txCount').textContent = txs.length + ' signals';

  if (!txs.length) return;

  tbody.innerHTML = txs.map(t => {
    const cls = scoreClass(t.score);
    const clusterHtml = t.cluster ? '<span class="cluster-badge">CLUSTER</span>' : '';
    return `<tr>
      <td><span class="score ${cls}">${t.score}</span>${clusterHtml}</td>
      <td class="ticker">${t.ticker}</td>
      <td>${t.insider_name}</td>
      <td>${t.insider_title}</td>
      <td>${fmt.date(t.trade_date)}</td>
      <td>${fmt.num(t.shares)}</td>
      <td>${fmt.usd(t.price)}</td>
      <td>${fmt.usd(t.value)}</td>
      <td>${t.hit_rate != null ? t.hit_rate + '%' : '—'}</td>
    </tr>`;
  }).join('');
}

function renderTrades(trades) {
  const tbody = document.getElementById('tradesBody');
  document.getElementById('tradeCount').textContent = trades.length + ' trade' + (trades.length !== 1 ? 's' : '');

  if (!trades.length) return;

  tbody.innerHTML = trades.map(t => `<tr>
    <td>${t.timestamp}</td>
    <td class="${t.action === 'BUY' ? 'action-buy' : 'action-sell'}">${t.action}</td>
    <td class="ticker">${t.ticker}</td>
    <td>${fmt.usd(t.price)}</td>
    <td>${fmt.num(t.shares)}</td>
    <td>${fmt.usd(t.total_value)}</td>
    <td>${t.reason}</td>
    <td>${t.triggering_insider}</td>
  </tr>`).join('');
}

// Bootstrap
init();
