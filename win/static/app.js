const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
let NET = {};
let LAST = { cameras: [], others: [] };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status}: ${t}`);
  }
  return r.json();
}

async function loadNet() {
  NET = await api("/api/network");
  $("#net-info").innerHTML = `
    <div>Host: <b>${NET.hostname}</b></div>
    <div>LAN: <b>${NET.subnet}</b></div>
    <div>Tailscale: <b>${NET.tailscale_ip || "—"}</b></div>
  `;
  $("#host-info").textContent = NET.tailscale_ip
    ? `${NET.hostname} (${NET.tailscale_ip})`
    : NET.hostname;
  $("#subnet").placeholder = NET.subnet;
  $("#ts-hint").textContent = NET.tailscale_ip
    ? `Forward sẽ bind trên ${NET.tailscale_ip} (chỉ máy trong tailnet truy cập được)`
    : "⚠ Chưa có Tailscale IP";
}

function hostCard(h) {
  const ports = h.ports.map((p) => {
    const fp = (h.fingerprints || {})[p];
    const label = fp && fp.vendor
      ? `${p} · ${fp.vendor}`
      : p === 554 ? "554 · RTSP"
      : p === 37777 ? "37777 · Dahua"
      : p === 34567 ? "34567 · XMEye"
      : `${p}`;
    return `<span class="chip" data-ip="${h.ip}" data-port="${p}" data-vendor="${h.vendor_guess || ''}">${label}</span>`;
  }).join("");
  const detail = Object.entries(h.fingerprints || {}).map(([p, fp]) => {
    if (!fp.status) return "";
    const extras = [fp.server, fp.realm, fp.title].filter(Boolean).join(" · ");
    return `<div>${p} → HTTP ${fp.status} ${extras}</div>`;
  }).join("");
  return `
    <div class="host ${h.is_camera_candidate ? "cam" : ""}" data-ip="${h.ip}">
      <div class="host-head">
        <div>
          <span class="host-ip">${h.ip}</span>
          ${h.vendor_guess ? `<span class="host-vendor">${h.vendor_guess}</span>` : ""}
        </div>
        <div class="host-ports">${ports}</div>
      </div>
      ${detail ? `<div class="host-detail">${detail}</div>` : ""}
      <div class="probe-row" data-ip="${h.ip}"></div>
    </div>
  `;
}

async function doScan() {
  $("#scan-status").textContent = "scanning...";
  $("#scan-status").className = "status";
  $("#btn-scan").disabled = true;
  try {
    const subnet = $("#subnet").value.trim();
    const r = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ subnet }),
    });
    LAST = r;
    if (r.error) {
      $("#scan-results").innerHTML =
        `<div style="color:var(--danger);background:rgba(248,113,113,0.1);padding:14px;border-radius:6px;font-size:13px;line-height:1.5">
          ⚠ Không scan được: ${r.error.replace(/</g,'&lt;')}
        </div>`;
      $("#scan-status").textContent = "lỗi scanner";
      $("#scan-status").className = "status err";
      return;
    }
    const cams = r.cameras || [];
    const others = r.others || [];
    $("#scan-results").innerHTML =
      (cams.length ? `<h3 style="margin:4px 0;font-size:13px;color:var(--accent-2)">${cams.length} camera/DVR candidates</h3>` : `<div style="color:var(--muted);font-size:13px">Không phát hiện camera trên ${r.subnet}</div>`) +
      cams.map(hostCard).join("") +
      (others.length ? `<h3 style="margin:12px 0 4px;font-size:13px;color:var(--muted)">Thiết bị khác (${others.length})</h3>` + others.map(hostCard).join("") : "");
    $("#scan-status").textContent = `${cams.length} camera / ${r.total_hosts} host trong ${r.subnet}`;
    $("#scan-status").className = "status ok";
    attachChipHandlers();
  } catch (e) {
    $("#scan-status").textContent = "lỗi: " + e.message;
    $("#scan-status").className = "status err";
  } finally {
    $("#btn-scan").disabled = false;
  }
}

function attachChipHandlers() {
  $$(".chip").forEach((chip) => {
    chip.onclick = () => openForwardModal({
      ip: chip.dataset.ip,
      port: parseInt(chip.dataset.port),
      vendor: chip.dataset.vendor || "",
    });
  });
}

async function doProbeAll() {
  const user = $("#cred-user").value;
  const password = $("#cred-pass").value;
  if (!password) {
    alert("Nhập password trước");
    return;
  }
  const cams = LAST.cameras || [];
  if (!cams.length) {
    alert("Chưa có camera. Scan trước đã.");
    return;
  }
  for (const h of cams) {
    const row = document.querySelector(`.probe-row[data-ip="${h.ip}"]`);
    const httpPorts = h.ports.filter((p) => [80, 81, 82, 443, 8000, 8080, 8081, 8899].includes(p));
    row.innerHTML = `<small>probing ${httpPorts.join(",")}...</small>`;
    const results = [];
    for (const p of httpPorts) {
      try {
        const r = await api("/api/probe", {
          method: "POST",
          body: JSON.stringify({
            ip: h.ip, port: p, user, password, vendor: h.vendor_guess || "",
          }),
        });
        results.push({ port: p, ...r });
      } catch (e) {
        results.push({ port: p, ok: false, info: e.message });
      }
    }
    row.innerHTML = results.map((r) => {
      const cls = r.ok ? "ok" : "err";
      const label = r.ok
        ? `✓ ${r.port} · ${r.auth} · ${r.endpoint}`
        : `✗ ${r.port} · ${r.status || "no"}`;
      return `<span class="chip ${cls}" title="${(r.info||'').replace(/"/g, '')}">${label}</span>`;
    }).join("");
  }
}

