const state = {
    ...window.galleryState,
    currentPhoto: null,
    currentIndex: -1,
    playTimer: null,
    scanPollTimer: null,
    scanStatusClosed: false,
    detailsVisible: window.localStorage.getItem("gallery.detailsVisible") === "true",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function tagsFromInput(value) {
    return value.split(",").map((tag) => tag.trim()).filter(Boolean);
}

function renderTags(tags) {
    if (!tags || !tags.length) {
        return '<span class="muted">Aucun</span>';
    }
    return tags.map((tag) => `<span class="tag">${escapeHtml(tag.name || tag)}</span>`).join("");
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
        throw new Error(data.error || "Erreur serveur");
    }
    return data;
}

function setBusy(button, busyText) {
    if (!button) {
        return () => {};
    }
    const previous = button.textContent;
    button.disabled = true;
    button.textContent = busyText;
    return () => {
        button.disabled = false;
        button.textContent = previous;
    };
}

async function openPhoto(photoId) {
    const data = await fetchJson(`/api/photos/${photoId}`);
    state.currentPhoto = data.photo;
    state.currentIndex = state.photos.findIndex((photo) => photo.id === photoId);
    renderPhotoDetail(data.photo);
    applyDetailsVisibility();
    $("#photo-modal").classList.add("open");
    $("#photo-modal").setAttribute("aria-hidden", "false");
}

function closePhotoModal() {
    stopSlideshow();
    $("#photo-modal").classList.remove("open");
    $("#photo-modal").setAttribute("aria-hidden", "true");
}

function renderPhotoDetail(photo) {
    $("#viewer-image").src = photo.original_url || photo.thumbnail_url;
    $("#viewer-image").alt = photo.memberships[0]?.filename || photo.checksum;
    $("#detail-title").textContent = photo.memberships[0]?.filename || photo.checksum.slice(0, 12);
    $("#detail-albums").innerHTML = photo.memberships
        .map((membership) => `<span class="tag">${escapeHtml(membership.album_name)} · ${escapeHtml(membership.type)}</span>`)
        .join("");
    const metadata = photo.metadata || {};
    $("#detail-seed").textContent = metadata.seed_noise || metadata.seed || "-";
    $("#detail-used-images").innerHTML = photo.used_images.length
        ? photo.used_images.map((name) => `<div>${escapeHtml(name)}</div>`).join("")
        : '<span class="muted">Aucune</span>';
    $("#detail-loras").innerHTML = photo.loras.length
        ? photo.loras.map((lora) => `<div>${escapeHtml(lora.lora_name)} <span class="muted">${escapeHtml(lora.strength_model ?? "")}</span></div>`).join("")
        : '<span class="muted">Aucun</span>';
    $("#detail-prompt").textContent = metadata.prompt || "Aucun prompt extrait.";
    $("#detail-tags").innerHTML = renderTags(photo.tags);
    $("#photo-tags-input").value = photo.tags.map((tag) => tag.name).join(", ");
    renderLinks(photo.links);
    renderLinkedStrip(photo.links);
}

function applyDetailsVisibility() {
    const viewer = document.querySelector(".photo-viewer");
    const button = $("#details-toggle-button");
    if (!viewer || !button) {
        return;
    }
    viewer.classList.toggle("details-open", state.detailsVisible);
    button.setAttribute("aria-pressed", state.detailsVisible ? "true" : "false");
    button.title = state.detailsVisible ? "Masquer les détails" : "Afficher les détails";
}

function toggleDetailsPanel() {
    state.detailsVisible = !state.detailsVisible;
    window.localStorage.setItem("gallery.detailsVisible", state.detailsVisible ? "true" : "false");
    applyDetailsVisibility();
}

function renderLinks(links) {
    const container = $("#detail-links");
    if (!links.length) {
        container.innerHTML = '<span class="muted">Aucun lien</span>';
        return;
    }
    container.innerHTML = links.map((link) => `
        <div class="link-item">
            <img src="${link.thumbnail_url}" alt="">
            <button type="button" data-open-linked="${link.linked_photo_id}">
                ${escapeHtml(link.type)} · ${escapeHtml(link.filename)}
            </button>
            <button type="button" data-delete-link="${link.id}" title="Supprimer">×</button>
        </div>
    `).join("");
}

function renderLinkedStrip(links) {
    const strip = $("#linked-strip");
    if (!links.length) {
        strip.innerHTML = "";
        return;
    }
    strip.innerHTML = links.map((link) => `
        <button type="button" data-open-linked="${link.linked_photo_id}" title="${escapeHtml(link.type)}">
            <img src="${link.thumbnail_url}" alt="${escapeHtml(link.filename)}">
        </button>
    `).join("");
}

