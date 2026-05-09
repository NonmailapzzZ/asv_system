const MAX_POINTS = 50;
const chartData = {
  labels: [], depth: [], ph: [], do: []
};

function makeDataset(label, color, data) {
  return {
    label, data,
    borderColor: color,
    backgroundColor: color + '18',
    tension: 0.4,
    pointRadius: 0,
    borderWidth: 2,
    fill: true,
  };
}

async function exportByDateRange(startDate, endDate) {
  const response = await fetch('/api/export/date_range_download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      start_date: startDate,
      end_date: endDate
    })
  });
  
  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'survey_data.csv';
  a.click();
}

// Update sampling status ทุก 1 วินาที
setInterval(async () => {
  const response = await fetch('/api/sampling/status');
  const status = await response.json();
  
  document.getElementById('status-text').textContent = 
    status.active ? 'SAMPLING' : 'Idle';
  document.getElementById('status-progress').textContent = 
    status.progress.toFixed(0) + '%';
  document.getElementById('status-samples').textContent = 
    status.sample_count;
  document.getElementById('status-remaining').textContent = 
    status.remaining.toFixed(1);
}, 1000);


const commonOptions = (unit) => ({
  responsive: true,
  animation: { duration: 250 },
  interaction: { mode: 'index', intersect: false },
  plugins: { legend: { display: false } },
  scales: {
    x: {
      ticks: { color: '#4a7a85', font: { family: 'Share Tech Mono', size: 10 }, maxTicksLimit: 8 },
      grid: { color: 'rgba(0,200,255,0.05)' }
    },
    y: {
      ticks: { color: '#4a7a85', font: { family: 'Share Tech Mono', size: 10 },
               callback: v => v.toFixed(1) + ' ' + unit },
      grid: { color: 'rgba(0,200,255,0.05)' }
    }
  }
});

const depthChart = new Chart(document.getElementById('chart-depth'), {
  type: 'line',
  data: { labels: chartData.labels, datasets: [makeDataset('Depth (m)', '#00d4ff', chartData.depth)] },
  options: commonOptions('m')
});

const phChart = new Chart(document.getElementById('chart-ph'), {
  type: 'line',
  data: { labels: chartData.labels, datasets: [makeDataset('pH', '#ff6eb4', chartData.ph)] },
  options: commonOptions('')
});

const doChart = new Chart(document.getElementById('chart-do'), {
  type: 'line',
  data: { labels: chartData.labels, datasets: [makeDataset('DO (mg/L)', '#00ff9d', chartData.do)] },
  options: commonOptions('mg/L')
});

export function pushPoint(ts, depth, ph, doVal) {
  chartData.labels.push(ts);
  chartData.depth.push(depth);
  chartData.ph.push(ph);
  chartData.do.push(doVal);

  if (chartData.labels.length > MAX_POINTS) {
    chartData.labels.shift();
    chartData.depth.shift();
    chartData.ph.shift();
    chartData.do.shift();
  }

  depthChart.update('none');
  phChart.update('none');
  doChart.update('none');
}

export function loadHistory(records) {
  records.forEach(r => {
    chartData.labels.push(r.timestamp.slice(11, 19));
    chartData.depth.push(r.depth);
    chartData.ph.push(r.ph);
    chartData.do.push(r.do);
  });
  depthChart.update();
  phChart.update();
  doChart.update();
}
