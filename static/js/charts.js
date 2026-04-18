/* charts.js — Real-time rolling charts ด้วย Chart.js */

const MAX_POINTS = 50;

const timeLabels  = [];
const depthData   = [];
const phData      = [];
const doData      = [];

let chartDepth, chartPh, chartDo;

function makeChart(ctxId, label, color, data) {
  const ctx = document.getElementById(ctxId).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: timeLabels,
      datasets: [{
        label,
        data,
        borderColor: color,
        backgroundColor: color + '18',
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.4,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      scales: {
        x: {
          ticks: {
            color: '#4a7a99',
            font: { family: 'Share Tech Mono', size: 10 },
            maxTicksLimit: 8,
            maxRotation: 0,
          },
          grid: { color: 'rgba(0,212,255,0.05)' }
        },
        y: {
          ticks: { color: '#4a7a99', font: { family: 'Share Tech Mono', size: 10 } },
          grid: { color: 'rgba(0,212,255,0.05)' }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: 'index',
          backgroundColor: '#0d1821',
          borderColor: color,
          borderWidth: 1,
          titleColor: '#cde8f5',
          bodyColor: '#4a7a99',
        }
      }
    }
  });
}

function initCharts() {
  chartDepth = makeChart('chart-depth', 'Depth (m)',  '#00d4ff', depthData);
  chartPh    = makeChart('chart-ph',    'pH',         '#ff6b9d', phData);
  chartDo    = makeChart('chart-do',    'DO (mg/L)',  '#00ff9d', doData);
}

function pushChartData(ts, depth, ph, doVal) {
  timeLabels.push(ts);
  depthData.push(depth);
  phData.push(ph);
  doData.push(doVal);

  if (timeLabels.length > MAX_POINTS) {
    timeLabels.shift();
    depthData.shift();
    phData.shift();
    doData.shift();
  }

  chartDepth.update('none');
  chartPh.update('none');
  chartDo.update('none');
}
