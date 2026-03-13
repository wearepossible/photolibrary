/* Possible Photo Library — app.js */

(function () {
  "use strict";

  // Config — set R2_PUBLIC_URL after deployment
  const DATA_URL = "https://pub-b79e8a0629114570b848bf966ccd417c.r2.dev";
  const AUTH_KEY = "photo-library-auth";
  const BATCH_SIZE = 60;
  // SHA-256 hash of the password "showmethepics"
  const PASSWORD_HASH = "1bfaf1250f4b09e72d411aaa63675e51cc5e2221cd86290bd8d4c5b64c94997d";

  let allPhotos = [];
  let filteredPhotos = [];
  let renderedCount = 0;
  let currentPhoto = null;
  let editingField = null;

  // --- Authentication ---

  async function sha256(str) {
    const buf = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(str)
    );
    return Array.from(new Uint8Array(buf))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }

  function checkAuth() {
    return sessionStorage.getItem(AUTH_KEY) === "true";
  }

  function showApp() {
    document.getElementById("auth-gate").classList.add("hidden");
    document.body.classList.remove("locked");
    loadData();
  }

  document.getElementById("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const pw = document.getElementById("auth-password").value;
    const hash = await sha256(pw);

    if (hash === PASSWORD_HASH) {
      sessionStorage.setItem(AUTH_KEY, "true");
      sessionStorage.setItem("pw", pw);
      showApp();
    } else {
      // Try server-side verification as fallback (for local dev or changed password)
      try {
        const res = await fetch("/.netlify/functions/verify-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: pw }),
        });

        if (res.ok) {
          sessionStorage.setItem(AUTH_KEY, "true");
          sessionStorage.setItem("pw", pw);
          showApp();
        } else if (res.status === 401) {
          document.getElementById("auth-error").hidden = false;
        } else {
          // Server unavailable (local dev) — accept any non-empty password
          if (pw) {
            sessionStorage.setItem(AUTH_KEY, "true");
            sessionStorage.setItem("pw", pw);
            showApp();
          }
        }
      } catch {
        // Network error — accept any non-empty password for local dev
        if (pw) {
          sessionStorage.setItem(AUTH_KEY, "true");
          sessionStorage.setItem("pw", pw);
          showApp();
        }
      }
    }
  });

  // --- Data Loading ---

  async function loadData() {
    const loading = document.getElementById("loading");
    loading.hidden = false;

    try {
      const dataUrl = DATA_URL ? `${DATA_URL}/data.json` : "/data/data.json";
      const dataRes = await fetch(dataUrl);
      allPhotos = shuffle(await dataRes.json());
      applyFilters();

      // Open photo from URL hash (e.g. #photo-id)
      const hashId = window.location.hash.slice(1);
      if (hashId) {
        const linked = allPhotos.find((p) => p.id === hashId);
        if (linked) openModal(linked);
      }
    } catch (err) {
      loading.textContent = "Failed to load photos. Please try again.";
      console.error("Load error:", err);
    }
  }

  // --- Search with relevancy ranking ---

  function scorePhoto(photo, terms) {
    let score = 0;
    let allMatch = true;

    for (const term of terms) {
      let termFound = false;

      // High-value matches (human-authored metadata)
      const keywords = (photo.keywords || []).map((k) => k.toLowerCase());
      if (keywords.some((k) => k === term)) {
        score += 10;
        termFound = true;
      } else if (keywords.some((k) => k.includes(term))) {
        score += 6;
        termFound = true;
      }

      const campaign = (photo.campaign || "").toLowerCase();
      if (campaign === term) {
        score += 10;
        termFound = true;
      } else if (campaign.includes(term)) {
        score += 6;
        termFound = true;
      }

      const description = (photo.description || "").toLowerCase();
      if (description.includes(term)) {
        score += 5;
        termFound = true;
      }

      const credit = (photo.credit || "").toLowerCase();
      if (credit.includes(term)) {
        score += 5;
        termFound = true;
      }

      const altText = (photo.alt_text || "").toLowerCase();
      if (altText.includes(term)) {
        score += 4;
        termFound = true;
      }

      // Low-value matches (structural metadata)
      const folderPaths = (photo.locations || [])
        .map((l) => (l.folder_path || "").toLowerCase())
        .join(" ");
      if (folderPaths.includes(term)) {
        score += 1;
        termFound = true;
      }

      const filename = (photo.original_filename || "").toLowerCase();
      if (filename.includes(term)) {
        score += 1;
        termFound = true;
      }

      if (!termFound) allMatch = false;
    }

    return allMatch ? score : 0;
  }

  function applyFilters() {
    const query = document.getElementById("search-input").value.toLowerCase().trim();

    if (query) {
      const terms = query.split(/\s+/).filter(Boolean);
      const scored = allPhotos
        .map((photo) => ({ photo, score: scorePhoto(photo, terms) }))
        .filter((s) => s.score > 0);

      scored.sort((a, b) => b.score - a.score);
      filteredPhotos = scored.map((s) => s.photo);
    } else {
      filteredPhotos = allPhotos;
    }

    renderedCount = 0;
    document.getElementById("photo-grid").innerHTML = "";
    document.getElementById("no-results").hidden = filteredPhotos.length > 0;
    document.getElementById("loading").hidden = true;

    updateResultCount();
    renderBatch();
  }

  function updateResultCount() {
    const el = document.getElementById("result-count");
    const total = filteredPhotos.length;
    if (total === allPhotos.length) {
      el.textContent = `${total} photos`;
    } else {
      el.textContent = `${total} of ${allPhotos.length} photos`;
    }
  }

  // Debounced search
  let searchTimeout;
  document.getElementById("search-input").addEventListener("input", () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(applyFilters, 250);
  });

  // --- Rendering ---

  function renderBatch() {
    const grid = document.getElementById("photo-grid");
    const end = Math.min(renderedCount + BATCH_SIZE, filteredPhotos.length);

    for (let i = renderedCount; i < end; i++) {
      const photo = filteredPhotos[i];
      grid.appendChild(createPhotoCard(photo));
    }

    renderedCount = end;
  }

  function createPhotoCard(photo) {
    const card = document.createElement("div");
    card.className = "photo-card";
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", photo.description || photo.original_filename);
    card.addEventListener("click", () => openModal(photo));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openModal(photo);
      }
    });

    const img = document.createElement("img");
    img.src = photo.thumbnail_url || `thumbnails/${photo.id}.jpg`;
    img.alt = photo.alt_text || photo.description || photo.original_filename;
    img.loading = "lazy";

    const info = document.createElement("div");
    info.className = "photo-card-info";

    let html = "";
    if (photo.campaign) {
      html += `<span class="campaign-tag">${escapeHtml(photo.campaign)}</span>`;
    }

    const displayDesc = photo.description || "";
    if (displayDesc) {
      html += `<div class="photo-title" title="${escapeHtml(displayDesc)}">${escapeHtml(displayDesc)}</div>`;
    } else {
      html += `<div class="photo-title">${escapeHtml(photo.original_filename)}</div>`;
    }

    const altCount = (photo.alternatives || []).length;
    if (altCount > 0) {
      html += `<div class="alt-count">+${altCount} similar</div>`;
    }

    info.innerHTML = html;
    card.appendChild(img);
    card.appendChild(info);
    return card;
  }

  // Infinite scroll
  window.addEventListener("scroll", () => {
    if (renderedCount >= filteredPhotos.length) return;
    const scrollBottom = window.innerHeight + window.scrollY;
    if (scrollBottom >= document.body.offsetHeight - 500) {
      renderBatch();
    }
  });

  // --- Modal ---

  function openModal(photo) {
    currentPhoto = photo;
    history.replaceState(null, "", `#${photo.id}`);
    const modal = document.getElementById("modal");
    modal.hidden = false;
    document.body.style.overflow = "hidden";

    const img = document.getElementById("modal-img");
    img.src = photo.thumbnail_url || `thumbnails/${photo.id}.jpg`;
    img.alt = photo.alt_text || photo.description || "";

    document.getElementById("modal-filename").textContent = photo.original_filename;

    setEditableField("modal-description", photo.description, "Click to add description");
    setEditableField("modal-alt-text", photo.alt_text, "Click to add alt text");
    setEditableField("modal-keywords", (photo.keywords || []).join(", "), "Click to add keywords");
    setEditableField("modal-campaign", photo.campaign, "Click to set campaign");
    setEditableField("modal-credit", photo.credit, "Click to add credit");

    document.getElementById("modal-date-added").textContent = photo.date_added || "—";

    const loc = (photo.locations || [])[0];
    if (loc && loc.width && loc.height) {
      document.getElementById("modal-dimensions").textContent =
        `${loc.width} x ${loc.height} (${formatFileSize(loc.file_size_bytes)})`;
    } else {
      document.getElementById("modal-dimensions").textContent = "—";
    }

    renderLocations(photo);

    document.getElementById("modal-drive-link").href = photo.drive_file_url || "#";
    document.getElementById("modal-folder-link").href = photo.drive_folder_url || "#";

    renderAlternatives(photo);
  }

  function setEditableField(id, value, placeholder) {
    const el = document.getElementById(id);
    if (value) {
      el.textContent = value;
      el.classList.remove("empty");
    } else {
      el.textContent = placeholder;
      el.classList.add("empty");
    }
  }

  function renderLocations(photo) {
    const list = document.getElementById("modal-locations-list");
    list.innerHTML = "";

    (photo.locations || []).forEach((loc) => {
      const li = document.createElement("li");
      li.innerHTML = `
        <a href="https://drive.google.com/file/d/${loc.drive_file_id}/view" target="_blank" rel="noopener">
          ${escapeHtml(loc.folder_path || "Unknown folder")}
        </a>
        <span class="location-size">
          ${loc.width ? `${loc.width}x${loc.height}` : ""} ${formatFileSize(loc.file_size_bytes)}
        </span>
      `;
      list.appendChild(li);
    });
  }

  function renderAlternatives(photo) {
    const section = document.getElementById("modal-alternatives");
    const grid = document.getElementById("alternatives-grid");
    const alts = photo.alternatives || [];

    if (alts.length === 0) {
      section.hidden = true;
      return;
    }

    section.hidden = false;
    grid.innerHTML = "";

    alts.forEach((alt) => {
      const img = document.createElement("img");
      img.src = alt.thumbnail_url || `thumbnails/${alt.filename.replace(/\.[^.]+$/, ".jpg")}`;
      img.alt = alt.original_filename || alt.filename;
      img.title = `${alt.filename} (${alt.folder_path})`;
      img.addEventListener("click", (e) => {
        e.stopPropagation();
        window.open(alt.drive_file_url, "_blank");
      });
      grid.appendChild(img);
    });
  }

  // Close modal
  function closeModal() {
    document.getElementById("modal").hidden = true;
    document.body.style.overflow = "";
    currentPhoto = null;
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }

  document.querySelector(".modal-close").addEventListener("click", closeModal);
  document.querySelector(".modal-backdrop").addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!document.getElementById("edit-modal").hidden) {
        closeEditModal();
      } else if (!document.getElementById("modal").hidden) {
        closeModal();
      }
    }
  });

  // --- Inline Editing ---

  document.querySelectorAll(".editable").forEach((el) => {
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!currentPhoto) return;
      openEditModal(el.dataset.field);
    });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (!currentPhoto) return;
        openEditModal(el.dataset.field);
      }
    });
  });

  function openEditModal(field) {
    editingField = field;
    const editModal = document.getElementById("edit-modal");
    const input = document.getElementById("edit-input");
    const fieldName = document.getElementById("edit-field-name");
    const hint = document.getElementById("edit-hint");

    fieldName.textContent = field.replace(/_/g, " ");

    let value = "";
    if (field === "keywords") {
      value = (currentPhoto.keywords || []).join(", ");
      hint.textContent = "Comma-separated keywords";
      input.rows = 3;
    } else {
      value = currentPhoto[field] || "";
      hint.textContent = "";
      input.rows = field === "description" || field === "alt_text" ? 4 : 2;
    }

    input.value = value;
    editModal.hidden = false;
    input.focus();
  }

  function closeEditModal() {
    document.getElementById("edit-modal").hidden = true;
    editingField = null;
  }

  document.getElementById("edit-cancel").addEventListener("click", closeEditModal);
  document.querySelector(".edit-modal-backdrop").addEventListener("click", closeEditModal);

  document.getElementById("edit-save").addEventListener("click", async () => {
    if (!currentPhoto || !editingField) return;

    const input = document.getElementById("edit-input");
    let value = input.value.trim();

    if (editingField === "keywords") {
      currentPhoto.keywords = value
        ? value.split(",").map((k) => k.trim()).filter(Boolean)
        : [];
    } else {
      currentPhoto[editingField] = value;
    }

    openModal(currentPhoto);
    closeEditModal();

    try {
      const pw = sessionStorage.getItem("pw");
      await fetch("/.netlify/functions/update-record", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${pw}`,
        },
        body: JSON.stringify({
          id: currentPhoto.id,
          field: editingField,
          value: editingField === "keywords" ? currentPhoto.keywords : value,
        }),
      });
    } catch (err) {
      console.error("Save error:", err);
      showSyncStatus("Failed to save changes. They will be lost on reload.");
    }
  });

  // --- Sync ---

  document.getElementById("sync-btn").addEventListener("click", async () => {
    const btn = document.getElementById("sync-btn");
    btn.disabled = true;
    btn.textContent = "Syncing...";
    showSyncStatus("Starting sync...");

    try {
      const pw = sessionStorage.getItem("pw");
      const res = await fetch("/.netlify/functions/sync-drive", {
        method: "POST",
        headers: { "Authorization": `Bearer ${pw}` },
      });

      if (res.ok) {
        const result = await res.json();
        showSyncStatus(result.message || "Sync started. New photos will appear in a few minutes.");
      } else {
        showSyncStatus("Failed to start sync. Please try again.");
      }
    } catch (err) {
      console.error("Sync error:", err);
      showSyncStatus("Failed to start sync. Please try again.");
    }

    btn.disabled = false;
    btn.textContent = "Sync";
  });

  function showSyncStatus(message) {
    const el = document.getElementById("sync-status");
    document.getElementById("sync-message").textContent = message;
    el.hidden = false;
    setTimeout(() => {
      el.hidden = true;
    }, 5000);
  }

  // --- Utilities ---

  function shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function formatFileSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  // --- Init ---

  if (checkAuth()) {
    showApp();
  }
})();
