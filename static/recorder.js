const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
let NVRS = [];
let CUR_NVR = null;
let CUR_SESSION = null;
let HLS = null;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: {"Content-Type":"application/json"}, ...opts });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

function fmtBytes(n) {
  if (!n) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + " MB";
  return (n/1024/1024/1024).toFixed(2) + " GB";
}
function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts*1000).toLocaleString();
}

async function loadNvrs() {
  NVRS = await api("/api/nvrs");
  const list = $("#nvr-list");
  if (!NVRS.length) {
    list.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:8px">Chưa có NVR nào. Click + Add.</div>`;
  } else {
    list.innerHTML = NVRS.map((n) => `
      <div class="nvr-item ${CUR_NVR?.id === n.id ? 'active' : ''}" data-id="${n.id}">
        <button class="del" data-del="${n.id}" title="Xoá">×</button>
        <div class="name">${n.name}</div>
        <div class="host">${n.host}:${n.rtsp_port} · ${n.user}</div>
        <span class="test-btn" data-test="${n.id}">Test</span>
        <span class="test-btn" data-edit="${n.id}" style="margin-left:4px">Sửa…</span>
        <span class="test-btn" data-codec="${n.id}" style="margin-left:4px">Codec…</span>
        <span class="test-btn" data-osd="${n.id}" style="margin-left:4px">OSD…</span>
        <span class="test-btn" data-logo="${n.id}" style="margin-left:4px">Logo…</span>
        <span class="test-btn" data-dash="${n.id}" style="margin-left:4px;background:rgba(80,180,255,0.15)">⚙ Dashboard</span>
      </div>
    `).join("");
    $$(".nvr-item").forEach((el) => {
      el.onclick = (e) => {
        if (e.target.dataset.del || e.target.dataset.test) return;
        CUR_NVR = NVRS.find(x => x.id == el.dataset.id);
        $("#cur-nvr").textContent = CUR_NVR.name;
        $$(".nvr-item").forEach(x => x.classList.remove("active"));
        el.classList.add("active");
      };
    });
    $$("button[data-del]").forEach((b) => {
      b.onclick = async () => {
        if (!confirm("Xoá NVR?")) return;
        await api(`/api/nvrs/${b.dataset.del}`, {method:"DELETE"});
        if (CUR_NVR?.id == b.dataset.del) { CUR_NVR = null; $("#cur-nvr").textContent = "chọn NVR"; }
        loadNvrs();
      };
    });
    $$("span[data-test]").forEach((b) => {
      b.onclick = async () => {
        const ch = $("#ch").value || 1;
        b.textContent = "testing..."; b.style.color = "var(--muted)";
        try {
          const r = await api(`/api/nvrs/${b.dataset.test}/test?channel=${ch}`, {method:"POST"});
          b.textContent = r.ok ? `✓ ${r.info.replace(/\n/g, " ")}` : `✗ ${r.error.slice(0,60)}`;
          b.style.color = r.ok ? "var(--accent-2)" : "var(--danger)";
        } catch (e) {
          b.textContent = `✗ ${e.message.slice(0,60)}`;
          b.style.color = "var(--danger)";
        }
      };
    });
    $$("span[data-codec]").forEach((b) => {
      b.onclick = () => openCodecModal(parseInt(b.dataset.codec));
    });
    $$("span[data-edit]").forEach((b) => {
      b.onclick = () => openEditModal(parseInt(b.dataset.edit));
    });
    $$("span[data-osd]").forEach((b) => {
      b.onclick = () => openOsdModal(parseInt(b.dataset.osd));
    });
    $$("span[data-logo]").forEach((b) => {
      b.onclick = () => openLogoModal(parseInt(b.dataset.logo));
    });
    $$("span[data-dash]").forEach((b) => {
      b.onclick = () => openDashModal(parseInt(b.dataset.dash));
    });
  }
}

// ============ NVR Dashboard (system/storage/image/record/reboot) ============
let DASH_NVR_ID = null;

async function openDashModal(nid) {
  DASH_NVR_ID = nid;
  const n = NVRS.find(x => x.id === nid);
  $("#dash-title").textContent = `Dashboard · ${n.name} (${n.host})`;
  // Reset all sections
  $("#dash-sys").innerHTML = '<span class="status">loading...</span>';
  $("#dash-storage").innerHTML = '<span class="status">loading...</span>';
  $("#dash-record").innerHTML = '<span class="status">loading...</span>';
  $("#dash-img-ch").value = 1;
  $("#dash-img").innerHTML = '<span class="status">chọn channel → load</span>';
  $("#nvr-dash-modal").classList.remove("hidden");
  // Lazy load — các section tải song song nhưng không block modal mở
  loadDashSystem();
  loadDashStorage();
  loadDashRecord();
}

function closeDashModal() { $("#nvr-dash-modal").classList.add("hidden"); }

