const state = {
    ...window.galleryState,
    currentPhoto: null,
    currentIndex: -1,
    playTimer: null,
    scanPollTimer: null,
    scanStatusClosed: false,
    detailsVisible: window.localStorage.getItem("gallery.detailsVisible") === "true",
    comfyAvailable: false,
    comfyOptions: null,
    comfyReferences: {},
    comfyJobPollTimer: null,
    comfyPreviewVersion: null,
    comfyPollFailures: 0,
    swipeStartX: null,
    swipeStartY: null,
    swipeStartAt: 0,
    viewerLoadId: 0,
    zoomScale: 1,
    zoomTranslateX: 0,
    zoomTranslateY: 0,
    pinchStartDistance: null,
    pinchStartScale: 1,
    panStartX: null,
    panStartY: null,
    panStartTranslateX: 0,
    panStartTranslateY: 0,
    albumActionMode: null,
    albumActionSource: null,
    photoModalHistoryActive: false,
    tagFilterDraft: {},
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
    const previous = button.innerHTML;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = busyText;
    return () => {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.innerHTML = previous;
    };
}

async function openPhoto(photoId, options = {}) {
    const modal = $("#photo-modal");
    const wasOpen = modal.classList.contains("open");
    const loadId = ++state.viewerLoadId;
    resetViewerZoom();
    setViewerLoading(true);
    let data;
    try {
        data = await fetchJson(`/api/photos/${photoId}`);
    } catch (error) {
        if (loadId === state.viewerLoadId) {
            setViewerLoading(false);
        }
        throw error;
    }
    if (loadId !== state.viewerLoadId) {
        return;
    }
    state.currentPhoto = data.photo;
    state.currentIndex = state.photos.findIndex((photo) => photo.id === photoId);
    renderPhotoDetail(data.photo);
    applyDetailsVisibility();
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    if (!wasOpen && !options.skipHistory && !state.photoModalHistoryActive) {
        window.history.pushState({ ...(window.history.state || {}), photoModal: true }, "", window.location.href);
        state.photoModalHistoryActive = true;
    }
    await loadViewerImage(data.photo.original_url || data.photo.thumbnail_url, loadId);
}

function setViewerLoading(loading) {
    const indicator = $("#viewer-loading");
    if (indicator) {
        indicator.hidden = !loading;
    }
}

function loadViewerImage(url, loadId) {
    return new Promise((resolve) => {
        const preload = new Image();
        preload.onload = () => {
            if (loadId === state.viewerLoadId) {
                resetViewerZoom();
                $("#viewer-image").src = url;
                setViewerLoading(false);
            }
            resolve();
        };
        preload.onerror = () => {
            if (loadId === state.viewerLoadId) {
                setViewerLoading(false);
            }
            resolve();
        };
        preload.src = url;
    });
}

function closePhotoModal(options = {}) {
    const modal = $("#photo-modal");
    if (!modal.classList.contains("open")) {
        state.photoModalHistoryActive = false;
        return;
    }
    if (!options.fromHistory && state.photoModalHistoryActive) {
        window.history.back();
        return;
    }
    stopSlideshow();
    closePhotoActionsMenu();
    closeComfyModal();
    closeAlbumActionModal();
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    state.photoModalHistoryActive = false;
}

function renderPhotoDetail(photo) {
    $("#viewer-image").alt = photo.memberships[0]?.filename || photo.checksum;
    $("#detail-title").textContent = photo.memberships[0]?.filename || photo.checksum.slice(0, 12);
    $("#detail-albums").innerHTML = photo.memberships
        .map((membership) => `
            <span class="tag album-membership ${membership.available ? "" : "unavailable"}"
                  title="${membership.available ? "Album disponible" : "Album non joignable"}">
                ${membership.available ? "" : '<span class="album-unavailable-icon" aria-label="Album non joignable">⚠</span>'}
                ${escapeHtml(membership.album_name)} · ${escapeHtml(membership.type)}
            </span>
        `)
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
    refreshComfyStatus();
}

