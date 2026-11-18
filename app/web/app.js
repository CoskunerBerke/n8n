const state = {
  streamerFilter: "all",
  clipStatus: "pending_review",
  dashboard: null,
  streamers: [],
  clips: [],
};

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const contentType = res.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    throw new Error(typeof body === "object" && body.detail ? body.detail : String(body));
  }
  return body;
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderDashboard(data) {
  state.dashboard = data;
  document.getElementById("mobile-url-text").textContent = data.network.mobile_url;

  const cardsEl = document.getElementById("pipeline-cards");
  cardsEl.innerHTML = data.pipelines
    .map((p) => {
      const st = data.pipeline_stats[p.id] || {};
      const flag = p.id === "tr" ? "🇹🇷 TÜRKÇE HATTI" : "🌍 GLOBAL HATTI";
      const ytOk = p.id === "tr" ? data.accounts.youtube_tr : data.accounts.youtube_global;
      const igOk = p.id === "tr" ? data.accounts.instagram_tr : data.accounts.instagram_global;

      return `
      <article class="pipeline-card">
        <div class="pipeline-top">
          <div>
            <span class="meta-label">${flag}</span>
            <h2>${escapeHtml(p.name)}</h2>
          </div>
          <button class="btn btn-outline btn-sm" onclick="triggerScan('${p.id}')">Şimdi Tara</button>
        </div>

        <div class="stat-strip">
          <div class="stat-cell"><span class="stat-val">${st.live_now || 0}</span><span class="stat-key">Canlı</span></div>
          <div class="stat-cell"><span class="stat-val">${st.pending_review || 0}</span><span class="stat-key">Bekleyen</span></div>
          <div class="stat-cell"><span class="stat-val">${st.published || 0}</span><span class="stat-key">Yayınlanan</span></div>
          <div class="stat-cell"><span class="stat-val">${st.rejected || 0}</span><span class="stat-key">Elenen</span></div>
        </div>

        <div class="meta-label">
          YouTube: ${ytOk ? "Bağlı" : "Bağlı Değil"} · Instagram: ${igOk ? "Bağlı" : "Bağlı Değil"}
        </div>

        <div class="toggle-row">
          <label class="checkbox-row">
            <input type="checkbox" ${p.autopilot ? "checked" : ""} onchange="togglePipeline('${p.id}', 'autopilot', this.checked)" />
            <span>Otopilot (Onaysız Direkt Yayın)</span>
          </label>
          <label class="checkbox-row">
            <input type="checkbox" ${p.live_enabled ? "checked" : ""} onchange="togglePipeline('${p.id}', 'live_enabled', this.checked)" />
            <span>Canlı Yayın Yakala</span>
          </label>
          <label class="checkbox-row">
            <input type="checkbox" ${p.clips_enabled ? "checked" : ""} onchange="togglePipeline('${p.id}', 'clips_enabled', this.checked)" />
            <span>Trend Klipleri Tara</span>
          </label>
        </div>
      </article>`;
    })
    .join("");

  const liveEl = document.getElementById("live-sessions-list");
  document.getElementById("live-count-label").textContent = `${data.live_sessions.length} aktif`;
  liveEl.innerHTML = data.live_sessions.length
    ? data.live_sessions
        .map(
          (s) =>
            `<div class="log-item"><span class="status-dot live"></span><strong>${escapeHtml(s.display_name)}</strong> (${s.platform.toUpperCase()} · ${s.pipeline_id.toUpperCase()}) — ${escapeHtml(s.title || "Canlı")}</div>`
        )
        .join("")
    : `<div class="meta-label">Şu an aktif canlı yayın yakalayıcısı beklemede. Yayına giren kanallar otomatik bağlanır.</div>`;

  const logsEl = document.getElementById("activity-logs");
  logsEl.innerHTML = data.recent_logs.length
    ? data.recent_logs
        .map(
          (l) =>
            `<div class="log-item"><strong>[${escapeHtml(l.pipeline_id || "SYS").toUpperCase()}]</strong> ${escapeHtml(l.message)}</div>`
        )
        .join("")
    : `<div class="meta-label">Henüz kayıt yok.</div>`;
}

