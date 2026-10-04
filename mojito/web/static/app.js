// Flood risk map: zones coloured by the expected flooded share for the
// selected forecast day, plus the X.44 water level chart.

const THAI_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];
const ALERT_COLORS = { normal: "#2e8b57", bank: "#e08a00", flood: "#c62828" };

const state = { forecast: null, horizon: 1, zonesLayer: null, playing: null, dates: null };

const map = L.map("map", { zoomControl: true }).setView([7.0, 100.47], 11);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 18,
  className: "basemap",
}).addTo(map);

function thaiDate(iso, withYear = false) {
  const [y, m, d] = iso.split("-").map(Number);
  const text = `${d} ${THAI_MONTHS[m - 1]}`;
  return withYear ? `${text} ${String(y + 543).slice(2)}` : text;
}

// Yellow -> red -> dark red, transparent when nothing is expected to flood
function shareColor(share) {
  const stops = [
    [0.0, [255, 237, 160]],
    [0.25, [254, 178, 76]],
    [0.5, [240, 59, 32]],
    [1.0, [128, 0, 38]],
  ];
  let i = 1;
  while (i < stops.length - 1 && share > stops[i][0]) i += 1;
  const [s0, c0] = stops[i - 1];
  const [s1, c1] = stops[i];
  const t = Math.min(1, Math.max(0, (share - s0) / (s1 - s0)));
  const rgb = c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
  return `rgb(${rgb.join(",")})`;
}

function zoneStyle(feature) {
  const share = state.forecast ? state.forecast.horizons[state.horizon - 1].zone_share[feature.properties.index] : 0;
  return {
    stroke: false,
    fillColor: shareColor(share),
    fillOpacity: share > 0 ? 0.25 + 0.6 * Math.min(share * 1.5, 1) : 0,
  };
}

function zonePopup(feature) {
  const p = feature.properties;
  const horizon = state.forecast.horizons[state.horizon - 1];
  const share = horizon.zone_share[p.index];
  const start = p.start_stage <= 12 ? `${p.start_stage.toFixed(2)} ม.` : "สูงกว่า 12 ม. (เสี่ยงต่ำ)";
  return `
    <strong>${thaiDate(horizon.date, true)}</strong><br>
    คาดว่าพื้นที่ในโซนนี้ท่วม <strong>${Math.round(share * 100)}%</strong><br>
    เริ่มท่วมเมื่อ X.44 สูงถึง <strong>${start}</strong><br>
    <span style="color:#5b6878">น้ำท่วมจริง พ.ย. 2568: ${Math.round((p.flooded_share_2025 || 0) * 100)}% ของโซน</span>`;
}

async function loadJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.json();
}

async function loadZones() {
  const geojson = await loadJSON("/api/zones");
  geojson.features.forEach((feature, index) => { feature.properties.index = index; });
  state.zonesLayer = L.geoJSON(geojson, { style: zoneStyle }).addTo(map);
  state.zonesLayer.bindPopup((layer) => zonePopup(layer.feature));
  map.fitBounds(state.zonesLayer.getBounds(), { padding: [10, 10] });
}

async function loadStations() {
  const stations = await loadJSON("/api/stations");
  stations.forEach((s) => {
    const isCity = s.code === "X.44";
    L.circleMarker([s.lat, s.lon], {
      radius: isCity ? 8 : 5,
      color: "#fff",
      weight: 2,
      fillColor: "#1f6fb2",
      fillOpacity: 1,
    })
      .bindTooltip(`${s.code} · ${s.name}${s.min_bank ? ` (ตลิ่ง ${s.min_bank} ม.)` : ""}`, { permanent: isCity, direction: "right" })
      .addTo(map);
  });
}

function setBadge(element, alert) {
  element.textContent = alert.label;
  element.className = `badge ${alert.code}`;
}

function renderDayButtons() {
  const container = document.getElementById("day-buttons");
  container.innerHTML = "";
  state.forecast.horizons.forEach((h) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = h.h === state.horizon ? "active" : "";
    button.innerHTML = `<span class="dot" style="background:${ALERT_COLORS[h.alert.code]}"></span>+${h.h} วัน<small>${thaiDate(h.date)}</small>`;
    button.addEventListener("click", () => { stopPlaying(); selectHorizon(h.h); });
    container.appendChild(button);
  });
}

function renderStatus() {
  const f = state.forecast;
  const h = f.horizons[state.horizon - 1];
  document.getElementById("today-stage").textContent = `${f.today.stage.toFixed(2)} ม.`;
  setBadge(document.getElementById("today-alert"), f.today.alert);
  document.getElementById("selected-label").textContent = `คาดการณ์ ${thaiDate(h.date, true)} (+${h.h} วัน)`;
  document.getElementById("selected-stage").textContent = `${h.stage.toFixed(2)} ± ${h.sigma.toFixed(1)} ม.`;
  const observed = h.observed !== null ? ` · วัดได้จริง ${h.observed.toFixed(2)} ม.` : "";
  document.getElementById("selected-detail").textContent =
    `พื้นที่คาดว่าท่วม ~${h.flooded_km2} ตร.กม. · ${h.zones_at_risk} โซนเสี่ยงสูง${observed}`;
  setBadge(document.getElementById("selected-alert"), h.alert);
}