async function loadDashSystem() {
  try {
    const r = await api(`/api/nvrs/${DASH_NVR_ID}/system-info`);
    const driftCls = Math.abs(r.drift_seconds || 0) > 30 ? "err" : "ok";
    const driftStr = r.drift_seconds == null ? "?" :
      (r.drift_seconds >= 0 ? `+${r.drift_seconds}s` : `${r.drift_seconds}s`);
    $("#dash-sys").innerHTML = `
      <div class="kv"><span>Model:</span><b>${r.device_type || "?"}</b></div>
      <div class="kv"><span>Serial:</span><code>${r.serial || "?"}</code></div>
      <div class="kv"><span>Firmware:</span><code style="font-size:11px">${r.firmware || "?"}</code></div>
      <div class="kv"><span>Giờ Pi:</span><code>${r.pi_time}</code></div>
      <div class="kv"><span>Giờ NVR:</span><code>${r.nvr_time}</code> <span class="pill ${driftCls}">${driftStr}</span></div>
      ${Math.abs(r.drift_seconds || 0) > 30 ?
        `<button class="btn small" id="dash-sync-time" style="margin-top:6px">🕐 Đồng bộ giờ NVR = giờ Pi</button>` : ""}
    `;
    if ($("#dash-sync-time")) {
      $("#dash-sync-time").onclick = async () => {
        const r = await api(`/api/nvrs/${DASH_NVR_ID}/sync-time`, {method:"POST"});
        alert(r.ok ? `✓ Đã set giờ NVR = ${r.set_to}` : `✗ ${r.resp}`);
        loadDashSystem();
      };
    }
  } catch (e) {
    $("#dash-sys").innerHTML = `<span class="status err">✗ ${e.message}</span>`;
  }
}

async function loadDashStorage() {
  try {
    const r = await api(`/api/nvrs/${DASH_NVR_ID}/storage`);
    if (!r.disks?.length) {
      $("#dash-storage").innerHTML = '<span class="status">Không có HDD</span>';
      return;
    }
    $("#dash-storage").innerHTML = r.disks.map(d => {
      const pctCls = d.used_pct > 95 ? "err" : (d.used_pct > 80 ? "warn" : "ok");
      const healthIcon = d.healthy ? "✓" : "⚠";
      // NVR ghi loop → used=100% là bình thường, state=Success mới quan trọng
      const note = d.used_pct > 99 ? ' <small style="color:var(--muted)">(bình thường: NVR ghi loop)</small>' : '';
      return `
        <div class="kv"><span>${d.name}:</span><b>${d.total_gb} GB</b> ${healthIcon} ${d.state}</div>
        <div style="background:var(--bg);border-radius:4px;height:18px;overflow:hidden;margin:4px 0 10px">
          <div class="pill ${pctCls}" style="width:${Math.min(100,d.used_pct)}%;height:100%;border-radius:0;display:flex;align-items:center;padding:0 8px;font-size:11px">
            ${d.used_pct}% · ${d.used_gb}/${d.total_gb} GB${note}
          </div>
        </div>`;
    }).join("");
  } catch (e) {
    $("#dash-storage").innerHTML = `<span class="status err">✗ ${e.message}</span>`;
  }
}

async function loadDashImage() {
  const ch = parseInt($("#dash-img-ch").value) || 1;
  $("#dash-img").innerHTML = '<span class="status">loading...</span>';
  try {
    const r = await api(`/api/nvrs/${DASH_NVR_ID}/image?channel=${ch}`);
    $("#dash-img").innerHTML = `
      <div class="kv"><span>Brightness:</span><input type="range" min="0" max="100" value="${r.brightness}" id="img-br"><code id="img-br-v">${r.brightness}</code></div>
      <div class="kv"><span>Contrast:</span><input type="range" min="0" max="100" value="${r.contrast}" id="img-ct"><code id="img-ct-v">${r.contrast}</code></div>
      <div class="kv"><span>Saturation:</span><input type="range" min="0" max="100" value="${r.saturation}" id="img-st"><code id="img-st-v">${r.saturation}</code></div>
      <div class="kv"><span>Hue:</span><input type="range" min="0" max="100" value="${r.hue}" id="img-hu"><code id="img-hu-v">${r.hue}</code></div>
      <div class="kv"><span>Day/Night:</span>
        <select id="img-dn">
          <option value="0" ${r.day_night_mode===0?"selected":""}>Auto (IR tự động)</option>
          <option value="1" ${r.day_night_mode===1?"selected":""}>Màu (ban ngày)</option>
          <option value="2" ${r.day_night_mode===2?"selected":""}>Trắng đen (đêm)</option>
        </select>
      </div>
      <button class="btn primary small" id="img-apply" style="margin-top:8px">Áp dụng</button>
      <small style="color:var(--muted);display:block;margin-top:4px">Áp dụng cho cả 4 time section — hiệu lực ngay sau khi lưu.</small>
    `;
    ["br","ct","st","hu"].forEach(k => {
      const el = $("#img-"+k);
      el.oninput = () => $("#img-"+k+"-v").textContent = el.value;
    });
    $("#img-apply").onclick = async () => {
      const body = {
        channel: ch,
        brightness: parseInt($("#img-br").value),
        contrast:   parseInt($("#img-ct").value),
        saturation: parseInt($("#img-st").value),
        hue:        parseInt($("#img-hu").value),
        day_night_mode: parseInt($("#img-dn").value),
      };
      try {
        const r = await api(`/api/nvrs/${DASH_NVR_ID}/image`, {
          method: "PUT", body: JSON.stringify(body)});
        alert(r.ok ? `✓ Applied ${r.applied} fields` : `✗ ${r.resp}`);
      } catch (e) { alert("Lỗi: " + e.message); }
    };
  } catch (e) {
    $("#dash-img").innerHTML = `<span class="status err">✗ ${e.message}</span>`;
  }
}