function openForwardModal({ ip, port, vendor }) {
  $("#m-name").value = `${vendor || "camera"}-${ip}-${port}`;
  $("#m-ip").value = ip;
  $("#m-port").value = port;
  $("#m-proto").value = "tcp";
  $("#m-notes").value = "";
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }

async function createForward() {
  try {
    await api("/api/forwards", {
      method: "POST",
      body: JSON.stringify({
        name: $("#m-name").value,
        target_ip: $("#m-ip").value,
        target_port: parseInt($("#m-port").value),
        protocol: $("#m-proto").value,
        notes: $("#m-notes").value,
      }),
    });
    closeModal();
    await refreshForwards();
  } catch (e) {
    alert("Lỗi: " + e.message);
  }
}

function urlFor(tsIp, listenPort, targetPort, proto) {
  const PROTO_MAP = {
    80: { scheme: "http", label: "" },
    8080: { scheme: "http", label: "" },
    8081: { scheme: "http", label: "" },
    8899: { scheme: "http", label: "" },
    443: { scheme: "https", label: "" },
    8443: { scheme: "https", label: "" },
    554: { scheme: "rtsp", label: "RTSP" },
    1935: { scheme: "rtmp", label: "RTMP" },
    22: { scheme: "ssh", label: "SSH" },
  };
  const BINARY_PROTO = {
    37777: { label: "Dahua SDK", client: "Dahua SmartPSS / DMSS / Config Tool" },
    37778: { label: "Dahua SDK (UDP)", client: "Dahua SmartPSS" },
    8000: { label: "Hikvision SDK", client: "iVMS-4200 / Hik-Connect" },
    8899: { label: "Hikvision/Dahua", client: "" },
    34567: { label: "XMEye", client: "XMEye / CMS" },
    9000: { label: "Milesight/other SDK", client: "vendor client" },
  };
  if (proto === "udp") {
    return { href: "", display: `${tsIp}:${listenPort} / UDP` };
  }
  if (BINARY_PROTO[targetPort]) {
    const b = BINARY_PROTO[targetPort];
    return {
      href: "",
      display: `${tsIp}:${listenPort}`,
      hint: `${b.label} · dùng ${b.client}`,
    };
  }
  const m = PROTO_MAP[targetPort] || { scheme: "http", label: "" };
  const path = m.scheme === "rtsp" ? "/" : "/";
  const href = `${m.scheme}://${tsIp}:${listenPort}${path}`;
  return { href, display: href, hint: m.label };
}