async function rescanCurrentMetadata() {
    if (!state.currentPhoto) {
        return;
    }
    const done = setBusy($("#rescan-metadata-button"), "Scan...");
    try {
        const data = await fetchJson(`/api/photos/${state.currentPhoto.id}/metadata/rescan`, { method: "POST", body: "{}" });
        state.currentPhoto = data.photo;
        renderPhotoDetail(data.photo);
    } catch (error) {
        alert(error.message);
    } finally {
        done();
    }
}

async function savePhotoTags() {
    if (!state.currentPhoto) {
        return;
    }
    const data = await fetchJson(`/api/photos/${state.currentPhoto.id}/tags`, {
        method: "PUT",
        body: JSON.stringify({ tags: tagsFromInput($("#photo-tags-input").value) }),
    });
    state.currentPhoto = data.photo;
    renderPhotoDetail(data.photo);
}

async function searchLinkTargets() {
    const query = $("#link-search-input").value.trim();
    const container = $("#link-search-results");
    if (query.length < 2) {
        container.innerHTML = "";
        return;
    }
    const data = await fetchJson(`/api/photos/search?q=${encodeURIComponent(query)}`);
    container.innerHTML = data.photos
        .filter((photo) => !state.currentPhoto || photo.id !== state.currentPhoto.id)
        .map((photo) => `
            <button type="button" class="search-result" data-link-target="${photo.id}">
                <img src="${photo.thumbnail_url}" alt="">
                <span>${escapeHtml(photo.filename)}<small>${escapeHtml(photo.album_name)}</small></span>
            </button>
        `).join("");
}

async function createLink(targetPhotoId) {
    if (!state.currentPhoto) {
        return;
    }
    const data = await fetchJson(`/api/photos/${state.currentPhoto.id}/links`, {
        method: "POST",
        body: JSON.stringify({
            target_photo_id: targetPhotoId,
            type: $("#link-type-select").value,
        }),
    });
    state.currentPhoto = data.photo;
    renderPhotoDetail(data.photo);
}

async function deleteLink(linkId) {
    await fetchJson(`/api/photo-links/${linkId}`, { method: "DELETE" });
    await openPhoto(state.currentPhoto.id);
}

function navigate(delta) {
    if (!state.photos.length) {
        return;
    }
    const nextIndex = (state.currentIndex + delta + state.photos.length) % state.photos.length;
    openPhoto(state.photos[nextIndex].id);
}

function toggleSlideshow() {
    if (state.playTimer) {
        stopSlideshow();
        return;
    }
    $("#play-button").textContent = "Ⅱ";
    state.playTimer = window.setInterval(() => navigate(1), 3500);
}

function stopSlideshow() {
    if (state.playTimer) {
        window.clearInterval(state.playTimer);
        state.playTimer = null;
    }
    $("#play-button").textContent = "▶";
}

function openAdmin() {
    renderAlbumAdmin();
    $("#admin-modal").classList.add("open");
    $("#admin-modal").setAttribute("aria-hidden", "false");
}

function closeAdmin() {
    $("#admin-modal").classList.remove("open");
    $("#admin-modal").setAttribute("aria-hidden", "true");
}

function renderAlbumAdmin() {
    $("#album-admin-list").innerHTML = state.albums.map((album) => `
        <form class="album-admin-card" data-album-id="${album.id}">
            <div>
                <strong>${escapeHtml(album.name)}</strong>
                <small>${album.photo_count || 0} images</small>
            </div>
            <input name="display_name" type="text" value="${escapeHtml(album.display_name)}" aria-label="Nom affiché">
            <select name="type">
                ${state.allowedAlbumTypes.map((type) => `<option value="${type}" ${album.type === type ? "selected" : ""}>${type}</option>`).join("")}
            </select>
            <input name="tags" type="text" value="${escapeHtml((album.tags || []).join(", "))}" placeholder="tags album">
            ${album.scan_error ? `<p class="error">${escapeHtml(album.scan_error)}</p>` : ""}
            <button type="submit">Enregistrer</button>
        </form>
    `).join("");
}

async function saveAlbum(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const albumId = form.dataset.albumId;
    const data = Object.fromEntries(new FormData(form).entries());
    const response = await fetchJson(`/api/albums/${albumId}`, {
        method: "PATCH",
        body: JSON.stringify({
            display_name: data.display_name,
            type: data.type,
            tags: tagsFromInput(data.tags || ""),
        }),
    });
    state.albums = response.albums;
    renderAlbumAdmin();
}

async function scanAlbums() {
    const done = setBusy($("#scan-button"), "Scan...");
    try {
        state.scanStatusClosed = false;
        const data = await fetchJson("/api/scan", { method: "POST", body: JSON.stringify({ metadata: false }) });
        renderScanStatus(data.job, { force: true });
        startScanPolling(done);
    } catch (error) {
        alert(error.message);
        done();
    }
}