async function loadDashRecord() {
  try {
    const r = await api(`/api/nvrs/${DASH_NVR_ID}/record`);
    const chs = Object.keys(r.modes).map(k => parseInt(k)).sort((a,b)=>a-b);
    if (!chs.length) {
      $("#dash-record").innerHTML = '<span class="status">Không đọc được RecordMode</span>';
      return;
    }
    const modeOpts = (sel) => `
      <option value="0" ${sel===0?"selected":""}>Auto (theo lịch)</option>
      <option value="1" ${sel===1?"selected":""}>Luôn ghi (manual)</option>
      <option value="2" ${sel===2?"selected":""}>Tắt ghi</option>`;
    $("#dash-record").innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
        <small style="color:var(--muted)">Set tất cả:</small>
        <select id="rec-all"><option value="">—</option>${modeOpts(-1)}</select>
        <button class="btn small" id="rec-apply-all">Áp dụng tất cả</button>
      </div>
      <table style="width:100%;font-size:12px">
        <thead><tr><th style="width:40px">Ch</th><th>Record mode</th></tr></thead>
        <tbody>${chs.map(c => `
          <tr>
            <td style="text-align:center;font-family:var(--mono)">${c}</td>
            <td><select class="rec-ch" data-ch="${c}">${modeOpts(r.modes[c])}</select></td>
          </tr>`).join("")}</tbody>
      </table>
    `;
    $$(".rec-ch").forEach(sel => {
      sel.onchange = async () => {
        const body = {channel: parseInt(sel.dataset.ch), mode: parseInt(sel.value)};
        try {
          const r = await api(`/api/nvrs/${DASH_NVR_ID}/record`, {
            method: "PUT", body: JSON.stringify(body)});
          sel.style.background = r.ok ? "rgba(74,222,128,0.1)" : "rgba(248,113,113,0.1)";
          setTimeout(() => sel.style.background = "", 1200);
        } catch (e) { alert("Lỗi: " + e.message); }
      };
    });
    $("#rec-apply-all").onclick = async () => {
      const m = $("#rec-all").value;
      if (m === "") { alert("Chọn mode trước"); return; }
      if (!confirm(`Set tất cả ${chs.length} channel → mode ${m}?`)) return;
      try {
        const r = await api(`/api/nvrs/${DASH_NVR_ID}/record`, {
          method: "PUT", body: JSON.stringify({all: parseInt(m)})});
        alert(r.ok ? `✓ Set ${r.applied} channels` : `✗ ${r.resp}`);
        loadDashRecord();
      } catch (e) { alert("Lỗi: " + e.message); }
    };
  } catch (e) {
    $("#dash-record").innerHTML = `<span class="status err">✗ ${e.message}</span>`;
  }
}

async function rebootNvr() {
  if (!confirm("⚠ Reboot NVR? Mất 60-120s để NVR khởi động lại, trong lúc đó\n- Mọi cam offline\n- Clips đang chạy sẽ fail\n- Khách clip bị gián đoạn")) return;
  if (!confirm("Chắc chắn chưa? Lần cuối hỏi.")) return;
  try {
    const r = await api(`/api/nvrs/${DASH_NVR_ID}/reboot`, {method:"POST"});
    alert(r.ok ? "✓ NVR đã nhận lệnh reboot. Đợi 60-120s rồi kiểm tra." : `✗ ${r.resp || r.note}`);
    closeDashModal();
  } catch (e) {
    // Thường reboot ngắt connection → coi như thành công
    alert("✓ NVR đã nhận lệnh (kết nối bị cắt khi reboot). Đợi 60-120s.");
    closeDashModal();
  }
}

// ---------- Logo editor (Dahua text watermark) ----------
let LOGO_NVR_ID = null;
function openLogoModal(nid) {
  LOGO_NVR_ID = nid;
  const n = NVRS.find(x => x.id === nid);
  $("#logo-nvr-info").textContent = n ? `${n.name} — ${n.host}` : "";
  $("#lg-ch").value = parseInt($("#ch").value) || 1;
  $("#lg-current").textContent = "Chưa load";
  $("#lg-status").textContent = "";
  $("#logo-modal").classList.remove("hidden");
}
function closeLogoModal() { $("#logo-modal").classList.add("hidden"); }

async function loadLogo() {
  const ch = parseInt($("#lg-ch").value) || 1;
  $("#lg-status").textContent = "loading..."; $("#lg-status").className = "status";
  try {
    const r = await api(`/api/nvrs/${LOGO_NVR_ID}/logo?channel=${ch}`);
    $("#lg-current").innerHTML = `
      <div><b>Hiện tại:</b> ${r.enabled ? "BẬT" : "tắt"}</div>
      <div><b>Text:</b> "${r.text || "(trống)"}"</div>
      <div><b>Rect:</b> [${(r.rect||[]).join(", ")}]</div>
      <div><b>Màu:</b> RGBA(${(r.color||[]).join(", ")})</div>
    `;
    if (r.text) $("#lg-text").value = r.text;
    $("#lg-status").textContent = "✓ loaded"; $("#lg-status").className = "status ok";
  } catch (e) {
    $("#lg-status").textContent = "lỗi: " + e.message;
    $("#lg-status").className = "status err";
  }
}

function _logoPayload(includeChannel) {
  const text = $("#lg-text").value;
  const position = $("#lg-pos").value;
  const color = $("#lg-color").value.split(",").map(x => parseInt(x));
  const p = { text, position, color };
  if (includeChannel) p.channel = parseInt($("#lg-ch").value) || 1;
  return p;
}

async function applyLogo() {
  $("#lg-status").textContent = "applying..."; $("#lg-status").className = "status";
  try {
    const r = await api(`/api/nvrs/${LOGO_NVR_ID}/logo`, {
      method: "PUT", body: JSON.stringify(_logoPayload(true)),
    });
    if (r.ok) {
      $("#lg-status").textContent = "✓ applied — đợi NVR áp dụng ~3s";
      $("#lg-status").className = "status ok";
      setTimeout(loadLogo, 3000);
    } else {
      $("#lg-status").textContent = "✗ " + (r.response || r.error);
      $("#lg-status").className = "status err";
    }
  } catch (e) {
    $("#lg-status").textContent = "lỗi: " + e.message;
    $("#lg-status").className = "status err";
  }
}

async function applyLogoAll() {
  if (!confirm("Áp dụng logo cho TẤT CẢ channel đã add trong DB của NVR này?")) return;
  $("#lg-status").textContent = "applying to all channels..."; $("#lg-status").className = "status";
  try {
    const r = await api(`/api/nvrs/${LOGO_NVR_ID}/logo/apply-all`, {
      method: "POST", body: JSON.stringify(_logoPayload(false)),
    });
    $("#lg-status").textContent = r.ok
      ? `✓ ${r.applied}/${r.total} channels OK`
      : `⚠ ${r.applied}/${r.total} OK — xem console`;
    $("#lg-status").className = r.ok ? "status ok" : "status err";
    if (!r.ok) console.warn("logo apply-all results:", r.results);
    setTimeout(loadLogo, 3000);
  } catch (e) {
    $("#lg-status").textContent = "lỗi: " + e.message;
    $("#lg-status").className = "status err";
  }
}

// ---------- OSD editor (bulk — tất cả kênh) ----------
let OSD_NVR_ID = null;
let OSD_ORIGINAL = []; // snapshot để diff khi apply

function openOsdModal(nid) {
  OSD_NVR_ID = nid;
  const n = NVRS.find(x => x.id === nid);
  $("#osd-nvr-info").textContent = n ? `${n.name} — ${n.host}` : "";
  $("#osd-tbody").innerHTML =
    `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">
       Đang tải từ NVR…</td></tr>`;
  $("#osd-status").textContent = "";
  $("#osd-modal").classList.remove("hidden");
  loadOsdAll(); // tự load ngay
}
function closeOsdModal() { $("#osd-modal").classList.add("hidden"); }

function _escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function loadOsdAll() {
  $("#osd-status").textContent = "đang tải tất cả kênh…";
  $("#osd-status").className = "status";
  try {
    const r = await api(`/api/nvrs/${OSD_NVR_ID}/osd/all`);
    OSD_ORIGINAL = r.channels || [];
    renderOsdTable(OSD_ORIGINAL);
    $("#osd-status").textContent = `✓ ${r.count} kênh`;
    $("#osd-status").className = "status ok";
  } catch (e) {
    $("#osd-tbody").innerHTML =
      `<tr><td colspan="5" style="text-align:center;color:var(--danger);padding:20px">
         Lỗi: ${_escHtml(e.message)}</td></tr>`;
    $("#osd-status").textContent = "lỗi: " + e.message;
    $("#osd-status").className = "status err";
  }
}

function renderOsdTable(rows) {
  if (!rows.length) {
    $("#osd-tbody").innerHTML =
      `<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:20px">
         Không có kênh nào.</td></tr>`;
    return;
  }
  $("#osd-tbody").innerHTML = rows.map(r => `
    <tr data-ch="${r.channel}">
      <td class="mono" style="text-align:center">${r.channel}</td>
      <td>${_escHtml(r.display_name || r.channel_name) || "<i style='color:var(--muted)'>(trống)</i>"}</td>
      <td><input class="osd-name" type="text" placeholder="(giữ nguyên)"
                 style="width:100%;padding:4px 6px"></td>
      <td style="text-align:center">
        <input class="osd-ct" type="checkbox" ${r.channel_title ? "checked" : ""}></td>
      <td style="text-align:center">
        <input class="osd-tt" type="checkbox" ${r.time_title ? "checked" : ""}></td>
    </tr>
  `).join("");
}

async function applyOsd() {
  // Thu diff từ OSD_ORIGINAL vs state hiện tại trong table
  const changes = [];
  for (const orig of OSD_ORIGINAL) {
    const tr = $(`#osd-tbody tr[data-ch="${orig.channel}"]`);
    if (!tr) continue;
    const newName = tr.querySelector(".osd-name").value.trim();
    const ct = tr.querySelector(".osd-ct").checked;
    const tt = tr.querySelector(".osd-tt").checked;
    const item = { channel: orig.channel };
    let changed = false;
    if (newName && newName !== orig.channel_name) {
      item.channel_name = newName; changed = true;
    }
    if (ct !== !!orig.channel_title) {
      item.channel_title = ct; changed = true;
    }
    if (tt !== !!orig.time_title) {
      item.time_title = tt; changed = true;
    }
    if (changed) changes.push(item);
  }
  if (!changes.length) {
    $("#osd-status").textContent = "không có gì để đổi";
    $("#osd-status").className = "status";
    return;
  }
  $("#osd-status").textContent = `đang áp dụng ${changes.length} thay đổi…`;
  $("#osd-status").className = "status";
  try {
    const r = await api(`/api/nvrs/${OSD_NVR_ID}/osd/bulk`, {
      method: "PUT", body: JSON.stringify({ changes }),
    });
    if (r.ok) {
      $("#osd-status").textContent = `✓ áp dụng ${r.applied} kênh`;
      $("#osd-status").className = "status ok";
      setTimeout(loadOsdAll, 2500); // reload hiển thị state mới
    } else {
      $("#osd-status").textContent = "✗ " + (r.response || r.error);
      $("#osd-status").className = "status err";
    }
  } catch (e) {
    $("#osd-status").textContent = "lỗi: " + e.message;
    $("#osd-status").className = "status err";
  }
}