async function refreshForwards() {
  const r = await api("/api/forwards");
  const tbody = $("#fw-table tbody");
  if (!r.forwards.length) {
    tbody.innerHTML = `<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">Chưa có forward nào. Click vào port chip ở trên để tạo.</td></tr>`;
    return;
  }
  tbody.innerHTML = r.forwards.map((f) => {
    const u = urlFor(r.tailscale_ip, f.listen_port, f.target_port, f.protocol);
    const urlCell = u.href
      ? `<a class="url" href="${u.href}" target="_blank">${u.display}</a>${u.hint ? ` <small style="color:var(--muted)">(${u.hint})</small>` : ""}`
      : `<span class="mono">${u.display}</span>${u.hint ? `<br><small style="color:var(--warn)">⚠ ${u.hint}</small>` : ""}`;
    return `
      <tr>
        <td class="mono">${f.id}</td>
        <td>${f.name}</td>
        <td class="mono">${f.target_ip}:${f.target_port}</td>
        <td>${f.protocol.toUpperCase()}</td>
        <td>${urlCell}</td>
        <td><span class="pill ${f.alive ? "alive" : "dead"}">${f.alive ? "alive" : "dead"}</span></td>
        <td><button class="btn danger small" data-del="${f.id}">Xoá</button></td>
      </tr>
    `;
  }).join("");
  $$("button[data-del]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm(`Xoá forward #${b.dataset.del}?`)) return;
      await api(`/api/forwards/${b.dataset.del}`, { method: "DELETE" });
      refreshForwards();
    };
  });
}

async function refreshSystem() {
  const r = await api("/api/system-forwards");
  const onlyFw = $("#sys-only-forwards").checked;
  const tbody = $("#sys-table tbody");
  const rows = (r.forwards || []).filter((f) => !onlyFw || f.target);
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:20px">Không có port nào ${onlyFw ? "có target forward" : "đang listen"}.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((f) => {
    let urlCell = "—";
    if (f.target) {
      const [, tgtPortStr] = f.target.split(":");
      const tgtPort = parseInt(tgtPortStr);
      const u = urlFor(r.tailscale_ip, f.port, tgtPort, f.proto);
      urlCell = u.href
        ? `<a class="url" href="${u.href}" target="_blank">${u.display}</a>${u.hint ? ` <small style="color:var(--muted)">(${u.hint})</small>` : ""}`
        : `<span class="mono">${u.display}</span>${u.hint ? `<br><small style="color:var(--warn)">⚠ ${u.hint}</small>` : ""}`;
    }
    return `
      <tr>
        <td class="mono">${f.port}</td>
        <td>${f.proto.toUpperCase()}</td>
        <td class="mono">${f.bind}</td>
        <td>${f.process}</td>
        <td class="mono">${f.target || "—"}</td>
        <td>${urlCell}</td>
        <td>${f.managed ? '<span class="pill alive">yes</span>' : '<span class="pill dead">no</span>'}</td>
      </tr>
    `;
  }).join("");
}

document.addEventListener("DOMContentLoaded", () => {
  loadNet();
  refreshForwards();
  refreshSystem();
  $("#btn-scan").onclick = doScan;
  $("#btn-probe-all").onclick = doProbeAll;
  $("#btn-refresh-fw").onclick = refreshForwards;
  $("#btn-refresh-sys").onclick = refreshSystem;
  $("#sys-only-forwards").onchange = refreshSystem;
  $("#m-ok").onclick = createForward;
  $("#m-cancel").onclick = closeModal;
  $("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };
});