function renderChart() {
  const svg = document.getElementById("chart");
  const f = state.forecast;
  const W = 360, H = 190, left = 30, right = 10, top = 10, bottom = 24;
  const points = [
    ...f.history.map((d) => ({ date: d.date, stage: d.stage, kind: "history" })),
    ...f.horizons.map((h) => ({ date: h.date, stage: h.stage, sigma: h.sigma, observed: h.observed, kind: "forecast" })),
  ];
  const levels = Object.values(f.alert_levels);
  const values = points.flatMap((p) => [p.stage, p.observed, p.sigma ? p.stage + p.sigma : null]).filter((v) => v !== null && v !== undefined);
  const yMax = Math.max(8, ...values) + 0.5;
  const yMin = Math.min(0, ...values.filter((v) => v !== null));
  const x = (i) => left + (i / (points.length - 1)) * (W - left - right);
  const y = (v) => top + (1 - (v - yMin) / (yMax - yMin)) * (H - top - bottom);

  let html = "";
  for (let v = Math.ceil(yMin); v <= yMax; v += 2) {
    html += `<line x1="${left}" x2="${W - right}" y1="${y(v)}" y2="${y(v)}" stroke="#eef1f5"/>`;
    html += `<text x="${left - 4}" y="${y(v) + 3}" text-anchor="end">${v}</text>`;
  }
  levels.forEach((level, k) => {
    const color = k === 0 ? "#e08a00" : "#c62828";
    html += `<line x1="${left}" x2="${W - right}" y1="${y(level)}" y2="${y(level)}" stroke="${color}" stroke-dasharray="3 3"/>`;
  });
  html += `<text x="${left + 4}" y="${y(Math.max(...levels)) - 4}" text-anchor="start" style="fill:#c62828">ท่วมพื้นที่ลุ่มต่ำ ${Math.max(...levels)} ม.</text>`;

  const firstForecast = f.history.length;
  const todayIndex = firstForecast - 1;
  // Uncertainty band from today through the forecast days
  const band = [
    [x(todayIndex), y(f.today.stage)],
    ...f.horizons.map((h, i) => [x(firstForecast + i), y(h.stage + h.sigma)]),
    ...f.horizons.map((h, i) => [x(firstForecast + i), y(Math.max(yMin, h.stage - h.sigma))]).reverse(),
  ];
  html += `<polygon points="${band.map((p) => p.join(",")).join(" ")}" fill="rgba(31,111,178,0.15)"/>`;

  const historyPath = f.history.map((d, i) => (d.stage === null ? null : `${x(i)},${y(d.stage)}`)).filter(Boolean);
  html += `<polyline points="${historyPath.join(" ")}" fill="none" stroke="#1d2733" stroke-width="2"/>`;
  const forecastPath = [[x(todayIndex), y(f.today.stage)], ...f.horizons.map((h, i) => [x(firstForecast + i), y(h.stage)])];
  html += `<polyline points="${forecastPath.map((p) => p.join(",")).join(" ")}" fill="none" stroke="#1f6fb2" stroke-width="2" stroke-dasharray="5 3"/>`;

  f.horizons.forEach((h, i) => {
    const cx = x(firstForecast + i);
    const selected = h.h === state.horizon;
    html += `<circle cx="${cx}" cy="${y(h.stage)}" r="${selected ? 5 : 3}" fill="#1f6fb2" stroke="#fff" stroke-width="1.5"/>`;
    if (h.observed !== null) html += `<circle cx="${cx}" cy="${y(h.observed)}" r="3" fill="none" stroke="#1d2733" stroke-width="1.5"/>`;
  });
  html += `<line x1="${x(todayIndex)}" x2="${x(todayIndex)}" y1="${top}" y2="${H - bottom}" stroke="#9aa5b1" stroke-dasharray="2 2"/>`;
  html += `<text x="${x(todayIndex)}" y="${H - 8}" text-anchor="middle">วันนี้</text>`;
  [0, todayIndex - 5, points.length - 1].forEach((i) => {
    if (i > 0 && i !== todayIndex) html += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${thaiDate(points[i].date)}</text>`;
  });
  html += `<text x="${x(0)}" y="${H - 8}" text-anchor="start">${thaiDate(points[0].date)}</text>`;
  svg.innerHTML = html;
}

function selectHorizon(h) {
  state.horizon = h;
  state.zonesLayer.setStyle(zoneStyle);
  renderDayButtons();
  renderStatus();
  renderChart();
}

async function loadForecast(date) {
  try {
    state.forecast = await loadJSON(`/api/forecast?date=${date}`);
  } catch (error) {
    alert(`โหลดพยากรณ์ไม่สำเร็จ: ${error.message}`);
    return;
  }
  document.getElementById("issue-date").value = state.forecast.issued;
  history.replaceState(null, "", `?date=${state.forecast.issued}`);
  selectHorizon(state.horizon);
}

function stopPlaying() {
  clearInterval(state.playing);
  state.playing = null;
  document.getElementById("play-button").textContent = "▶";
}

document.getElementById("play-button").addEventListener("click", () => {
  if (state.playing) return stopPlaying();
  document.getElementById("play-button").textContent = "❚❚";
  selectHorizon(1);
  state.playing = setInterval(() => selectHorizon((state.horizon % 5) + 1), 1300);
});

document.getElementById("issue-date").addEventListener("change", (event) => loadForecast(event.target.value));
document.getElementById("demo-button").addEventListener("click", (event) => loadForecast(event.target.dataset.date));
document.getElementById("latest-button").addEventListener("click", () => loadForecast(state.dates.last));

(async function init() {
  state.dates = await loadJSON("/api/dates");
  const input = document.getElementById("issue-date");
  input.min = state.dates.first;
  input.max = state.dates.last;
  await Promise.all([loadZones(), loadStations()]);
  const requested = new URLSearchParams(location.search).get("date");
  await loadForecast(requested || document.getElementById("demo-button").dataset.date);
})();
