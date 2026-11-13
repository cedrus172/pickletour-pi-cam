const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => el.querySelectorAll(s);

const State = {
  screen: "home",
  channels: [],
  curCh: null,
  curSession: null,
  hls: null,
  quickMin: 5,
  customStart: "",
  customEnd: "",
  mode: "quick",
  retention: { ttl_secs: 10800, ttl_human: "3 giờ" },
};

const HIST_KEY = "pickclip_history_v1";
function histLoad() {
  try { return JSON.parse(localStorage.getItem(HIST_KEY) || "[]"); }
  catch { return []; }
}
function histSave(arr) {
  try { localStorage.setItem(HIST_KEY, JSON.stringify(arr.slice(0, 200))); } catch {}
}
function histPush(entry) {
  const arr = histLoad();
  arr.unshift(entry);
  histSave(arr);
}
function histClear() { localStorage.removeItem(HIST_KEY); }

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {"Content-Type":"application/json"},
    ...opts,
  });
  const txt = await r.text();
  if (!r.ok) {
    let msg = txt;
    try { msg = JSON.parse(txt).error || txt; } catch {}
    throw new Error(msg || r.statusText);
  }
  if (!txt) return {};
  try { return JSON.parse(txt); } catch { return {}; }
}

function fmtBytes(n) {
  if (!n) return "—";
  if (n < 1024*1024) return (n/1024).toFixed(0) + " KB";
  if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + " MB";
  return (n/1024/1024/1024).toFixed(2) + " GB";
}

