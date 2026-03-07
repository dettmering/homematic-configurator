const DAY_NAMES = {
  MONDAY: 'Mo', TUESDAY: 'Di', WEDNESDAY: 'Mi',
  THURSDAY: 'Do', FRIDAY: 'Fr', SATURDAY: 'Sa', SUNDAY: 'So',
};
const DAY_NAMES_LONG = {
  MONDAY: 'Montag', TUESDAY: 'Dienstag', WEDNESDAY: 'Mittwoch',
  THURSDAY: 'Donnerstag', FRIDAY: 'Freitag', SATURDAY: 'Samstag', SUNDAY: 'Sonntag',
};
const DAYS_ORDER = ['MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY','SUNDAY'];

document.addEventListener('DOMContentLoaded', loadDashboard);

async function loadDashboard() {
  const loading = document.getElementById('dashboard-loading');
  const content = document.getElementById('dashboard-content');

  try {
    const res = await fetch('/api/dashboard');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

    loading.style.display = 'none';
    content.classList.remove('hidden');

    renderOutdoorTemp(data.outdoor_temp);
    renderMetricCards(data);
    renderHeatmap(data.heatmap);
    renderDailyChart(data.daily_degree_hours);
    renderSimultaneityChart(data.hourly_simultaneity, data.num_devices);
    renderDeviceRanking(data.device_ranking);
  } catch (e) {
    loading.textContent = 'Fehler beim Laden: ' + e.message;
  }
}

function renderOutdoorTemp(temp) {
  const el = document.getElementById('outdoor-temp');
  if (temp !== null && temp !== undefined) {
    el.textContent = `Aussentemperatur: ${temp.toFixed(1)} °C`;
  } else {
    el.textContent = 'Kein Aussensensor gefunden';
  }
}

function renderMetricCards(data) {
  // 1. Average temperature
  document.getElementById('m-avg-temp').textContent = `${data.avg_temp} °C`;
  const dailyTemps = DAYS_ORDER.map(d => `${DAY_NAMES[d]} ${data.daily_avg_temps[d]}°`).join(', ');
  document.getElementById('m-avg-temp-sub').textContent = dailyTemps;

  // 2. Heating hours
  document.getElementById('m-heating-hours').textContent = `${data.total_heating_hours_week} h`;
  document.getElementById('m-heating-hours-sub').textContent =
    `${data.num_devices} Heizkoerper, Schwelle: >17 °C`;

  // 3. Spread
  document.getElementById('m-spread').textContent = `${data.avg_spread} °C`;
  document.getElementById('m-spread-sub').textContent = 'Durchschnittl. Max-Spreizung (Absenkung zu Komfort)';

  // 4. Degree hours
  document.getElementById('m-degree-hours').textContent = formatLargeNumber(data.total_degree_hours_week);
  const dhSub = data.outdoor_temp !== null
    ? `Bereinigt um Aussentemp. (${data.outdoor_temp.toFixed(1)} °C)`
    : 'Ohne Aussentemperatur-Bereinigung';
  document.getElementById('m-degree-hours-sub').textContent = dhSub;

  // 5. Simultaneity
  document.getElementById('m-simultaneity').textContent =
    `${data.max_simultaneity} / ${data.num_devices}`;
  document.getElementById('m-simultaneity-sub').textContent =
    'Max. gleichzeitig aktive Heizkoerper (Wochenschnitt)';
}

function formatLargeNumber(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return n.toFixed(0);
}

// ── Heatmap ─────────────────────────────────────────────────────────────────

function tempToHeatmapColor(temp) {
  // Map temperature to color: 4.5-16 blue, 17-19 cyan/green, 20-22 amber, 23+ red
  if (temp <= 5) return '#1e3a5f';
  if (temp <= 14) return '#3b82f6';
  if (temp <= 16) return '#06b6d4';
  if (temp <= 18) return '#22c55e';
  if (temp <= 20) return '#84cc16';
  if (temp <= 21) return '#f59e0b';
  if (temp <= 22.5) return '#f97316';
  return '#ef4444';
}

function renderHeatmap(heatmap) {
  const container = document.getElementById('heatmap');
  container.innerHTML = '';

  // Header row: empty corner + hour labels
  const corner = document.createElement('div');
  container.appendChild(corner);
  for (let h = 0; h < 24; h++) {
    const lbl = document.createElement('div');
    lbl.className = 'heatmap-header';
    lbl.textContent = `${h}`;
    container.appendChild(lbl);
  }

  // Data rows
  DAYS_ORDER.forEach(day => {
    const label = document.createElement('div');
    label.className = 'heatmap-label';
    label.textContent = DAY_NAMES_LONG[day];
    container.appendChild(label);

    const hours = heatmap[day] || [];
    hours.forEach(temp => {
      const cell = document.createElement('div');
      cell.className = 'heatmap-cell';
      cell.style.background = tempToHeatmapColor(temp);
      cell.textContent = temp > 0 ? temp.toFixed(0) : '';
      cell.title = `${temp.toFixed(1)} °C`;
      container.appendChild(cell);
    });
  });

  // Legend
  const legend = document.getElementById('heatmap-legend');
  legend.innerHTML = '<span>Kalt</span><div class="legend-gradient"></div><span>Warm</span>';
}