function renderStreamers() {
  const grid = document.getElementById("streamers-grid");
  const filtered = state.streamers.filter(
    (s) => state.streamerFilter === "all" || s.pipeline_id === state.streamerFilter
  );
  grid.innerHTML = filtered
    .map(
      (s) => `
    <article class="streamer-card">
      <div class="streamer-head">
        <div>
          <div class="meta-label">${s.pipeline_id === "tr" ? "🇹🇷 TÜRKÇE" : "🌍 GLOBAL"} · ${s.platform.toUpperCase()}</div>
          <strong><span class="status-dot ${s.is_live ? "live" : ""}"></span>${escapeHtml(s.display_name)}</strong>
        </div>
        <button class="btn btn-danger btn-sm" onclick="removeStreamer(${s.id})">Sil</button>
      </div>
      <div class="meta-label">${s.is_live ? "CANLI: " + escapeHtml(s.last_live_title || "Yayında") : "Çevrimdışı / Beklemede"}</div>
      <div class="toggle-row">
        <label class="checkbox-row">
          <input type="checkbox" ${s.enabled ? "checked" : ""} onchange="toggleStreamer(${s.id}, 'enabled', this.checked)" />
          <span>Aktif</span>
        </label>
        <label class="checkbox-row">
          <input type="checkbox" ${s.live_watch ? "checked" : ""} onchange="toggleStreamer(${s.id}, 'live_watch', this.checked)" />
          <span>Canlı</span>
        </label>
        <label class="checkbox-row">
          <input type="checkbox" ${s.clip_scan ? "checked" : ""} onchange="toggleStreamer(${s.id}, 'clip_scan', this.checked)" />
          <span>Klipler</span>
        </label>
      </div>
    </article>`
    )
    .join("");
}

function renderClips() {
  const grid = document.getElementById("clips-grid");
  if (!state.clips.length) {
    grid.innerHTML = `<div class="panel meta-label">Bu kategoride klip bulunmuyor.</div>`;
    return;
  }

  grid.innerHTML = state.clips
    .map((c) => {
      const mediaHtml = c.rendered_url
        ? `<video src="${escapeHtml(c.rendered_url)}" controls playsinline preload="metadata"></video>`
        : c.contact_sheet_url
        ? `<img src="${escapeHtml(c.contact_sheet_url)}" alt="Önizleme" />`
        : `<div class="meta-label">Görsel yok</div>`;

      const flag = c.pipeline_id === "tr" ? "🇹🇷 TR" : "🌍 GLOBAL";
      const reasonBlock =
        c.status === "rejected"
          ? `<div class="meta-label" style="color:#fca5a5;">Eleme Nedeni: ${escapeHtml(c.reject_reason)}</div>`
          : `<div class="meta-label">${escapeHtml(c.quality_reason)}</div>`;

      const editableFields =
        c.status !== "rejected"
          ? `
        <label class="field">
          <span>YouTube Başlık</span>
          <input id="yt-title-${c.id}" class="input" value="${escapeHtml(c.youtube_title)}" />
        </label>
        <label class="field">
          <span>Instagram Açıklama</span>
          <textarea id="ig-cap-${c.id}" class="input" rows="3">${escapeHtml(c.instagram_caption)}</textarea>
        </label>`
          : "";

      const actions =
        c.status === "pending_review"
          ? `
        <div class="clip-actions">
          <button class="btn btn-primary btn-sm" onclick="publishClip('${c.id}', ['youtube','instagram'])">YT + IG Paylaş</button>
          <button class="btn btn-outline btn-sm" onclick="publishClip('${c.id}', ['youtube'])">Sadece YT</button>
          <button class="btn btn-outline btn-sm" onclick="publishClip('${c.id}', ['instagram'])">Sadece IG</button>
          <button class="btn btn-ghost btn-sm" onclick="regenClip('${c.id}')">Başlık Yenile</button>
          <button class="btn btn-danger btn-sm" onclick="rejectClip('${c.id}')">Reddet</button>
        </div>`
          : c.status === "published"
          ? `<div class="meta-label" style="color:#86efac;">Yayınlandı · YT: ${escapeHtml(c.youtube_video_id || "-")} · IG: ${escapeHtml(c.instagram_media_id || "-")}</div>`
          : "";

      return `
      <article class="clip-card">
        <div class="clip-media">${mediaHtml}</div>
        <div class="clip-body">
          <div class="clip-meta-line">
            <span>${flag} · ${escapeHtml(c.streamer_display_name)} (${c.source_platform.toUpperCase()})</span>
            <span>Puan: ${c.viral_score}/10 · ${Math.round(c.duration_sec)}sn</span>
          </div>
          ${reasonBlock}
          ${editableFields}
          ${actions}
        </div>
      </article>`;
    })
    .join("");
}