function pad(n) { return String(n).padStart(2, "0"); }
function nowLocalStr() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function minusMin(d, m) { return new Date(d.getTime() - m*60000); }
function toLocalStr(d) {
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function toDahuaDt(local) {
  // "2026-10-26T10:30:00" or iOS "2026-10-26T10:30" → "2026-10-26 10:30:00"
  if (!local) return "";
  let s = local.replace("T", " ").trim();
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})(?::(\d{2}))?$/.exec(s);
  if (!m) return s;
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6] || "00"}`;
}

function fromLocalStr(s) {
  // "YYYY-MM-DDTHH:MM:SS" → Date (local)
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(s);
  if (!m) return null;
  return new Date(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +(m[6]||0));
}

const TABBAR_SCREENS = new Set(["home", "videos", "history", "settings"]);
function navigate(screen) {
  State.screen = screen;
  $$(".screen").forEach(el => {
    el.style.display = el.dataset.screen === screen ? "" : "none";
  });
  document.body.classList.toggle("tabbar-visible", TABBAR_SCREENS.has(screen));
  $$("#main-tabs .tab").forEach(t => t.classList.toggle("active", t.dataset.nav === screen));
  window.scrollTo(0, 0);

  if (screen === "videos") renderVideos();
  if (screen === "history") renderHistory();
  if (screen === "settings") renderSettings();
}

// ------------------- HOME -------------------
async function loadHome() {
  const grid = $("#cam-grid");
  grid.innerHTML = `<div class="cam-empty">Đang tải camera...</div>`;
  try {
    State.channels = await api("/api/public/channels" +
      (window.VENUE_SLUG ? "?venue=" + encodeURIComponent(window.VENUE_SLUG) : ""));
  } catch (e) {
    grid.innerHTML = `<div class="cam-empty">Lỗi: ${e.message}</div>`;
    return;
  }
  if (!State.channels.length) {
    grid.innerHTML = `<div class="cam-empty">Chưa có sân nào được cấu hình.<br>Liên hệ quản trị để thêm.</div>`;
    return;
  }
  grid.innerHTML = State.channels.map(c => `
    <div class="cam-card" data-cid="${c.id}">
      <img class="thumb" src="/api/public/channels/${c.id}/thumbnail" alt="${c.name}" loading="lazy">
      <div class="label">
        <div class="cam-name">${c.name}</div>
        <div class="cam-status"><span class="live-dot"></span> Trực tiếp</div>
      </div>
    </div>
  `).join("");
  $$(".cam-card").forEach(card => {
    card.onclick = () => openCamera(parseInt(card.dataset.cid));
  });
}

// ------------------- CAMERA -------------------
async function openCamera(cid) {
  const ch = State.channels.find(c => c.id === cid);
  if (!ch) return;
  State.curCh = ch;
  State.quickMin = 5;
  $("#cam-title").textContent = ch.name;
  $("#quick-label").textContent = `Cắt highlight (${State.quickMin} phút)`;
  $$('[data-min]').forEach(b => b.classList.toggle("active", +b.dataset.min === 5));
  navigate("cam");
  await startLive();
  updateCamTime();
  if (window.__timer) clearInterval(window.__timer);
  window.__timer = setInterval(updateCamTime, 1000);
}
function updateCamTime() {
  const d = new Date();
  $("#cam-time").textContent = toLocalStr(d).replace("T", " ");
}

async function startLive() {
  teardownPlayer();
  if (!State.curCh) return;
  try {
    const r = await api("/api/public/stream/start", {
      method: "POST",
      body: JSON.stringify({ channel_id: State.curCh.id, mode: "live" }),
    });
    State.curSession = r.session_id;
    const v = $("#cam-video");
    if (Hls.isSupported()) {
      State.hls = new Hls({ liveSyncDuration: 2, lowLatencyMode: true });
      State.hls.loadSource(r.manifest);
      State.hls.attachMedia(v);
      State.hls.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(()=>{}));
    } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = r.manifest; v.play().catch(()=>{});
    }
  } catch (e) {
    console.error("live fail", e);
  }
}

function teardownPlayer() {
  if (State.hls) { try { State.hls.destroy(); } catch{} State.hls = null; }
  const v = $("#cam-video");
  if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
  if (State.curSession) {
    api("/api/public/stream/stop", {
      method:"POST", body:JSON.stringify({session_id: State.curSession}),
    }).catch(()=>{});
    State.curSession = null;
  }
  if (window.__timer) { clearInterval(window.__timer); window.__timer = null; }
}

// ------------------- CUSTOM -------------------
function openCustom() {
  const now = new Date();
  const start = minusMin(now, 5);
  $("#dt-start").value = toLocalStr(start).slice(0,19);
  $("#dt-end").value = toLocalStr(now).slice(0,19);
  State.mode = "custom";
  navigate("custom");
  $$('[data-preset]').forEach(b => b.classList.remove("active"));
}

function applyPreset(min) {
  const now = new Date();
  const start = minusMin(now, min);
  $("#dt-start").value = toLocalStr(start).slice(0,19);
  $("#dt-end").value = toLocalStr(now).slice(0,19);
  $$('[data-preset]').forEach(b => b.classList.toggle("active", +b.dataset.preset === min));
}

// ------------------- PREVIEW -------------------
async function goPreview(startStr, endStr) {
  const s = fromLocalStr(startStr);
  const e = fromLocalStr(endStr);
  if (!s || !e) { alert("Nhập thời gian hợp lệ"); return; }
  const secs = (e - s) / 1000;
  if (secs <= 0) { alert("Thời gian kết thúc phải sau thời gian bắt đầu"); return; }
  if (secs > 30*60) { alert("Tối đa 30 phút"); return; }

  State.customStart = startStr;
  State.customEnd = endStr;
  navigate("preview");

  $("#prev-range").textContent = `${startStr.replace("T", " ")} → ${endStr.replace("T", " ").slice(11)}`;
  $("#info-cam").textContent = State.curCh.name;
  $("#info-time").textContent = startStr.replace("T", " ") + " · " + endStr.replace("T", " ").slice(11);
  const mins = Math.round(secs / 60);
  const extra = Math.round(secs % 60);
  $("#info-dur").textContent = (mins ? `${mins} phút ` : "") + (extra ? `${extra}s` : "") || `${secs}s`;
  const stamp = `${State.curCh.name.replace(/\s+/g, "_")}_${startStr.replace(/[-:]/g,"").replace("T","_")}`;
  $("#info-fname").value = stamp;

  // Start HLS playback preview of the range (so user sees before clipping)
  teardownPrev();
  try {
    const r = await api("/api/public/stream/start", {
      method:"POST",
      body: JSON.stringify({
        channel_id: State.curCh.id,
        mode: "playback",
        start: startStr.replace("T", " "),
        end: endStr.replace("T", " "),
      }),
    });
    State.previewSession = r.session_id;
    const v = $("#prev-video");
    if (Hls.isSupported()) {
      State.previewHls = new Hls({ liveSyncDuration: 2 });
      State.previewHls.loadSource(r.manifest);
      State.previewHls.attachMedia(v);
      State.previewHls.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(()=>{}));
    } else if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = r.manifest; v.play().catch(()=>{});
    }
  } catch (e) {
    console.error("preview fail", e);
  }
}

function teardownPrev() {
  if (State.previewHls) { try { State.previewHls.destroy(); } catch{} State.previewHls = null; }
  const v = $("#prev-video");
  if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
  if (State.previewSession) {
    api("/api/public/stream/stop", {
      method:"POST", body:JSON.stringify({session_id: State.previewSession}),
    }).catch(()=>{});
    State.previewSession = null;
  }
}

// ------------------- EXPORT -------------------
async function startExport() {
  teardownPrev();
  navigate("exporting");
  // Reset progress UI
  const pctEl = $("#prog-pct");  if (pctEl) pctEl.textContent = "0%";
  const fillEl = $("#prog-fill"); if (fillEl) fillEl.style.width = "0%";
  const metaEl = $("#prog-meta"); if (metaEl) metaEl.textContent = "Đang khởi động...";
  const hintEl = $("#rtsp-hint"); if (hintEl) hintEl.style.display = "none";
  try {
    let payload = { channel_id: State.curCh.id };
    if (State.mode === "quick") {
      payload.quick_min = State.quickMin;
    } else {
      payload.start = toDahuaDt(State.customStart);
      payload.end = toDahuaDt(State.customEnd);
    }
    const r = await api("/api/public/clip", { method:"POST", body: JSON.stringify(payload) });
    pollClip(r.clip_id);
  } catch (e) {
    showError(e.message);
  }
}

async function pollClip(clipId) {
  const start = Date.now();
  const tick = async () => {
    if (Date.now() - start > 20 * 60000) {
      showError("Timeout — ffmpeg quá lâu");
      return;
    }
    try {
      const s = await api(`/api/public/clips/${clipId}`);
      if (s.status === "running") {
        const phase = s.phase || "";
        const pctEl = $("#prog-pct");
        const fillEl = $("#prog-fill");
        const metaEl = $("#prog-meta");
        const hintEl = $("#rtsp-hint");
        if (hintEl) hintEl.style.display = phase === "rtsp_fallback" ? "block" : "none";
        let pct = 0, metaText = "chờ...";

        if (phase === "fast_download") {
          // Fast: download bytes. Estimate %: expected_sec * ~1Mbps / 8 ≈ bytes
          const bytes = s.downloaded || 0;
          const expEst = (s.expected_sec || 0) * 1024 * 128;  // ~1 Mbps estimate
          pct = expEst > 0 ? Math.min(99, (bytes / expEst) * 100) : 0;
          metaText = `⚡ Fast · ${fmtBytes(bytes)}`;
        } else if (phase === "rtsp_fallback" || !phase || phase === "init") {
          const prog = s.progress || {};
          const cur = prog.out_time_sec || 0;
          const exp = s.expected_sec || 0;
          pct = exp > 0 ? Math.min(100, (cur / exp) * 100) : 0;
          const curStr = cur ? `${cur.toFixed(1)}s` : "chờ...";
          const expStr = exp ? ` / ${exp}s` : "";
          const spd = prog.speed ? ` · ${prog.speed}x` : "";
          const sz = prog.size ? ` · ${fmtBytes(prog.size)}` : "";
          const prefix = phase === "rtsp_fallback" ? "RTSP (fallback) · " : "";
          metaText = `${prefix}${curStr}${expStr}${spd}${sz}`;
        }

        if (pctEl) pctEl.textContent = pct.toFixed(0) + "%";
        if (fillEl) fillEl.style.width = pct + "%";
        if (metaEl) metaEl.textContent = metaText;
      }
      if (s.status === "done") {
        $("#succ-dur").textContent = "✓ Đã xuất";
        $("#succ-size").textContent = fmtBytes(s.size);
        $("#btn-download").href = s.download_url;
        $("#btn-download").setAttribute("download", s.name);
        const v = $("#succ-video");
        v.src = s.download_url;
        v.load();
        const note = document.getElementById("retention-note");
        if (note) note.textContent = `⏱ Clip sẽ tự xoá sau ${State.retention.ttl_human} — tải về máy ngay nhé.`;
        // Push to local history
        histPush({
          id: clipId,
          name: s.name,
          size: s.size,
          created: Date.now(),
          cam_name: State.curCh ? State.curCh.name : "",
          start: State.customStart,
          end: State.customEnd,
          mode: State.mode,
          download_url: s.download_url,
        });
        navigate("success");
        return;
      }
      if (s.status === "error") {
        showError(s.err || "ffmpeg lỗi không rõ");
        return;
      }
      setTimeout(tick, 1500);
    } catch (e) {
      showError(e.message);
    }
  };
  tick();
}

function showError(msg) {
  $("#err-msg").textContent = msg;
  navigate("error");
}

// ------------------- SHARE -------------------
async function share(kind) {
  const url = location.origin + $("#btn-download").getAttribute("href");
  const title = "PickClip highlight";
  if (kind === "copy") {
    try { await navigator.clipboard.writeText(url); alert("Đã copy link"); }
    catch { prompt("Copy link:", url); }
    return;
  }
  if (navigator.share) {
    try { await navigator.share({ title, url }); return; } catch {}
  }
  const map = {
    zalo: `https://zalo.me/share?url=${encodeURIComponent(url)}`,
    fb: `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(url)}`,
    ig: "https://instagram.com",
    tt: "https://www.tiktok.com/upload",
  };
  if (map[kind]) window.open(map[kind], "_blank");
}