// ── Daily bar chart ─────────────────────────────────────────────────────────

function renderDailyChart(dailyDegreeHours) {
  const container = document.getElementById('chart-daily');
  container.innerHTML = '';

  const values = DAYS_ORDER.map(d => dailyDegreeHours[d] || 0);
  const maxVal = Math.max(...values, 1);

  const chart = document.createElement('div');
  chart.className = 'bar-chart';

  DAYS_ORDER.forEach((day, i) => {
    const val = values[i];
    const pct = (val / maxVal) * 100;

    const group = document.createElement('div');
    group.className = 'bar-group';

    const valEl = document.createElement('div');
    valEl.className = 'bar-value';
    valEl.textContent = val >= 1000 ? (val / 1000).toFixed(1) + 'k' : val.toFixed(0);

    const bar = document.createElement('div');
    bar.className = 'bar';
    bar.style.height = `${pct}%`;
    bar.style.background = barColor(pct);

    const label = document.createElement('div');
    label.className = 'bar-label';
    label.textContent = DAY_NAMES[day];

    group.appendChild(valEl);
    group.appendChild(bar);
    group.appendChild(label);
    chart.appendChild(group);
  });

  container.appendChild(chart);
}

function barColor(pct) {
  if (pct < 40) return '#3b82f6';
  if (pct < 70) return '#f59e0b';
  return '#ef4444';
}

// ── Simultaneity chart ──────────────────────────────────────────────────────

function renderSimultaneityChart(hourly, numDevices) {
  const container = document.getElementById('chart-simultaneity');
  container.innerHTML = '';

  const maxVal = Math.max(...hourly, 1);

  const chart = document.createElement('div');
  chart.className = 'sim-chart';

  hourly.forEach((val, hour) => {
    const pct = (val / numDevices) * 100;

    const group = document.createElement('div');
    group.className = 'sim-bar-group';

    const valEl = document.createElement('div');
    valEl.className = 'sim-value';
    valEl.textContent = val > 0 ? val.toFixed(0) : '';

    const bar = document.createElement('div');
    bar.className = 'sim-bar';
    bar.style.height = `${(val / maxVal) * 100}%`;
    bar.style.background = simColor(pct);

    const label = document.createElement('div');
    label.className = 'sim-label';
    label.textContent = hour % 3 === 0 ? `${hour}` : '';

    group.appendChild(valEl);
    group.appendChild(bar);
    group.appendChild(label);
    chart.appendChild(group);
  });

  container.appendChild(chart);
}

function simColor(pct) {
  if (pct < 30) return '#22c55e';
  if (pct < 60) return '#f59e0b';
  return '#ef4444';
}

// ── Device ranking ──────────────────────────────────────────────────────────

function renderDeviceRanking(ranking) {
  const container = document.getElementById('device-ranking');
  container.innerHTML = '';

  if (!ranking || ranking.length === 0) {
    container.textContent = 'Keine Daten';
    return;
  }

  const maxDH = Math.max(...ranking.map(r => r.degree_hours_week), 1);

  const table = document.createElement('table');
  table.className = 'ranking-table';

  const thead = document.createElement('thead');
  thead.innerHTML = `
    <tr>
      <th>#</th>
      <th>Heizkoerper</th>
      <th>Grad-Stunden / Woche</th>
      <th class="ranking-bar-cell">Anteil</th>
      <th>Spreizung</th>
    </tr>`;
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  ranking.forEach((dev, i) => {
    const pct = (dev.degree_hours_week / maxDH) * 100;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="rank-number">${i + 1}</td>
      <td>${dev.name} <span style="color:var(--text-muted);font-size:0.75rem">(ID: ${dev.peer_id})</span></td>
      <td>${formatLargeNumber(dev.degree_hours_week)}</td>
      <td class="ranking-bar-cell">
        <div class="ranking-bar-bg">
          <div class="ranking-bar-fill" style="width:${pct}%;background:${barColor(pct)}"></div>
        </div>
      </td>
      <td>${dev.avg_spread} °C</td>`;
    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  container.appendChild(table);
}
