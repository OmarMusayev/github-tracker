const SUMMARY_URL = "data/summary.json";

let SUMMARY = null;
const charts = {};

const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());

const fmtShortDate = (s) => {
  if (!s) return "";
  const [y, m, d] = s.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
};

const minDateAcross = (repos, pick) => {
  let min = null;
  for (const r of Object.values(repos || {})) {
    for (const row of pick(r) || []) {
      if (row && row.date && (!min || row.date < min)) min = row.date;
    }
  }
  return min;
};

async function load() {
  let r;
  try {
    r = await fetch(SUMMARY_URL, { cache: "no-store" });
  } catch (e) {
    return showError("Could not fetch summary.json: " + e.message);
  }
  if (!r.ok) return showError(`summary.json not found (HTTP ${r.status}). Has the collector run yet?`);
  SUMMARY = await r.json();

  document.getElementById("generated-at").textContent =
    SUMMARY.generated_at
      ? "updated " + new Date(SUMMARY.generated_at).toLocaleString()
      : "no data yet — run the workflow once";

  renderTotals();
  renderRepoPicker();
  document.getElementById("repo-select").addEventListener("change", renderRepo);
  document.getElementById("range-select").addEventListener("change", renderRepo);
  renderRepo();
}

function showError(msg) {
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div class="error">${msg}</div>`
  );
}

function renderTotals() {
  const t = SUMMARY.totals || {};
  const repos = SUMMARY.repos || {};
  const delta = (n) =>
    n != null && n > 0
      ? `<span class="pos">+${fmt(n)}</span> last 30d`
      : "&nbsp;";

  const trafficSince = minDateAcross(repos, (r) => [
    ...(r.views || []),
    ...(r.clones || []),
  ]);
  const npmSince = minDateAcross(repos, (r) => (r.npm && r.npm.daily) || []);
  const since = (d) => (d ? `since ${fmtShortDate(d)}` : "all-time");

  const heroes = [
    { lbl: "stars", num: t.total_stars, delta: "" },
    { lbl: `views — ${since(trafficSince)}`, num: t.views_all_time, delta: delta(t.views_30d) },
    { lbl: `clones — ${since(trafficSince)}`, num: t.clones_all_time, delta: delta(t.clones_30d) },
    { lbl: `npm downloads — ${since(npmSince)}`, num: t.npm_downloads_all_time, delta: delta(t.npm_downloads_30d) },
    { lbl: "repos tracked", num: t.repo_count, delta: "" },
  ];

  const heroHtml = heroes
    .map(
      (h) => `
      <div class="hero-stat">
        <div class="hero-lbl">${h.lbl}</div>
        <div class="hero-num">${fmt(h.num)}</div>
        <div class="hero-delta">${h.delta || "&nbsp;"}</div>
      </div>`
    )
    .join("");

  const SEP = '<span class="sep">·</span>';
  const stripBits = [
    [t.unique_visitors_all_time, "unique visitors"],
    [t.unique_cloners_all_time, "unique cloners"],
    [t.total_forks, "forks"],
    [t.total_watchers, "watchers"],
    [t.npm_packages_count, t.npm_packages_count === 1 ? "npm package" : "npm packages"],
  ]
    .filter(([v]) => v != null)
    .map(([v, lbl]) => `<strong>${fmt(v)}</strong> ${lbl}`)
    .join(SEP);

  document.getElementById("totals").innerHTML = `
    <div class="hero-row">${heroHtml}</div>
    <div class="more-strip">${stripBits}</div>`;
}

function renderRepoPicker() {
  const sel = document.getElementById("repo-select");
  const repos = Object.keys(SUMMARY.repos || {}).sort((a, b) => {
    const av = (SUMMARY.repos[a].views || []).reduce((s, x) => s + (x.count || 0), 0);
    const bv = (SUMMARY.repos[b].views || []).reduce((s, x) => s + (x.count || 0), 0);
    return bv - av || a.localeCompare(b);
  });
  if (repos.length === 0) {
    sel.innerHTML = `<option>(no repos yet)</option>`;
    sel.disabled = true;
    return;
  }
  sel.innerHTML = repos.map((r) => `<option value="${r}">${r}</option>`).join("");
}

function inRange(date, days) {
  if (days === "all") return true;
  const d = new Date(date + "T00:00:00Z");
  const cutoff = Date.now() - parseInt(days, 10) * 86400000;
  return d.getTime() >= cutoff;
}

function renderRepo() {
  const sel = document.getElementById("repo-select");
  const repo = sel.value;
  const days = document.getElementById("range-select").value;
  const data = (SUMMARY.repos || {})[repo];
  const npmCard = document.getElementById("card-npm");
  if (!data) {
    document.getElementById("repo-meta").innerHTML = "";
    npmCard.hidden = true;
    return;
  }

  const meta = data.meta || {};
  const allTime = data.all_time || {};
  document.getElementById("repo-meta").innerHTML = [
    meta.archived ? "archived" : null,
    meta.fork ? "fork" : null,
    meta.language ? `lang: ${meta.language}` : null,
    `★ ${fmt(meta.stars)}`,
    `⑂ ${fmt(meta.forks)}`,
    allTime.views != null ? `views: ${fmt(allTime.views)}` : null,
    allTime.clones != null ? `clones: ${fmt(allTime.clones)}` : null,
    data.npm ? `npm: ${fmt(data.npm.all_time)} dl` : null,
    `<a href="https://github.com/${repo}" target="_blank" rel="noopener">github →</a>`,
    data.npm
      ? `<a href="https://www.npmjs.com/package/${data.npm.name}" target="_blank" rel="noopener">npm →</a>`
      : null,
  ]
    .filter(Boolean)
    .map((s) => `<span class="tag">${s}</span>`)
    .join("");

  const clones = (data.clones || []).filter((d) => inRange(d.date, days));
  const views = (data.views || []).filter((d) => inRange(d.date, days));
  const history = (data.history || []).filter((d) => inRange(d.date, days));

  drawDual("chart-views", views, "views", "unique visitors", "#4f9cff", "#ffaa4f");
  drawDual("chart-clones", clones, "clones", "unique cloners", "#7ddc7d", "#ff7da6");
  drawHistory("chart-history", history);

  if (data.npm) {
    npmCard.hidden = false;
    const n = data.npm;
    document.getElementById("npm-pkg-name").innerHTML =
      `<a href="https://www.npmjs.com/package/${n.name}" target="_blank" rel="noopener">${escape(n.name)}</a>` +
      (n.latest_version ? `<span class="ver">v${escape(n.latest_version)}</span>` : "");
    const npmStart = (n.daily || []).find((d) => d.date)?.date;
    document.getElementById("npm-stats").innerHTML = [
      [npmStart ? `since ${fmtShortDate(npmStart)}` : "all-time", n.all_time],
      ["last 30d", n.d30],
      ["last 7d", n.d7],
      ["versions", n.version_count],
    ]
      .map(
        ([lbl, v]) =>
          `<div class="npm-stat"><div class="num">${fmt(v)}</div><div class="lbl">${lbl}</div></div>`
      )
      .join("");
    const npmDaily = (n.daily || []).filter((d) => inRange(d.date, days));
    drawNpm("chart-npm", npmDaily);
  } else {
    npmCard.hidden = true;
  }

  fillTable(
    "table-referrers",
    (data.referrers || []).map((r) => [
      r.referrer,
      fmt(r.count),
      fmt(r.uniques),
    ])
  );
  fillTable(
    "table-paths",
    (data.paths || []).map((p) => [
      `<a href="https://github.com${p.path}" target="_blank" rel="noopener" title="${escapeAttr(p.title || "")}">${escape(p.path)}</a>`,
      fmt(p.count),
      fmt(p.uniques),
    ])
  );
  fillTable(
    "table-releases",
    (data.releases || []).map((r) => [
      escape(r.tag || ""),
      escape(r.asset || ""),
      fmt(r.downloads),
    ])
  );

  const totalBytes = (data.languages || []).reduce((s, x) => s + (x.bytes || 0), 0);
  fillTable(
    "table-languages",
    (data.languages || []).map((l) => [
      escape(l.language),
      fmt(l.bytes),
      totalBytes ? ((l.bytes / totalBytes) * 100).toFixed(1) + "%" : "—",
    ])
  );
}

function drawDual(canvasId, rows, l1, l2, c1, c2) {
  if (charts[canvasId]) charts[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  charts[canvasId] = new Chart(ctx, {
    type: "line",
    data: {
      labels: rows.map((r) => r.date),
      datasets: [
        {
          label: l1,
          data: rows.map((r) => r.count),
          borderColor: c1,
          backgroundColor: c1 + "26",
          fill: true,
          tension: 0.25,
          pointRadius: 2,
        },
        {
          label: l2,
          data: rows.map((r) => r.uniques),
          borderColor: c2,
          backgroundColor: c2 + "26",
          fill: true,
          tension: 0.25,
          pointRadius: 2,
        },
      ],
    },
    options: chartOpts(),
  });
}

function drawHistory(canvasId, rows) {
  if (charts[canvasId]) charts[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  charts[canvasId] = new Chart(ctx, {
    type: "line",
    data: {
      labels: rows.map((r) => r.date),
      datasets: [
        { label: "stars", data: rows.map((r) => r.stars), borderColor: "#ffd24a", tension: 0.25, pointRadius: 1 },
        { label: "forks", data: rows.map((r) => r.forks), borderColor: "#7ddc7d", tension: 0.25, pointRadius: 1 },
        { label: "watchers", data: rows.map((r) => r.watchers), borderColor: "#c08aff", tension: 0.25, pointRadius: 1 },
      ],
    },
    options: chartOpts(),
  });
}

function drawNpm(canvasId, rows) {
  if (charts[canvasId]) charts[canvasId].destroy();
  const ctx = document.getElementById(canvasId);
  charts[canvasId] = new Chart(ctx, {
    type: "bar",
    data: {
      labels: rows.map((r) => r.date),
      datasets: [
        {
          label: "downloads",
          data: rows.map((r) => r.downloads),
          backgroundColor: "#cb3837",
          borderColor: "#cb3837",
          borderWidth: 0,
        },
      ],
    },
    options: chartOpts(),
  });
}

function chartOpts() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    scales: {
      y: { beginAtZero: true, ticks: { color: "#8b949e" }, grid: { color: "#21262d" } },
      x: { ticks: { color: "#8b949e", maxRotation: 0, autoSkip: true }, grid: { color: "#21262d" } },
    },
    plugins: {
      legend: { labels: { color: "#cdd6e0" } },
      tooltip: { backgroundColor: "#161b22", borderColor: "#30363d", borderWidth: 1 },
    },
  };
}

function fillTable(tableId, rows) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  if (!rows || rows.length === 0) {
    const cols = document.querySelectorAll(`#${tableId} thead th`).length;
    tbody.innerHTML = `<tr><td colspan="${cols}" class="muted">no data</td></tr>`;
    return;
  }
  tbody.innerHTML = rows
    .map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`)
    .join("");
}

function escape(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
function escapeAttr(s) { return escape(s); }

load();