// ---------- Codec editor (Dahua) ----------
let CODEC_NVR_ID = null;
function openCodecModal(nid) {
  CODEC_NVR_ID = nid;
  const n = NVRS.find(x => x.id === nid);
  $("#codec-nvr-info").textContent = n ? `${n.name} — ${n.host}` : "";
  $("#cd-ch").value = parseInt($("#ch").value) || 1;
  $("#cd-main").value = "";
  $("#cd-sub").value = "";
  $("#cd-current").textContent = "Chưa load";
  $("#cd-status").textContent = "";
  $("#codec-modal").classList.remove("hidden");
}
function closeCodecModal() { $("#codec-modal").classList.add("hidden"); }

async function loadCodec() {
  const ch = parseInt($("#cd-ch").value) || 1;
  $("#cd-status").textContent = "loading..."; $("#cd-status").className = "status";
  try {
    const r = await api(`/api/nvrs/${CODEC_NVR_ID}/encode?channel=${ch}`);
    const main = r.main || {};
    const sub = r.sub || {};
    $("#cd-current").innerHTML = `
      <div><b>Main:</b> ${main.codec || "?"} · ${main.resolution || "?"} · ${main.fps || "?"}fps · ${main.bitrate || "?"}kbps</div>
      <div><b>Sub:</b> ${sub.codec || "?"} · ${sub.resolution || "?"} · ${sub.fps || "?"}fps · ${sub.bitrate || "?"}kbps</div>
    `;
    $("#cd-status").textContent = "✓ loaded";
    $("#cd-status").className = "status ok";
  } catch (e) {
    $("#cd-status").textContent = "lỗi: " + e.message;
    $("#cd-status").className = "status err";
  }
}