async function loadSettingsForm() {
  const s = await api("/api/settings");
  const form = document.getElementById("settings-form");
  for (const el of form.elements) {
    if (!el.name || !(el.name in s)) continue;
    if (el.type === "checkbox") {
      el.checked = Boolean(s[el.name]);
    } else {
      el.value = s[el.name] || "";
    }
  }
  document.getElementById("yt-tr-status").textContent = s.youtube_tr_connected
    ? "Bağlı (Yüklemeye hazır)"
    : "Bağlı değil";
  document.getElementById("yt-global-status").textContent = s.youtube_global_connected
    ? "Bağlı (Yüklemeye hazır)"
    : "Bağlı değil";
  document.getElementById("yt-secret-status").textContent = s.youtube_client_secrets_exists
    ? "credentials/youtube_client_secret.json hazır"
    : "Henüz yüklenmedi";
}

async function refreshAll() {
  try {
    const [dash, streamers, clips] = await Promise.all([
      api("/api/dashboard"),
      api("/api/streamers"),
      api(`/api/clips?status=${encodeURIComponent(state.clipStatus)}`),
    ]);
    renderDashboard(dash);
    state.streamers = streamers;
    renderStreamers();
    state.clips = clips;
    renderClips();
  } catch (err) {
    showToast("Veri yükleme hatası: " + err.message);
  }
}

window.togglePipeline = async (id, field, val) => {
  await api(`/api/pipelines/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [field]: val }),
  });
  showToast(`${id.toUpperCase()} ayarı güncellendi.`);
  refreshAll();
};

window.triggerScan = async (id) => {
  showToast(`${id.toUpperCase()} taraması başlatıldı...`);
  try {
    const res = await api(`/api/pipelines/${id}/run-cycle`, { method: "POST" });
    showToast(`${id.toUpperCase()} taraması tamamlandı: ${res.approved} onaylandı, ${res.rejected} elendi.`);
    refreshAll();
  } catch (err) {
    showToast("Tarama hatası: " + err.message);
  }
};

window.toggleStreamer = async (id, field, val) => {
  await api(`/api/streamers/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [field]: val }),
  });
  refreshAll();
};

window.removeStreamer = async (id) => {
  await api(`/api/streamers/${id}`, { method: "DELETE" });
  showToast("Yayıncı kaldırıldı.");
  refreshAll();
};