function galleryPhotoFromDetail(photo) {
    const membership = photo.memberships.find((item) => item.album_name === state.selectedAlbum?.name) || photo.memberships[0];
    if (!membership) {
        return null;
    }
    return {
        id: photo.id,
        checksum: photo.checksum,
        filename: membership.filename,
        relative_path: membership.relative_path,
        album_name: membership.album_name,
        width: photo.width,
        height: photo.height,
        tags: photo.tags || [],
        favorite: Boolean(photo.memberships.some((item) => item.type === "output") && photo.memberships.some((item) => item.type === "user")),
        album_count: photo.memberships.length,
        user_album_count: photo.memberships.filter((item) => item.type === "user").length,
        original_url: photo.original_url,
        thumbnail_url: photo.thumbnail_url,
    };
}

function addPhotoToCurrentGallery(photo) {
    const gallery = $("#gallery-list");
    if (!gallery || !state.selectedAlbum || !photo.memberships.some((item) => item.album_name === state.selectedAlbum.name)) {
        return;
    }
    const galleryPhoto = galleryPhotoFromDetail(photo);
    if (!galleryPhoto) {
        return;
    }
    state.photos = [galleryPhoto, ...state.photos.filter((item) => item.id !== photo.id)];
    gallery.querySelector(`[data-photo-id="${photo.id}"]`)?.closest("li")?.remove();
    gallery.insertAdjacentHTML("afterbegin", renderGalleryItem(galleryPhoto));
}

function removePhotoFromCurrentGallery(photoId) {
    const gallery = $("#gallery-list");
    state.photos = state.photos.filter((item) => item.id !== photoId);
    gallery?.querySelector(`[data-photo-id="${photoId}"]`)?.closest("li")?.remove();
    state.currentIndex = state.photos.findIndex((item) => state.currentPhoto && item.id === state.currentPhoto.id);
}

function syncPhotoInCurrentGallery(photo) {
    if (!state.selectedAlbum) {
        return;
    }
    if (photo.memberships.some((item) => item.album_name === state.selectedAlbum.name)) {
        addPhotoToCurrentGallery(photo);
        state.currentIndex = state.photos.findIndex((item) => item.id === photo.id);
        return;
    }
    removePhotoFromCurrentGallery(photo.id);
}