async function applyCodec() {
  const ch = parseInt($("#cd-ch").value) || 1;
  const main = $("#cd-main").value;
  const sub = $("#cd-sub").value;
  if (!main && !sub) { alert("Chọn ít nhất 1 stream để đổi"); return; }
  $("#cd-status").textContent = "applying..."; $("#cd-status").className = "status";
  try {
    const r = await api(`/api/nvrs/${CODEC_NVR_ID}/encode`, {
      method:"PUT",
      body: JSON.stringify({channel: ch, main: main || undefined, sub: sub || undefined}),
    });
    if (r.ok) {
      $("#cd-status").textContent = "✓ applied — đợi NVR áp dụng";
      $("#cd-status").className = "status ok";
      setTimeout(loadCodec, 3000);
    } else {
      $("#cd-status").textContent = "✗ " + (r.response || r.error);
      $("#cd-status").className = "status err";
    }
  } catch (e) {
    $("#cd-status").textContent = "lỗi: " + e.message;
    $("#cd-status").className = "status err";
  }
}

let EDIT_NVR_ID = null;

function openModal() {
  EDIT_NVR_ID = null;
  $("#n-title").textContent = "Thêm NVR";
  ["n-name","n-host","n-user","n-pass","n-wm"].forEach(id => $("#"+id).value = "");
  $("#n-port").value = 554; $("#n-ch").value = 16; $("#n-user").value = "admin";
  $("#n-wm-pos").value = "bl";
  $("#n-wm-size").value = "40";
  $("#n-wm-scroll").checked = false;
  $("#n-source").value = "rtsp";
  $("#n-encoder").value = "libx264";
  $("#n-pass").placeholder = "";
  $("#n-pass-hint").style.display = "none";
  $("#nvr-modal").classList.remove("hidden");
}
function openEditModal(nid) {
  const n = NVRS.find(x => x.id === nid);
  if (!n) return;
  EDIT_NVR_ID = nid;
  $("#n-title").textContent = `Sửa NVR: ${n.name}`;
  $("#n-name").value = n.name;
  $("#n-host").value = n.host;
  $("#n-port").value = n.rtsp_port;
  $("#n-user").value = n.user;
  $("#n-pass").value = "";
  $("#n-pass").placeholder = "(để trống nếu giữ nguyên)";
  $("#n-pass-hint").style.display = "block";
  $("#n-ch").value = n.channels;
  $("#n-wm").value = n.clip_watermark || "";
  $("#n-wm-pos").value = n.clip_wm_pos || "bl";
  $("#n-wm-size").value = String(n.clip_wm_size || 40);
  $("#n-wm-scroll").checked = !!n.clip_wm_scroll;
  $("#n-source").value = n.clip_source || "rtsp";
  $("#n-encoder").value = n.clip_encoder || "libx264";
  $("#nvr-modal").classList.remove("hidden");
}
function closeModal() {
  $("#nvr-modal").classList.add("hidden");
  // Reset scan panel
  $("#n-scan-result").style.display = "none";
  $("#n-scan-body").innerHTML = "";
  $("#n-scan-hint").textContent = "Bấm \"Scan ngay\" để NVR trả về list channel đã đặt tên. Tick những sân muốn thêm/update vào list.";
  $("#n-scan-hint").style.color = "var(--muted)";
}