window.publishClip = async (clipId, targets) => {
  const titleEl = document.getElementById(`yt-title-${clipId}`);
  const capEl = document.getElementById(`ig-cap-${clipId}`);
  showToast("Yayınlanıyor...");
  try {
    const res = await api(`/api/clips/${clipId}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        targets,
        youtube_title: titleEl ? titleEl.value : undefined,
        instagram_caption: capEl ? capEl.value : undefined,
      }),
    });
    showToast("Yayınlandı: " + (res.summary || "Tamam"));
    refreshAll();
  } catch (err) {
    showToast("Yayınlama hatası: " + err.message);
  }
};

window.rejectClip = async (clipId) => {
  await api(`/api/clips/${clipId}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: "Panelden manuel reddedildi" }),
  });
  showToast("Klip reddedildi.");
  refreshAll();
};

window.regenClip = async (clipId) => {
  showToast("Başlık ve açıklama yenileniyor...");
  await api(`/api/clips/${clipId}/regenerate-metadata`, { method: "POST" });
  showToast("Başlıklar güncellendi.");
  refreshAll();
};

document.getElementById("btn-scan-tr").addEventListener("click", () => triggerScan("tr"));
document.getElementById("btn-scan-global").addEventListener("click", () => triggerScan("global"));
document.getElementById("btn-refresh-all").addEventListener("click", refreshAll);

document.getElementById("manual-url-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pipeline_id = document.getElementById("manual-pipeline").value;
  const url = document.getElementById("manual-url").value.trim();
  const startRaw = document.getElementById("manual-start").value;
  const endRaw = document.getElementById("manual-end").value;
  showToast("Video indiriliyor, kalite kontrolünden geçiyor ve 9:16 kesiliyor...");
  try {
    const res = await api("/api/clips/submit-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pipeline_id,
        url,
        start_sec: startRaw ? Number(startRaw) : null,
        end_sec: endRaw ? Number(endRaw) : null,
      }),
    });
    if (res.status === "rejected") {
      showToast("Klip otomatik elendi: " + res.reject_reason);
    } else {
      showToast("Klip hazır! Kuyruğa eklendi.");
      document.getElementById("manual-url").value = "";
    }
    refreshAll();
  } catch (err) {
    showToast("Hata: " + err.message);
  }
});

document.getElementById("add-streamer-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/streamers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pipeline_id: document.getElementById("new-streamer-pipeline").value,
      platform: document.getElementById("new-streamer-platform").value,
      username: document.getElementById("new-streamer-username").value.trim(),
      display_name: document.getElementById("new-streamer-display").value.trim() || null,
    }),
  });
  document.getElementById("new-streamer-username").value = "";
  document.getElementById("new-streamer-display").value = "";
  showToast("Yayıncı listeye eklendi.");
  refreshAll();
});

document.querySelectorAll("[data-streamer-filter]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-streamer-filter]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.streamerFilter = btn.dataset.streamerFilter;
    renderStreamers();
  });
});

document.querySelectorAll("[data-clip-status]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    document.querySelectorAll("[data-clip-status]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.clipStatus = btn.dataset.clipStatus;
    state.clips = await api(`/api/clips?status=${encodeURIComponent(state.clipStatus)}`);
    renderClips();
  });
});

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    payload[el.name] = el.type === "checkbox" ? el.checked : el.value.trim();
  }
  await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  showToast("Ayarlar kaydedildi ve anında aktif edildi.");
  await loadSettingsForm();
  await refreshAll();
});

document.getElementById("yt-secret-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  await api("/api/settings/upload-youtube-secret", { method: "POST", body: fd });
  showToast("YouTube OAuth JSON yüklendi.");
  loadSettingsForm();
});

async function connectYouTube(pid) {
  try {
    const res = await api(`/api/auth/youtube/${pid}/start`);
    window.open(res.auth_url, "_blank", "noopener");
  } catch (err) {
    showToast(err.message);
  }
}

document.getElementById("btn-connect-yt-tr").addEventListener("click", () => connectYouTube("tr"));
document.getElementById("btn-connect-yt-global").addEventListener("click", () => connectYouTube("global"));

refreshAll();
loadSettingsForm();
setInterval(refreshAll, 20000);