// ------------------- VIDEOS / HISTORY / SETTINGS -------------------
function fmtAgo(ts) {
  const d = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (d < 60) return `${d}s trước`;
  if (d < 3600) return `${Math.floor(d/60)} phút trước`;
  if (d < 86400) return `${Math.floor(d/3600)}h trước`;
  return `${Math.floor(d/86400)} ngày trước`;
}

async function checkAlive(entry) {
  // Expired by TTL?
  if ((Date.now() - entry.created) / 1000 > State.retention.ttl_secs) {
    return "expired";
  }
  try {
    const r = await fetch(entry.download_url, { method: "HEAD" });
    return r.ok ? "alive" : "gone";
  } catch { return "gone"; }
}

async function renderVideos() {
  const grid = $("#videos-grid");
  $("#ret-h").textContent = State.retention.ttl_human;
  const hist = histLoad();
  if (!hist.length) {
    grid.innerHTML = `<div class="cam-empty">Chưa có video nào.<br>Vào Trang chủ → chọn sân → cắt highlight.</div>`;
    return;
  }
  grid.innerHTML = hist.map(h => `
    <div class="cam-card" data-vid="${h.id}">
      <div style="aspect-ratio:16/10;background:#0a1414;position:relative">
        <video src="${h.download_url}#t=1" preload="metadata" style="width:100%;height:100%;object-fit:cover" muted playsinline></video>
        <div class="vid-overlay"><div class="play-ic">▶</div></div>
      </div>
      <div class="label">
        <div class="cam-name">${h.cam_name || "—"}</div>
        <div class="cam-status" style="color:var(--muted)">${fmtBytes(h.size)} · ${fmtAgo(h.created)}</div>
      </div>
    </div>
  `).join("");
  // Check each entry's existence; mark dead ones
  for (const h of hist) {
    const card = grid.querySelector(`[data-vid="${h.id}"]`);
    if (!card) continue;
    const state = await checkAlive(h);
    if (state !== "alive") {
      card.style.opacity = "0.4";
      card.style.pointerEvents = "none";
      const status = card.querySelector(".cam-status");
      if (status) status.innerHTML = state === "expired" ? "⏱ Đã hết hạn" : "✗ Không tìm thấy";
    } else {
      card.onclick = () => {
        window.open(h.download_url, "_blank");
      };
    }
  }
}