function renderGalleryItem(photo) {
    return `
        <li>
            <button class="thumbnail" type="button" data-photo-id="${photo.id}">
                <img src="${photo.thumbnail_url}" alt="${escapeHtml(photo.filename)}" loading="lazy">
                ${photo.favorite ? `
                    <span class="favorite-badge" title="Present dans output et dans un album user">
                        *${photo.user_album_count > 1 ? `<small>${photo.user_album_count}</small>` : ""}
                    </span>
                ` : ""}
                <span class="filename">${escapeHtml(photo.filename)}</span>
            </button>
        </li>
    `;
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

function updateComfyButton() {
    const button = $("#comfy-generate-button");
    if (!button) {
        return;
    }
    button.disabled = !state.comfyAvailable || !state.currentPhoto;
    button.title = state.comfyAvailable ? "Regenerer avec ComfyUI" : "ComfyUI indisponible";
}

async function refreshComfyStatus() {
    try {
        const data = await fetchJson("/api/comfy/status");
        state.comfyAvailable = Boolean(data.available);
    } catch (_error) {
        state.comfyAvailable = false;
    }
    updateComfyButton();
}

function togglePhotoActionsMenu() {
    const menu = $("#photo-actions-menu");
    const button = $("#photo-actions-button");
    if (!menu || !button) {
        return;
    }
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    button.setAttribute("aria-expanded", willOpen ? "true" : "false");
}

function closePhotoActionsMenu() {
    const menu = $("#photo-actions-menu");
    const button = $("#photo-actions-button");
    if (menu) {
        menu.hidden = true;
    }
    if (button) {
        button.setAttribute("aria-expanded", "false");
    }
}

function currentSourceMembership() {
    if (!state.currentPhoto || !state.currentPhoto.memberships.length) {
        return null;
    }
    return state.currentPhoto.memberships.find((item) => item.album_name === state.selectedAlbum?.name) || state.currentPhoto.memberships[0];
}

function albumActionLabel(mode) {
    return mode === "move" ? "Deplacer vers..." : "Copier vers...";
}

function openAlbumActionModal(mode) {
    if (!state.currentPhoto) {
        return;
    }
    closePhotoActionsMenu();
    state.albumActionMode = mode;
    state.albumActionSource = currentSourceMembership();
    $("#album-action-title").textContent = albumActionLabel(mode);
    $("#album-action-submit").textContent = mode === "move" ? "Deplacer" : "Copier";
    $("#album-action-status").textContent = "";
    $("#album-action-status").classList.remove("error");
    renderAlbumActionOptions();
    $("#album-action-modal").classList.add("open");
    $("#album-action-modal").setAttribute("aria-hidden", "false");
}

function closeAlbumActionModal() {
    $("#album-action-modal").classList.remove("open");
    $("#album-action-modal").setAttribute("aria-hidden", "true");
    state.albumActionMode = null;
    state.albumActionSource = null;
}

function renderAlbumActionOptions() {
    const select = $("#album-action-destination");
    const membershipNames = new Set((state.currentPhoto?.memberships || []).map((item) => item.album_name));
    const sourceName = state.albumActionSource?.album_name;
    const albums = state.albums.filter((album) => {
        if (album.scan_error) {
            return false;
        }
        if (state.albumActionMode === "move") {
            return album.name !== sourceName;
        }
        return !membershipNames.has(album.name);
    });
    select.innerHTML = albums.map((album) => (
        `<option value="${escapeHtml(album.name)}">${escapeHtml(album.display_name || album.name)} (${escapeHtml(album.type)})</option>`
    )).join("");
    const disabled = albums.length === 0;
    select.disabled = disabled;
    $("#album-action-submit").disabled = disabled;
    if (disabled) {
        $("#album-action-status").textContent = "Aucun album de destination disponible.";
    }
}

async function submitAlbumAction(event) {
    event.preventDefault();
    if (!state.currentPhoto || !state.albumActionMode) {
        return;
    }
    const done = setBusy($("#album-action-submit"), state.albumActionMode === "move" ? "Deplacement..." : "Copie...");
    $("#album-action-status").textContent = "";
    try {
        const data = await fetchJson(`/api/photos/${state.currentPhoto.id}/album-action`, {
            method: "POST",
            body: JSON.stringify({
                action: state.albumActionMode,
                destination_album_name: $("#album-action-destination").value,
                source_album_name: state.albumActionSource?.album_name,
            }),
        });
        state.albums = data.albums || state.albums;
        state.currentPhoto = data.photo;
        renderPhotoDetail(data.photo);
        syncPhotoInCurrentGallery(data.photo);
        closeAlbumActionModal();
    } catch (error) {
        $("#album-action-status").textContent = error.message;
        $("#album-action-status").classList.add("error");
    } finally {
        done();
    }
}

async function deleteCurrentPhoto() {
    if (!state.currentPhoto) {
        return;
    }
    closePhotoActionsMenu();
    const filename = state.currentPhoto.memberships[0]?.filename || state.currentPhoto.checksum.slice(0, 12);
    if (!window.confirm(`Supprimer definitivement "${filename}" de la base et du disque ?`)) {
        return;
    }
    const deletedPhotoId = state.currentPhoto.id;
    const currentIndex = state.photos.findIndex((photo) => photo.id === deletedPhotoId);
    const remainingPhotos = state.photos.filter((photo) => photo.id !== deletedPhotoId);
    const nextPhoto = remainingPhotos.length && currentIndex >= 0 ? remainingPhotos[Math.min(currentIndex, remainingPhotos.length - 1)] : null;
    try {
        const data = await fetchJson(`/api/photos/${deletedPhotoId}`, { method: "DELETE" });
        state.albums = data.albums || state.albums;
        removePhotoFromCurrentGallery(deletedPhotoId);
        if (nextPhoto) {
            await openPhoto(nextPhoto.id);
        } else {
            closePhotoModal();
            state.currentPhoto = null;
            state.currentIndex = -1;
        }
    } catch (error) {
        alert(error.message);
    }
}

async function openComfyModal() {
    if (!state.currentPhoto) {
        return;
    }
    closePhotoActionsMenu();
    const modal = $("#comfy-modal");
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    setComfyStatus("Chargement des options...");
    state.comfyOptions = null;
    state.comfyReferences = {};
    try {
        const data = await fetchJson(`/api/photos/${state.currentPhoto.id}/comfy/edit-options`);
        state.comfyOptions = data.options;
        renderComfyOptions(data.options);
        setComfyStatus("");
    } catch (error) {
        setComfyStatus(error.message, true);
    }
}

function closeComfyModal() {
    $("#comfy-modal").classList.remove("open");
    $("#comfy-modal").setAttribute("aria-hidden", "true");
}

function setComfyStatus(message, isError = false) {
    const status = $("#comfy-status");
    status.textContent = message || "";
    status.classList.toggle("error", Boolean(isError));
}

function renderComfyOptions(options) {
    $("#comfy-prompt").value = options.prompt || "";
    $("#comfy-seed-mode").value = "keep";
    $("#comfy-steps").value = options.steps || "";
    $("#comfy-preview").hidden = true;
    $("#comfy-preview-image").removeAttribute("src");
    $("#comfy-preview-image").onerror = () => {
        $("#comfy-preview").hidden = true;
    };
    state.comfyPreviewVersion = null;
    renderComfyLoras(options.loras || [], options.lora_catalog || []);
    renderComfyReferences(options.images || []);
}

function renderComfyLoras(loras, catalog) {
    const container = $("#comfy-loras");
    if (!loras.length) {
        container.innerHTML = '<span class="muted">Aucun LoRA editable</span>';
        return;
    }
    container.innerHTML = loras.map((lora) => {
        const names = catalog.map((item) => item.lora_name);
        if (!names.includes(lora.lora_name)) {
            names.unshift(lora.lora_name);
        }
        return `
            <div class="comfy-row comfy-lora-row" data-node-id="${escapeHtml(lora.node_id)}">
                <label class="checkbox-field">
                    <input type="checkbox" data-comfy-lora-enabled ${lora.enabled ? "checked" : ""}>
                    <span>${escapeHtml(lora.node_id)}</span>
                </label>
                <select data-comfy-lora-name>
                    ${names.map((name) => `<option value="${escapeHtml(name)}" ${name === lora.lora_name ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}
                </select>
                <input type="number" data-comfy-lora-strength step="0.05" value="${escapeHtml(lora.strength_model ?? 1)}" aria-label="Force LoRA">
            </div>
        `;
    }).join("");
}

function renderComfyReferences(images) {
    const container = $("#comfy-references");
    if (!images.length) {
        container.innerHTML = '<span class="muted">Aucune image source editable</span>';
        return;
    }
    container.innerHTML = images.map((image) => `
        <div class="comfy-ref-row" data-node-id="${escapeHtml(image.node_id)}">
            <div class="comfy-ref-current">
                <strong>Node ${escapeHtml(image.node_id)}</strong>
                <small>${escapeHtml(image.image_name)}</small>
                <span data-comfy-ref-selected></span>
            </div>
            <input type="search" data-comfy-ref-search placeholder="Chercher une photo de reference">
            <div class="search-results" data-comfy-ref-results></div>
        </div>
    `).join("");
}

async function searchComfyReferences(event) {
    const input = event.target;
    const row = input.closest(".comfy-ref-row");
    if (!row) {
        return;
    }
    const results = row.querySelector("[data-comfy-ref-results]");
    const query = input.value.trim();
    if (query.length < 2) {
        results.innerHTML = "";
        return;
    }
    const data = await fetchJson(`/api/photos/search?q=${encodeURIComponent(query)}`);
    const nodeId = row.dataset.nodeId;
    results.innerHTML = data.photos
        .filter((photo) => !state.currentPhoto || photo.id !== state.currentPhoto.id)
        .map((photo) => `
            <button type="button" class="search-result" data-comfy-ref-result="${photo.id}" data-node-id="${escapeHtml(nodeId)}" data-filename="${escapeHtml(photo.filename)}">
                <img src="${photo.thumbnail_url}" alt="">
                <span>${escapeHtml(photo.filename)}<small>${escapeHtml(photo.album_name)}</small></span>
            </button>
        `).join("");
}

const debouncedComfyReferenceSearch = debounce(searchComfyReferences, 250);

function selectComfyReference(button) {
    const nodeId = button.dataset.nodeId;
    const row = button.closest(".comfy-ref-row");
    state.comfyReferences[nodeId] = Number(button.dataset.comfyRefResult);
    row.querySelector("[data-comfy-ref-selected]").textContent = `Selection: ${button.dataset.filename}`;
    row.querySelector("[data-comfy-ref-results]").innerHTML = "";
    row.querySelector("[data-comfy-ref-search]").value = "";
}

async function submitComfyGeneration(event) {
    event.preventDefault();
    if (!state.currentPhoto || !state.comfyOptions) {
        return;
    }
    const done = setBusy($("#comfy-submit-button"), "Generation...");
    setComfyStatus("Envoi a ComfyUI...");
    const payload = {
        prompt: $("#comfy-prompt").value,
        seed_mode: $("#comfy-seed-mode").value,
        steps: Number($("#comfy-steps").value || state.comfyOptions.steps || 1),
        loras: $$(".comfy-lora-row").map((row) => ({
            node_id: row.dataset.nodeId,
            enabled: row.querySelector("[data-comfy-lora-enabled]").checked,
            lora_name: row.querySelector("[data-comfy-lora-name]").value,
            strength_model: Number(row.querySelector("[data-comfy-lora-strength]").value || 0),
        })),
        references: Object.entries(state.comfyReferences).map(([nodeId, photoId]) => ({
            node_id: nodeId,
            photo_id: photoId,
        })),
    };
    try {
        setComfyStatus("Lancement du job...");
        const data = await fetchJson(`/api/photos/${state.currentPhoto.id}/comfy/generate`, {
            method: "POST",
            body: JSON.stringify(payload),
        });
        startComfyJobPolling(data.job.id, done);
        renderComfyJob(data.job);
    } catch (error) {
        setComfyStatus(error.message, true);
        done();
        refreshComfyStatus();
    }
}

function startComfyJobPolling(jobId, done) {
    stopComfyJobPolling();
    state.comfyPollFailures = 0;
    pollComfyJob(jobId, done, 1000);
}

function stopComfyJobPolling() {
    window.clearTimeout(state.comfyJobPollTimer);
    state.comfyJobPollTimer = null;
}

function pollComfyJob(jobId, done, delay) {
    state.comfyJobPollTimer = window.setTimeout(async () => {
        try {
            const data = await fetchJson(`/api/comfy/jobs/${jobId}`);
            state.comfyPollFailures = 0;
            renderComfyJob(data.job);
            if (!data.job.active) {
                stopComfyJobPolling();
                done();
                refreshComfyStatus();
                if (data.job.state === "done" && data.job.photo) {
                    addPhotoToCurrentGallery(data.job.photo);
                    closeComfyModal();
                    await openPhoto(data.job.photo.id);
                }
                return;
            }
            pollComfyJob(jobId, done, 1000);
        } catch (error) {
            state.comfyPollFailures += 1;
            if (state.comfyPollFailures >= 8) {
                stopComfyJobPolling();
                setComfyStatus(`Suivi interrompu: ${error.message}`, true);
                done();
                refreshComfyStatus();
                return;
            }
            const retryDelay = Math.min(1000 * Math.pow(1.7, state.comfyPollFailures), 10000);
            setComfyStatus(`Connexion temporairement perdue, nouvelle tentative ${state.comfyPollFailures}/8...`);
            pollComfyJob(jobId, done, retryDelay);
        }
    }, delay);
}

function renderComfyJob(job) {
    const pieces = [job.message || job.state || "Generation"];
    if (job.node) {
        pieces.push(`node ${job.node}`);
    }
    if (job.progress && job.progress_max) {
        pieces.push(`${job.progress}/${job.progress_max}`);
    }
    if (job.prompt_id) {
        pieces.push(job.prompt_id);
    }
    setComfyStatus(pieces.join(" | "), job.state === "error");
    if (job.preview_available) {
        const version = job.preview_updated_at || Date.now();
        if (state.comfyPreviewVersion !== version) {
            state.comfyPreviewVersion = version;
            $("#comfy-preview-image").src = `/api/comfy/jobs/${job.id}/preview?t=${encodeURIComponent(version)}`;
        }
        $("#comfy-preview").hidden = false;
    }
    if (job.state === "done" && !job.photo) {
        setComfyStatus("Generation terminee, image non retrouvee dans l'album output.", true);
    }
    if (job.state === "error") {
        setComfyStatus(job.error || job.message || "Generation en erreur", true);
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

function resetViewerSwipe() {
    state.swipeStartX = null;
    state.swipeStartY = null;
    state.swipeStartAt = 0;
}

function resetViewerZoom() {
    state.zoomScale = 1;
    state.zoomTranslateX = 0;
    state.zoomTranslateY = 0;
    state.pinchStartDistance = null;
    state.pinchStartScale = 1;
    state.panStartX = null;
    state.panStartY = null;
    applyViewerTransform();
}

function applyViewerTransform() {
    $("#viewer-image").style.transform = `translate3d(${state.zoomTranslateX}px, ${state.zoomTranslateY}px, 0) scale(${state.zoomScale})`;
}

function clampViewerTranslation() {
    const image = $("#viewer-image");
    const stage = $(".viewer-stage");
    if (!image.naturalWidth || !image.naturalHeight || !stage.clientWidth || !stage.clientHeight) {
        return;
    }
    const fitScale = Math.min(stage.clientWidth / image.naturalWidth, stage.clientHeight / image.naturalHeight);
    const renderedWidth = image.naturalWidth * fitScale * state.zoomScale;
    const renderedHeight = image.naturalHeight * fitScale * state.zoomScale;
    const maxX = Math.max(0, (renderedWidth - stage.clientWidth) / 2);
    const maxY = Math.max(0, (renderedHeight - stage.clientHeight) / 2);
    state.zoomTranslateX = Math.min(maxX, Math.max(-maxX, state.zoomTranslateX));
    state.zoomTranslateY = Math.min(maxY, Math.max(-maxY, state.zoomTranslateY));
}

function touchDistance(touches) {
    return Math.hypot(
        touches[0].clientX - touches[1].clientX,
        touches[0].clientY - touches[1].clientY,
    );
}

function isInteractiveSwipeTarget(target) {
    return Boolean(target.closest("button, input, select, textarea, a, [role='button']"));
}

function handleViewerTouchStart(event) {
    if (event.touches.length === 2 && !isInteractiveSwipeTarget(event.target)) {
        resetViewerSwipe();
        state.panStartX = null;
        state.panStartY = null;
        state.pinchStartDistance = touchDistance(event.touches);
        state.pinchStartScale = state.zoomScale;
        return;
    }
    if (event.touches.length === 1 && state.zoomScale > 1 && !isInteractiveSwipeTarget(event.target)) {
        resetViewerSwipe();
        state.panStartX = event.touches[0].clientX;
        state.panStartY = event.touches[0].clientY;
        state.panStartTranslateX = state.zoomTranslateX;
        state.panStartTranslateY = state.zoomTranslateY;
        return;
    }
    if (event.touches.length !== 1 || isInteractiveSwipeTarget(event.target)) {
        resetViewerSwipe();
        return;
    }
    const touch = event.touches[0];
    state.swipeStartX = touch.clientX;
    state.swipeStartY = touch.clientY;
    state.swipeStartAt = Date.now();
}

function handleViewerTouchMove(event) {
    if (event.touches.length === 1 && state.panStartX !== null) {
        event.preventDefault();
        state.zoomTranslateX = state.panStartTranslateX + event.touches[0].clientX - state.panStartX;
        state.zoomTranslateY = state.panStartTranslateY + event.touches[0].clientY - state.panStartY;
        clampViewerTranslation();
        applyViewerTransform();
        return;
    }
    if (event.touches.length !== 2 || state.pinchStartDistance === null) {
        return;
    }
    event.preventDefault();
    const scale = state.pinchStartScale * (touchDistance(event.touches) / state.pinchStartDistance);
    state.zoomScale = Math.min(5, Math.max(1, scale));
    clampViewerTranslation();
    applyViewerTransform();
}

function handleViewerTouchCancel() {
    resetViewerSwipe();
    state.pinchStartDistance = null;
    state.pinchStartScale = state.zoomScale;
    state.panStartX = null;
    state.panStartY = null;
}

function handleViewerTouchEnd(event) {
    if (state.pinchStartDistance !== null) {
        if (event.touches.length < 2) {
            state.pinchStartDistance = null;
            state.pinchStartScale = state.zoomScale;
        }
        resetViewerSwipe();
        return;
    }
    if (state.panStartX !== null) {
        state.panStartX = null;
        state.panStartY = null;
        resetViewerSwipe();
        return;
    }
    if (state.swipeStartX === null || event.changedTouches.length !== 1) {
        resetViewerSwipe();
        return;
    }
    const touch = event.changedTouches[0];
    const deltaX = touch.clientX - state.swipeStartX;
    const deltaY = touch.clientY - state.swipeStartY;
    const elapsed = Date.now() - state.swipeStartAt;
    resetViewerSwipe();

    if (!$("#photo-modal").classList.contains("open") || elapsed > 1200) {
        return;
    }

    const horizontal = Math.abs(deltaX);
    const vertical = Math.abs(deltaY);
    if (horizontal < 55 || horizontal < vertical * 1.4) {
        return;
    }
    navigate(deltaX < 0 ? 1 : -1);
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

function appliedTagFilterState(tagName) {
    if ((state.tagFilters?.include || []).includes(tagName)) {
        return "true";
    }
    if ((state.tagFilters?.exclude || []).includes(tagName)) {
        return "false";
    }
    return "any";
}

function renderTagFilters() {
    const container = $("#tag-filter-list");
    const tagStats = state.albumTagStats || [];
    if (!tagStats.length) {
        container.innerHTML = '<p class="muted tag-filter-empty">Aucun tag photo dans cet album.</p>';
        return;
    }
    const maxOccurrence = Math.max(...tagStats.map((tag) => Number(tag.occurrence_count) || 0), 1);
    container.innerHTML = tagStats.map((tag) => {
        const tagId = String(tag.id);
        const selected = state.tagFilterDraft[tagId] || "any";
        const occurrence = Number(tag.occurrence_count) || 0;
        const progress = Math.min(100, Math.max(0, (occurrence / maxOccurrence) * 100));
        const escapedName = escapeHtml(tag.name);
        return `
            <div class="tag-filter-row">
                <span class="tag-filter-name" title="${escapedName}">${escapedName}</span>
                <span class="tag-filter-count" style="--tag-progress: ${progress}%" title="${occurrence} occurrence${occurrence > 1 ? "s" : ""}">
                    ${occurrence}
                </span>
                <div class="tag-filter-toggle" role="group" aria-label="Filtre ${escapedName}">
                    ${["any", "true", "false"].map((value) => `
                        <button type="button"
                                class="tag-filter-choice is-${value}${selected === value ? " is-selected" : ""}"
                                data-tag-filter-id="${tagId}"
                                data-tag-filter-value="${value}"
                                aria-pressed="${selected === value}">
                            ${value === "any" ? "Any" : value === "true" ? "True" : "False"}
                        </button>
                    `).join("")}
                </div>
            </div>
        `;
    }).join("");
}

function openTagFilter() {
    state.tagFilterDraft = {};
    (state.albumTagStats || []).forEach((tag) => {
        state.tagFilterDraft[String(tag.id)] = appliedTagFilterState(tag.name);
    });
    renderTagFilters();
    $("#tag-filter-modal").classList.add("open");
    $("#tag-filter-modal").setAttribute("aria-hidden", "false");
}

function sameTagSet(left, right) {
    const leftSet = new Set(left || []);
    const rightSet = new Set(right || []);
    return leftSet.size === rightSet.size && [...leftSet].every((value) => rightSet.has(value));
}

function applyTagFilters() {
    const include = [];
    const exclude = [];
    (state.albumTagStats || []).forEach((tag) => {
        const value = state.tagFilterDraft[String(tag.id)] || "any";
        if (value === "true") {
            include.push(tag.name);
        } else if (value === "false") {
            exclude.push(tag.name);
        }
    });
    const currentInclude = state.tagFilters?.include || [];
    const currentExclude = state.tagFilters?.exclude || [];
    if (sameTagSet(include, currentInclude) && sameTagSet(exclude, currentExclude)) {
        return;
    }
    const url = new URL(window.location.href);
    url.searchParams.delete("include_tag");
    url.searchParams.delete("exclude_tag");
    include.forEach((tagName) => url.searchParams.append("include_tag", tagName));
    exclude.forEach((tagName) => url.searchParams.append("exclude_tag", tagName));
    url.searchParams.set("page", "1");
    window.location.href = url.toString();
}

function closeTagFilter() {
    const modal = $("#tag-filter-modal");
    if (!modal.classList.contains("open")) {
        return;
    }
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    applyTagFilters();
}

function chooseTagFilter(event) {
    const button = event.target.closest("[data-tag-filter-value]");
    if (!button) {
        return;
    }
    state.tagFilterDraft[button.dataset.tagFilterId] = button.dataset.tagFilterValue;
    renderTagFilters();
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
    const done = setBusy($("#scan-button"), "…");
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
            const done = setBusy($("#scan-button"), "…");
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
    $("#filter-button")?.addEventListener("click", openTagFilter);
    $("#scan-button")?.addEventListener("click", scanAlbums);
    $("#scan-status-close")?.addEventListener("click", () => {
        state.scanStatusClosed = true;
        $("#scan-status").hidden = true;
    });
    $("#admin-button")?.addEventListener("click", openAdmin);
    $("#rescan-metadata-button")?.addEventListener("click", rescanCurrentMetadata);
    $("#comfy-generate-button")?.addEventListener("click", openComfyModal);
    $("#delete-photo-button")?.addEventListener("click", deleteCurrentPhoto);
    $("#photo-actions-button")?.addEventListener("click", (event) => {
        event.stopPropagation();
        togglePhotoActionsMenu();
    });
    $("#album-action-form")?.addEventListener("submit", submitAlbumAction);
    $("#comfy-form")?.addEventListener("submit", submitComfyGeneration);
    $("#comfy-references")?.addEventListener("input", (event) => {
        if (event.target.matches("[data-comfy-ref-search]")) {
            debouncedComfyReferenceSearch(event);
        }
    });
    $("#save-photo-tags-button")?.addEventListener("click", savePhotoTags);
    $("#link-search-input")?.addEventListener("input", debounce(searchLinkTargets, 250));
    $("#prev-button")?.addEventListener("click", () => navigate(-1));
    $("#next-button")?.addEventListener("click", () => navigate(1));
    $("#play-button")?.addEventListener("click", toggleSlideshow);
    $("#details-toggle-button")?.addEventListener("click", toggleDetailsPanel);
    $(".viewer-stage")?.addEventListener("touchstart", handleViewerTouchStart, { passive: true });
    $(".viewer-stage")?.addEventListener("touchmove", handleViewerTouchMove, { passive: false });
    $(".viewer-stage")?.addEventListener("touchend", handleViewerTouchEnd, { passive: true });
    $(".viewer-stage")?.addEventListener("touchcancel", handleViewerTouchCancel, { passive: true });
    $$("[data-close-modal]").forEach((button) => button.addEventListener("click", closePhotoModal));
    $$("[data-close-admin]").forEach((button) => button.addEventListener("click", closeAdmin));
    $$("[data-close-comfy]").forEach((button) => button.addEventListener("click", closeComfyModal));
    $$("[data-close-album-action]").forEach((button) => button.addEventListener("click", closeAlbumActionModal));

    $("#gallery-list")?.addEventListener("click", (event) => {
        const button = event.target.closest("[data-photo-id]");
        if (button) {
            openPhoto(Number(button.dataset.photoId)).catch((error) => alert(error.message));
        }
    });

    document.body.addEventListener("click", (event) => {
        if (!event.target.closest(".viewer-menu")) {
            closePhotoActionsMenu();
        }
        const albumAction = event.target.closest("[data-album-action-open]");
        if (albumAction) {
            openAlbumActionModal(albumAction.dataset.albumActionOpen);
        }
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
        const comfyReference = event.target.closest("[data-comfy-ref-result]");
        if (comfyReference) {
            selectComfyReference(comfyReference);
        }
    });

    $("#album-admin-list")?.addEventListener("submit", (event) => {
        if (event.target.matches(".album-admin-card")) {
            saveAlbum(event).catch((error) => alert(error.message));
        }
    });
    $("#tag-filter-list")?.addEventListener("click", chooseTagFilter);
    $$('[data-close-tag-filter]').forEach((button) => button.addEventListener("click", closeTagFilter));

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closePhotoActionsMenu();
            closePhotoModal();
            closeAdmin();
            closeComfyModal();
            closeAlbumActionModal();
            closeTagFilter();
        }
        if ($("#photo-modal").classList.contains("open") && event.key === "ArrowRight") {
            navigate(1);
        }
        if ($("#photo-modal").classList.contains("open") && event.key === "ArrowLeft") {
            navigate(-1);
        }
    });

    window.addEventListener("popstate", () => {
        if ($("#photo-modal").classList.contains("open") || state.photoModalHistoryActive) {
            closePhotoModal({ fromHistory: true });
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
refreshComfyStatus();