/* --- Scan channels từ NVR --- */
async function scanChannels() {
  const btn = $("#n-scan");
  const hint = $("#n-scan-hint");
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = "⏳ Đang scan...";
  hint.textContent = "Đang gọi API NVR...";
  hint.style.color = "var(--muted)";
  try {
    let resp;
    if (EDIT_NVR_ID) {
      // Dùng creds đã lưu trong DB
      resp = await api(`/api/nvrs/${EDIT_NVR_ID}/channels-remote`);
    } else {
      // Chưa save NVR → gửi raw creds
      const host = $("#n-host").value.trim();
      const user = $("#n-user").value.trim();
      const pw = $("#n-pass").value;
      if (!host || !user || !pw) {
        hint.textContent = "⚠ Nhập host, user, password trước khi scan.";
        hint.style.color = "var(--err,#e55)";
        return;
      }
      resp = await api("/api/nvrs/scan-channels", {
        method: "POST",
        body: JSON.stringify({host, user, password: pw}),
      });
    }
    renderScanResult(resp.channels || []);
    hint.textContent = `✓ Tìm thấy ${resp.channels.length} channel. Tick những sân muốn thêm.`;
    hint.style.color = "var(--ok,#4e4)";
  } catch (e) {
    hint.textContent = "✗ Lỗi: " + e.message;
    hint.style.color = "var(--err,#e55)";
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

async function scanChannelsOnvif() {
  const btn = $("#n-scan-onvif");
  const hint = $("#n-scan-hint");
  const host = $("#n-host").value.trim();
  const user = $("#n-user").value.trim();
  const pw = $("#n-pass").value;
  if (!host || !user || !pw) {
    hint.textContent = "⚠ Nhập host, user, password trước khi scan ONVIF.";
    hint.style.color = "var(--err,#e55)";
    return;
  }
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = "⏳";
  hint.textContent = "Đang gọi ONVIF SOAP...";
  hint.style.color = "var(--muted)";
  try {
    const r = await api("/api/nvrs/scan-onvif", {
      method: "POST",
      body: JSON.stringify({host, user, password: pw}),
    });
    // ONVIF trả về profiles — map thành channel-like format
    // Mỗi profile = 1 "channel" ảo (gộp main/sub cùng cam)
    const chs = (r.profiles || []).map((p, idx) => ({
      channel: idx + 1,
      title: `${p.name || p.token} (${p.width}x${p.height} ${p.encoding})`,
      in_local: false,
      local_name: p.name || `ONVIF Ch${idx+1}`,
      local_subtype: 0,
    }));
    if (!chs.length) {
      hint.textContent = "⚠ ONVIF trả về 0 profile — NVR chưa enable ONVIF hoặc port khác 80.";
      hint.style.color = "var(--err,#e55)";
      return;
    }
    renderScanResult(chs);
    hint.innerHTML = `✓ Tìm thấy ${chs.length} ONVIF profile. ` +
      `<small>RTSP URLs lưu trong response (xem console).</small>`;
    hint.style.color = "var(--ok,#4e4)";
    console.log("ONVIF profiles:", r.profiles);
  } catch (e) {
    hint.textContent = "✗ Lỗi: " + e.message;
    hint.style.color = "var(--err,#e55)";
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

function renderScanResult(channels) {
  const body = $("#n-scan-body");
  body.innerHTML = "";
  for (const c of channels) {
    const tr = document.createElement("tr");
    const checked = c.in_local ? "checked" : "";
    const displayName = c.local_name || c.title;
    const subtype = c.local_subtype || 0;
    // Highlight dòng đã có trong DB bằng background xanh nhạt
    if (c.in_local) tr.style.background = "rgba(80,180,255,0.08)";
    tr.innerHTML = `
      <td style="text-align:center">
        <input type="checkbox" class="scan-ck" data-ch="${c.channel}" ${checked}>
      </td>
      <td style="text-align:center;color:var(--muted);font-family:var(--mono)">${c.channel}</td>
      <td>
        <input type="text" class="scan-name" data-ch="${c.channel}"
               value="${escapeHtml(displayName)}">
      </td>
      <td>
        <select class="scan-sub" data-ch="${c.channel}">
          <option value="0" ${subtype===0?"selected":""}>Main</option>
          <option value="1" ${subtype===1?"selected":""}>Sub</option>
        </select>
      </td>`;
    body.appendChild(tr);
  }
  $("#n-scan-result").style.display = "block";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
}

async function saveNvr() {
  const body = {
    name: $("#n-name").value.trim(),
    host: $("#n-host").value.trim(),
    rtsp_port: parseInt($("#n-port").value),
    user: $("#n-user").value.trim(),
    password: $("#n-pass").value,
    channels: parseInt($("#n-ch").value),
    clip_watermark: $("#n-wm").value,
    clip_wm_pos: $("#n-wm-pos").value,
    clip_wm_size: parseInt($("#n-wm-size").value) || 40,
    clip_wm_scroll: $("#n-wm-scroll").checked ? 1 : 0,
    clip_source: $("#n-source").value,
    clip_encoder: $("#n-encoder").value,
  };
  // Collect ticked channels from scan panel
  const scanRows = document.querySelectorAll("#n-scan-body tr");
  const channelsToUpsert = [];
  for (const tr of scanRows) {
    const ck = tr.querySelector(".scan-ck");
    if (!ck || !ck.checked) continue;
    const ch = parseInt(ck.dataset.ch);
    const name = tr.querySelector(".scan-name").value.trim();
    const sub = parseInt(tr.querySelector(".scan-sub").value);
    if (name) channelsToUpsert.push({channel: ch, name, subtype: sub});
  }

  try {
    let nvrId = EDIT_NVR_ID;
    if (nvrId) {
      // PATCH — if password blank, backend keeps old
      await api(`/api/nvrs/${nvrId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
    } else {
      if (!body.password) { alert("Nhập password"); return; }
      const r = await api("/api/nvrs", {
        method: "POST",
        body: JSON.stringify(body),
      });
      nvrId = r.id;
    }
    // Bulk upsert channels nếu có tick
    if (channelsToUpsert.length) {
      const res = await api(`/api/nvrs/${nvrId}/channels-bulk`, {
        method: "POST",
        body: JSON.stringify({channels: channelsToUpsert}),
      });
      const msgs = [];
      if (res.added?.length) msgs.push(`thêm ${res.added.length} sân`);
      if (res.updated?.length) msgs.push(`cập nhật ${res.updated.length} sân`);
      if (res.errors?.length) msgs.push(`⚠ ${res.errors.length} lỗi`);
      if (msgs.length) console.log("Bulk channels:", msgs.join(", "));
    }
    closeModal();
    await loadNvrs();
    await loadChannels();
  } catch (e) { alert("Lỗi: " + e.message); }
}

function toDahuaDt(local) {
  // "2026-10-26T10:30:00" or "2026-10-26T10:30" → "2026-10-26 10:30:00"
  if (!local) return "";
  let s = local.replace("T", " ");
  if (s.length === 16) s += ":00";
  return s;
}

function teardownPlayer() {
  const v = $("#video");
  if (HLS) { HLS.destroy(); HLS = null; }
  v.pause(); v.removeAttribute("src"); v.load();
  if (CUR_SESSION) {
    api("/api/stream/stop", {method:"POST", body:JSON.stringify({session_id:CUR_SESSION})}).catch(()=>{});
    CUR_SESSION = null;
  }
}

async function doPlay() {
  if (!CUR_NVR) { alert("Chọn NVR trước"); return; }
  teardownPlayer();
  const mode = $("#mode").value;
  const payload = {
    nvr_id: CUR_NVR.id,
    channel: parseInt($("#ch").value),
    mode: mode,
    subtype: parseInt($("#subtype").value),
  };
  if (mode === "playback") {
    payload.start = toDahuaDt($("#start").value);
    payload.end = toDahuaDt($("#end").value);
    if (!payload.start || !payload.end) { alert("Chọn start/end"); return; }
  }
  $("#stream-status").textContent = "starting ffmpeg...";
  $("#stream-status").className = "status";
  try {
    const r = await api("/api/stream/start", {method:"POST", body:JSON.stringify(payload)});
    CUR_SESSION = r.session_id;
    const v = $("#video");
    if (Hls.isSupported()) {
      HLS = new Hls({ liveSyncDuration: 2, liveMaxLatencyDuration: 6, lowLatencyMode: true });
      HLS.loadSource(r.manifest); HLS.attachMedia(v);
      HLS.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(()=>{}));
      HLS.on(Hls.Events.ERROR, (_, d) => {
        if (d.fatal) {
          $("#stream-status").textContent = "HLS error: " + d.details;
          $("#stream-status").className = "status err";
        }
      });
    } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = r.manifest; v.play().catch(()=>{});
    }
    $("#stream-status").textContent = `playing ${mode} ch${payload.channel}`;
    $("#stream-status").className = "status ok";
  } catch (e) {
    $("#stream-status").textContent = "lỗi: " + e.message;
    $("#stream-status").className = "status err";
  }
}

async function doClip() {
  if (!CUR_NVR) { alert("Chọn NVR trước"); return; }
  const mode = $("#mode").value;
  const payload = {
    nvr_id: CUR_NVR.id,
    channel: parseInt($("#ch").value),
    mode: mode,
    subtype: parseInt($("#subtype").value),
  };
  if (mode === "playback") {
    payload.start = toDahuaDt($("#start").value);
    payload.end = toDahuaDt($("#end").value);
    if (!payload.start || !payload.end) { alert("Chọn start/end"); return; }
  } else {
    payload.duration = parseInt($("#clip-dur").value) || 30;
  }
  try {
    const r = await api("/api/clip", {method:"POST", body:JSON.stringify(payload)});
    $("#stream-status").textContent = `clip queued: ${r.name}`;
    $("#stream-status").className = "status ok";
    setTimeout(refreshClips, 500);
  } catch (e) {
    alert("Lỗi: " + e.message);
  }
}

async function refreshClips() {
  const clips = await api("/api/clips");
  const tbody = $("#clip-table tbody");
  if (!clips.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="color:var(--muted);text-align:center;padding:16px">Chưa có clip.</td></tr>`;
    return;
  }
  tbody.innerHTML = clips.map((c) => {
    const action = c.status === "done"
      ? `<a class="btn small" href="${c.download_url}" download>⬇ Download</a>`
      : c.status === "running"
      ? `<small style="color:var(--warn)">ffmpeg...</small>`
      : `<small class="mono" title="${(c.err||'').replace(/"/g,'')}" style="color:var(--danger)">err</small>`;
    return `<tr>
      <td><span class="clip-pill ${c.status}">${c.status}</span></td>
      <td>${c.nvr || "—"}</td>
      <td>${c.channel || "—"}</td>
      <td>${c.mode || "—"}</td>
      <td class="mono">${c.name}</td>
      <td>${fmtBytes(c.size)}</td>
      <td>${fmtTime(c.created)}</td>
      <td>${action} ${c.id ? `<button class="btn small danger" data-delclip="${c.id}">×</button>` : ""}</td>
    </tr>`;
  }).join("");
  $$("button[data-delclip]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm("Xoá clip?")) return;
      await api(`/api/clips/${b.dataset.delclip}`, {method:"DELETE"});
      refreshClips();
    };
  });
}

function updateModeUI() {
  const mode = $("#mode").value;
  $$(".playback-only").forEach(e => e.style.display = mode === "playback" ? "" : "none");
  $$(".live-only").forEach(e => e.style.display = mode === "live" ? "" : "none");
}

// ---------- Branding ----------
async function loadBranding() {
  const r = await api("/api/settings");
  for (const k of ["brand_title", "brand_tagline", "brand_footer", "brand_footer_url"]) {
    const el = document.getElementById(k);
    if (el) el.value = r.settings[k] || r.defaults[k] || "";
  }
}

async function saveBranding() {
  const payload = {};
  for (const k of ["brand_title", "brand_tagline", "brand_footer", "brand_footer_url"]) {
    const el = document.getElementById(k);
    if (el) payload[k] = el.value;
  }
  try {
    await api("/api/settings", {method:"PUT", body: JSON.stringify(payload)});
    const s = document.getElementById("brand-status");
    s.textContent = "✓ đã lưu — reload trang khách để thấy";
    s.className = "status ok";
    setTimeout(() => s.textContent = "", 3000);
  } catch (e) {
    const s = document.getElementById("brand-status");
    s.textContent = "lỗi: " + e.message;
    s.className = "status err";
  }
}

// ---------- Channels ----------
async function loadChannels() {
  const list = await api("/api/channels");
  const tbody = $("#ch-table tbody");
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:var(--muted);text-align:center;padding:12px">Chưa có channel. Click + Thêm channel.</td></tr>`;
    return;
  }
  tbody.innerHTML = list.map(c => `
    <tr>
      <td><b>${c.name}</b></td>
      <td class="mono">${c.nvr_name}</td>
      <td class="mono">${c.channel}</td>
      <td>${c.subtype === 0 ? "Main" : "Sub"}</td>
      <td style="color:var(--muted)">${c.description || "—"}</td>
      <td><button class="btn small danger" data-chdel="${c.id}">Xoá</button></td>
    </tr>
  `).join("");
  $$("button[data-chdel]").forEach(b => {
    b.onclick = async () => {
      if (!confirm("Xoá channel?")) return;
      await api(`/api/channels/${b.dataset.chdel}`, {method:"DELETE"});
      loadChannels();
    };
  });
}

function openChannelModal() {
  if (!NVRS.length) { alert("Add NVR trước"); return; }
  const sel = $("#c-nvr");
  sel.innerHTML = NVRS.map(n => `<option value="${n.id}">${n.name}</option>`).join("");
  $("#c-name").value = "";
  $("#c-ch").value = 1;
  $("#c-sub").value = 0;
  $("#c-desc").value = "";
  $("#ch-modal").classList.remove("hidden");
}
function closeChannelModal() { $("#ch-modal").classList.add("hidden"); }

async function saveChannel() {
  try {
    await api("/api/channels", {
      method:"POST",
      body: JSON.stringify({
        name: $("#c-name").value.trim(),
        nvr_id: parseInt($("#c-nvr").value),
        channel: parseInt($("#c-ch").value),
        subtype: parseInt($("#c-sub").value),
        description: $("#c-desc").value.trim(),
      }),
    });
    closeChannelModal();
    loadChannels();
  } catch (e) { alert("Lỗi: " + e.message); }
}

document.addEventListener("DOMContentLoaded", async () => {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const nowStr = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:00`;
  const past = new Date(now.getTime() - 5*60000);
  const pastStr = `${past.getFullYear()}-${pad(past.getMonth()+1)}-${pad(past.getDate())}T${pad(past.getHours())}:${pad(past.getMinutes())}:00`;
  $("#start").value = pastStr; $("#end").value = nowStr;

  await loadNvrs();
  await loadChannels();
  await loadBranding();
  await refreshClips();
  updateModeUI();
  $("#footer-host").textContent = location.host;

  $("#btn-add-channel").onclick = openChannelModal;
  $("#c-cancel").onclick = closeChannelModal;
  $("#c-ok").onclick = saveChannel;
  $("#ch-modal").onclick = (e) => { if (e.target.id === "ch-modal") closeChannelModal(); };
  $("#btn-save-brand").onclick = saveBranding;

  $("#cd-cancel").onclick = closeCodecModal;
  $("#cd-load").onclick = loadCodec;
  $("#cd-apply").onclick = applyCodec;
  $("#codec-modal").onclick = (e) => { if (e.target.id === "codec-modal") closeCodecModal(); };

  $("#osd-cancel").onclick = closeOsdModal;
  $("#osd-reload").onclick = loadOsdAll;
  $("#osd-apply").onclick = applyOsd;
  $("#osd-modal").onclick = (e) => { if (e.target.id === "osd-modal") closeOsdModal(); };

  $("#lg-cancel").onclick = closeLogoModal;
  $("#lg-load").onclick = loadLogo;
  $("#lg-apply").onclick = applyLogo;
  $("#lg-apply-all").onclick = applyLogoAll;
  $("#logo-modal").onclick = (e) => { if (e.target.id === "logo-modal") closeLogoModal(); };

  $("#btn-add-nvr").onclick = openModal;
  $("#n-cancel").onclick = closeModal;
  $("#n-ok").onclick = saveNvr;
  $("#n-scan").onclick = scanChannels;
  $("#n-scan-onvif").onclick = scanChannelsOnvif;
  $("#n-scan-all").onchange = (e) => {
    document.querySelectorAll("#n-scan-body .scan-ck")
      .forEach(ck => ck.checked = e.target.checked);
  };
  // Dashboard
  $("#dash-close").onclick = closeDashModal;
  $("#dash-img-load").onclick = loadDashImage;
  $("#dash-reboot").onclick = rebootNvr;
  $("#nvr-modal").onclick = (e) => { if (e.target.id === "nvr-modal") closeModal(); };
  $("#mode").onchange = updateModeUI;
  $("#btn-play").onclick = doPlay;
  $("#btn-stop").onclick = () => { teardownPlayer(); $("#stream-status").textContent = "stopped"; };
  $("#btn-clip").onclick = doClip;
  $("#btn-refresh-clips").onclick = refreshClips;
  setInterval(refreshClips, 5000);
  window.addEventListener("beforeunload", teardownPlayer);
});