function startScanPolling(done) {
    window.clearInterval(state.scanPollTimer);
    state.scanPollTimer = window.setInterval(async () => {
        try {
            const data = await fetchJson("/api/scan/status");
            renderScanStatus(data.job, { force: true });
            if (!data.job.active) {
                window.clearInterval(state.scanPollTimer);
                done();
                if (data.job.state === "done") {
                    window.location.reload();
                }
            }
        } catch (error) {
            window.clearInterval(state.scanPollTimer);
            done();
            alert(error.message);
        }
    }, 1000);
}

function renderScanStatus(job, options = {}) {
    const box = $("#scan-status");
    if (!box || !job) {
        return;
    }
    if (!options.force && !job.active) {
        return;
    }
    if (state.scanStatusClosed && !job.active) {
        return;
    }
    box.hidden = false;
    box.dataset.state = job.state || "running";
    $("#scan-status-title").textContent = job.state === "done" ? "Scan termine" : job.state === "error" ? "Scan en erreur" : "Scan en cours";
    $("#scan-status-message").textContent = job.message || "Scan...";
    $("#scan-status-close").hidden = Boolean(job.active);
    const details = [];
    if (job.album) {
        details.push(`album: ${job.album}`);
    }
    if (job.file) {
        details.push(`fichier: ${job.file}`);
    }
    if (job.album_photos || job.photos) {
        details.push(`images: ${job.album_photos || 0} album / ${job.photos || 0} total`);
    }
    if (job.errors && job.errors.length) {
        details.push(`erreurs: ${job.errors.length}`);
    }
    $("#scan-status-detail").textContent = details.join(" | ");
}

async function resumeScanStatusIfNeeded() {
    try {
        const data = await fetchJson("/api/scan/status");
        if (data.job.active) {
            const done = setBusy($("#scan-button"), "Scan...");
            renderScanStatus(data.job, { force: true });
            startScanPolling(done);
        }
    } catch (_error) {
        // Scan status is non-critical during initial page load.
    }
}

function bindEvents() {
    $("#album-select")?.addEventListener("change", (event) => {
        window.location.href = `?album=${encodeURIComponent(event.target.value)}&page=1`;
    });
    $("#scan-button")?.addEventListener("click", scanAlbums);
    $("#scan-status-close")?.addEventListener("click", () => {
        state.scanStatusClosed = true;
        $("#scan-status").hidden = true;
    });
    $("#admin-button")?.addEventListener("click", openAdmin);
    $("#rescan-metadata-button")?.addEventListener("click", rescanCurrentMetadata);
    $("#save-photo-tags-button")?.addEventListener("click", savePhotoTags);
    $("#link-search-input")?.addEventListener("input", debounce(searchLinkTargets, 250));
    $("#prev-button")?.addEventListener("click", () => navigate(-1));
    $("#next-button")?.addEventListener("click", () => navigate(1));
    $("#play-button")?.addEventListener("click", toggleSlideshow);
    $("#details-toggle-button")?.addEventListener("click", toggleDetailsPanel);
    $$("[data-close-modal]").forEach((button) => button.addEventListener("click", closePhotoModal));
    $$("[data-close-admin]").forEach((button) => button.addEventListener("click", closeAdmin));

    $("#gallery-list")?.addEventListener("click", (event) => {
        const button = event.target.closest("[data-photo-id]");
        if (button) {
            openPhoto(Number(button.dataset.photoId)).catch((error) => alert(error.message));
        }
    });

    document.body.addEventListener("click", (event) => {
        const linked = event.target.closest("[data-open-linked]");
        if (linked) {
            openPhoto(Number(linked.dataset.openLinked)).catch((error) => alert(error.message));
        }
        const deleteButton = event.target.closest("[data-delete-link]");
        if (deleteButton) {
            deleteLink(Number(deleteButton.dataset.deleteLink)).catch((error) => alert(error.message));
        }
        const linkTarget = event.target.closest("[data-link-target]");
        if (linkTarget) {
            createLink(Number(linkTarget.dataset.linkTarget)).catch((error) => alert(error.message));
        }
    });

    $("#album-admin-list")?.addEventListener("submit", (event) => {
        if (event.target.matches(".album-admin-card")) {
            saveAlbum(event).catch((error) => alert(error.message));
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closePhotoModal();
            closeAdmin();
        }
        if ($("#photo-modal").classList.contains("open") && event.key === "ArrowRight") {
            navigate(1);
        }
        if ($("#photo-modal").classList.contains("open") && event.key === "ArrowLeft") {
            navigate(-1);
        }
    });
}

function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => fn(...args), delay);
    };
}

bindEvents();
resumeScanStatusIfNeeded();