async function renderHistory() {
  const list = $("#history-list");
  const hist = histLoad();
  if (!hist.length) {
    list.innerHTML = `<div class="cam-empty">Chưa có lịch sử.</div>`;
    return;
  }
  list.innerHTML = hist.map(h => {
    const dur = h.start && h.end ? ((new Date(h.end).getTime() - new Date(h.start).getTime()) / 60000).toFixed(0) + " phút" : "—";
    return `
      <div class="hist-item" data-hist="${h.id}">
        <div class="hist-ic">▶</div>
        <div class="hist-body">
          <div class="hist-name">${h.cam_name || "Clip"} · ${dur}</div>
          <div class="hist-meta">${new Date(h.created).toLocaleString()} · ${fmtBytes(h.size)}</div>
        </div>
        <button class="hist-action" data-dl="${h.id}" data-url="${h.download_url}">⬇</button>
      </div>
    `;
  }).join("");
  // Check alive
  for (const h of hist) {
    const row = list.querySelector(`[data-hist="${h.id}"]`);
    if (!row) continue;
    const state = await checkAlive(h);
    const ic = row.querySelector(".hist-ic");
    const act = row.querySelector(".hist-action");
    if (state === "expired") {
      ic.classList.add("gone"); ic.textContent = "⏱";
      act.classList.add("disabled"); act.textContent = "–";
      row.querySelector(".hist-meta").innerHTML += " · <span style='color:var(--danger)'>đã hết hạn</span>";
      act.onclick = null;
    } else if (state === "gone") {
      ic.classList.add("gone"); ic.textContent = "✗";
      act.classList.add("disabled"); act.textContent = "–";
      act.onclick = null;
    } else {
      act.onclick = () => window.open(h.download_url, "_blank");
    }
  }
}

