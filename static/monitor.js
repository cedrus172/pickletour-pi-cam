const $ = (s) => document.querySelector(s);
const CPU_HIST = [];
const MEM_HIST = [];
const MAX_POINTS = 30;

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + " MB";
  return (n/1024/1024/1024).toFixed(2) + " GB";
}
function fmtBps(n) {
  if (n < 1024) return n + " B/s";
  if (n < 1024*1024) return (n/1024).toFixed(1) + " KB/s";
  return (n/1024/1024).toFixed(2) + " MB/s";
}
function fmtUptime(s) {
  const d = Math.floor(s/86400);
  const h = Math.floor((s%86400)/3600);
  const m = Math.floor((s%3600)/60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
function barClass(pct) {
  if (pct >= 85) return "bar-fill danger";
  if (pct >= 65) return "bar-fill warn";
  return "bar-fill";
}

function drawChart(svgId, data, maxY = 100) {
  const svg = document.getElementById(svgId);
  if (!data.length) { svg.innerHTML = ""; return; }
  const w = 600, h = 120, pad = 2;
  const stepX = (w - pad*2) / Math.max(data.length - 1, 1);
  const pts = data.map((v, i) => `${pad + i*stepX},${h - pad - (v/maxY)*(h-pad*2)}`);
  const line = pts.join(" ");
  const area = `M${pad},${h-pad} L${line.replace(/ /g, " L")} L${pad + (data.length-1)*stepX},${h-pad} Z`;
  const last = data[data.length-1];
  const color = last >= 85 ? "var(--danger)" : last >= 65 ? "var(--warn)" : "var(--accent-2)";
  svg.innerHTML = `
    <defs><linearGradient id="g-${svgId}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.4"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0.05"/>
    </linearGradient></defs>
    <path d="${area}" fill="url(#g-${svgId})"/>
    <polyline points="${line}" fill="none" stroke="${color}" stroke-width="2"/>
    <line x1="${pad}" y1="${h/2}" x2="${w-pad}" y2="${h/2}" stroke="var(--border)" stroke-dasharray="3,3"/>
  `;
}

async function pollStats() {
  try {
    const r = await fetch("/api/stats").then(r => r.json());
    const h = r.hostname;
    $("#hdr-info").innerHTML = `
      <div>Host: <b>${h}</b></div>
      <div>Uptime: <b>${fmtUptime(r.uptime)}</b></div>
      <div>Load: <b>${r.cpu.load.join(" / ")}</b></div>
    `;
    $("#footer-host").textContent = `${h} · ${new Date().toLocaleTimeString()}`;

    // CPU
    $("#cpu-pct").textContent = r.cpu.percent.toFixed(1);
    $("#cpu-bar").className = barClass(r.cpu.percent);
    $("#cpu-bar").style.width = r.cpu.percent + "%";
    $("#cpu-meta").textContent = `${r.cpu.count} cores · ${r.cpu.mhz || "?"} MHz`;

    // RAM
    $("#mem-pct").textContent = r.mem.percent.toFixed(1);
    $("#mem-bar").className = barClass(r.mem.percent);
    $("#mem-bar").style.width = r.mem.percent + "%";
    $("#mem-meta").textContent = `${fmtBytes(r.mem.used)} / ${fmtBytes(r.mem.total)}` +
      (r.swap.total ? ` · swap ${r.swap.percent}%` : "");

    // Disk
    $("#disk-pct").textContent = r.disk.percent.toFixed(1);
    $("#disk-bar").className = barClass(r.disk.percent);
    $("#disk-bar").style.width = r.disk.percent + "%";
    $("#disk-meta").textContent = `${fmtBytes(r.disk.used)} / ${fmtBytes(r.disk.total)}`;

    // Temp
    const t = r.temp_c;
    $("#temp-val").textContent = t > 0 ? t.toFixed(1) : "—";
    const tPct = Math.min(100, (t / 90) * 100);
    $("#temp-bar").className = t >= 75 ? "bar-fill danger" : t >= 65 ? "bar-fill warn" : "bar-fill";
    $("#temp-bar").style.width = tPct + "%";
    $("#temp-meta").textContent = t >= 80 ? "⚠ throttle" : t >= 70 ? "warm" : "ok";

    // History + charts
    CPU_HIST.push(r.cpu.percent);
    MEM_HIST.push(r.mem.percent);
    if (CPU_HIST.length > MAX_POINTS) CPU_HIST.shift();
    if (MEM_HIST.length > MAX_POINTS) MEM_HIST.shift();
    drawChart("chart-cpu", CPU_HIST);
    drawChart("chart-mem", MEM_HIST);

    // Per-core
    $("#cores").innerHTML = r.cpu.per_core.map((pct, i) => `
      <div class="core">
        <div class="num">CPU ${i}</div>
        <div class="pct">${pct.toFixed(0)}<small style="font-size:11px">%</small></div>
        <div class="mini-bar"><div style="width:${pct}%;background:${pct>=85?'var(--danger)':pct>=65?'var(--warn)':'var(--accent-2)'}"></div></div>
      </div>
    `).join("");

    // Network
    const tbody = $("#net-table tbody");
    const nets = Object.entries(r.net || {}).sort();
    tbody.innerHTML = nets.length ? nets.map(([name, n]) => `
      <tr>
        <td class="mono">${name}</td>
        <td class="mono">${fmtBps(n.rx_bps)}</td>
        <td class="mono">${fmtBps(n.tx_bps)}</td>
        <td class="mono">${fmtBytes(n.rx_total)}</td>
        <td class="mono">${fmtBytes(n.tx_total)}</td>
      </tr>
    `).join("") : `<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:10px">—</td></tr>`;
  } catch (e) {
    console.error(e);
  }
}

async function pollProcs() {
  try {
    const r = await fetch("/api/processes").then(r => r.json());
    const sortBy = $("#proc-sort").value;
    const filter = $("#proc-filter").value.toLowerCase();
    let rows = r.processes;
    if (filter) {
      rows = rows.filter(p =>
        (p.name || "").toLowerCase().includes(filter) ||
        (p.user || "").toLowerCase().includes(filter) ||
        (p.cmd || "").toLowerCase().includes(filter)
      );
    }
    rows.sort((a, b) => (b[sortBy] || 0) - (a[sortBy] || 0));
    rows = rows.slice(0, 30);
    $("#proc-count").textContent = `${rows.length} / ${r.count} total`;
    const tbody = $("#proc-table tbody");
    tbody.innerHTML = rows.map(p => `
      <tr>
        <td class="pid">${p.pid}</td>
        <td class="user">${p.user}</td>
        <td>${p.name}</td>
        <td class="cpu-cell">${p.cpu.toFixed(1)}</td>
        <td class="mem-cell">${p.mem_pct.toFixed(1)}</td>
        <td class="mem-cell">${p.mem_mb.toFixed(1)}</td>
        <td><small>${p.status}</small></td>
        <td class="cmd" title="${(p.cmd||'').replace(/"/g,'')}">${p.cmd || p.name}</td>
      </tr>
    `).join("");
  } catch (e) {
    console.error(e);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  pollStats();
  pollProcs();
  setInterval(pollStats, 2000);
  setInterval(pollProcs, 3000);
});