async function renderSettings() {
  $("#set-cam-count").textContent = State.channels.length;
  $("#set-ret").textContent = State.retention.ttl_human;
  $("#set-hist").textContent = `${histLoad().length} clip`;
}

// ------------------- WIRING -------------------
document.addEventListener("DOMContentLoaded", async () => {
  // Branding
  try {
    const b = await api("/api/public/branding");
    if (b.title) $("#brand-title").textContent = b.title;
    if (b.tagline) $("#brand-tagline").textContent = b.tagline;
    if (b.footer) {
      const l = $("#brand-footer-link");
      l.textContent = b.footer;
      if (b.footer_url) l.href = b.footer_url;
    }
    document.title = b.title ? `${b.title} — Cắt highlight` : "PickClip";
  } catch {}
  // Retention
  try { State.retention = await api("/api/public/retention"); } catch {}

  await loadHome();
  document.body.classList.add("tabbar-visible");

  $("#btn-choose-cam").onclick = () => {
    document.getElementById("cam-grid").scrollIntoView({behavior:"smooth"});
  };

  // CAM screen quick chips
  $$('[data-min]').forEach(b => {
    b.onclick = () => {
      State.quickMin = +b.dataset.min;
      State.mode = "quick";
      $$('[data-min]').forEach(x => x.classList.toggle("active", x === b));
      $("#quick-label").textContent = `Cắt highlight (${State.quickMin} phút)`;
    };
  });

  $("#btn-quick-cut").onclick = () => {
    State.mode = "quick";
    const now = new Date();
    const end = minusMin(now, 0);
    const start = minusMin(end, State.quickMin);
    // Jump directly to export (quick = trust the N minute window)
    State.customStart = toLocalStr(start).slice(0,19);
    State.customEnd = toLocalStr(end).slice(0,19);
    // Still show preview for consistency
    goPreview(State.customStart, State.customEnd);
  };

  // CUSTOM screen
  $$('[data-preset]').forEach(b => {
    b.onclick = () => applyPreset(+b.dataset.preset);
  });
  $("#btn-custom-preview").onclick = () => {
    State.mode = "custom";
    goPreview($("#dt-start").value, $("#dt-end").value);
  };

  // Export
  $("#btn-export").onclick = startExport;

  // Nav buttons (data-nav) — includes tab bar + inline back buttons
  $$('[data-nav]').forEach(b => {
    b.onclick = () => {
      const target = b.dataset.nav;
      if (target === "home") { teardownPlayer(); teardownPrev(); navigate("home"); return; }
      if (target === "cam") { teardownPrev(); navigate("cam"); if (!State.hls) startLive(); return; }
      if (target === "custom") { openCustom(); return; }
      if (target === "videos") { teardownPlayer(); teardownPrev(); navigate("videos"); return; }
      if (target === "history") { teardownPlayer(); teardownPrev(); navigate("history"); return; }
      if (target === "settings") { teardownPlayer(); teardownPrev(); navigate("settings"); return; }
    };
  });

  // Videos / History specific buttons
  $("#btn-refresh-videos").onclick = () => renderVideos();
  $("#btn-clear-history").onclick = () => {
    if (confirm("Xoá toàn bộ lịch sử trên máy này?")) {
      histClear(); renderHistory();
    }
  };
  $("#set-clear").onclick = () => {
    if (confirm("Xoá lịch sử?")) { histClear(); renderSettings(); }
  };

  // Share
  $$('[data-share]').forEach(b => b.onclick = () => share(b.dataset.share));

  // Cleanup on leave
  window.addEventListener("pagehide", () => { teardownPlayer(); teardownPrev(); });
});
