const state = {
    ...window.galleryState,
    currentPhoto: null,
    currentIndex: -1,
    playTimer: null,
    scanPollTimer: null,
    scanStatusClosed: false,
    scanJob: null,
    scanBusyDone: null,
    detailsVisible: window.localStorage.getItem("gallery.detailsVisible") === "true",
    detailsSheetState: window.localStorage.getItem("gallery.detailsVisible") === "true" ? "compact" : "hidden",
    detailsSheetDrag: null,
    detailsSheetSuppressClick: false,
    comfyAvailable: false,
    comfyOptions: null,
    comfyReferences: [],
    comfyReferenceTarget: null,
    comfyReferenceAddOpen: false,
    comfyReferenceDragIndex: null,
    comfyReferencePointerId: null,
    comfyLoraCatalog: [],
    comfyJob: null,
    comfySourcePhotoId: null,
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
    albumActionBatch: false,
    photoModalHistoryActive: false,
    tagFilterDraft: {},
    tagFilterQuery: "",
    tagFacetRequestId: 0,
    tagFacetAbortController: null,
    selectionMode: false,
    selectedPhotoIds: new Set(),
    longPressTimer: null,
    longPressPointerId: null,
    longPressPhotoId: null,
    longPressStartX: 0,
    longPressStartY: 0,
    suppressPhotoClickId: null,
    faceIdentities: [],
    faceStatus: null,
    faceJobPollTimer: null,
    faceJobPollDone: null,
    faceJobStatusClosed: false,
    faceImport: null,
    configSection: "albums",
    loraTagMappings: [],
    loraTagCatalog: [],
    loraTagEditingId: null,
    tagSensitivities: [],
    tagSensitivityQuery: "",
    linkedStripIdleTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const MOBILE_DETAILS_MEDIA = window.matchMedia("(max-width: 760px)");
const DETAILS_SHEET_STATES = ["hidden", "compact", "expanded"];
const DETAILS_SHEET_SNAP_DISTANCE = 56;
const DETAILS_SHEET_SNAP_VELOCITY = 0.45;

function tagsFromInput(value) {
    return value.split(",").map((tag) => tag.trim()).filter(Boolean);
}

function renderTags(tags) {
    if (!tags || !tags.length) {
        return '<span class="muted">Aucun</span>';
    }
    return tags.map((tag) => {
        const item = typeof tag === "string" ? { name: tag, category: null } : tag;
        return `
            <span class="tag">
                ${tagCategoryIcon(item.category)}
                <span>${escapeHtml(item.name)}</span>
            </span>
        `;
    }).join("");
}

const TAG_CATEGORY_LABELS = {
    clothing: "Vêtement",
    person: "Personne",
    constraint: "Contrainte",
};

function tagCategoryIcon(category) {
    const label = TAG_CATEGORY_LABELS[category];
    if (!label) {
        return "";
    }
    const paths = category === "clothing"
        ? '<path d="M8 5 5 7l-2 5 4 2v7h10v-7l4-2-2-5-3-2c-.7 1.3-2.1 2-4 2s-3.3-.7-4-2Z"></path>'
        : category === "person"
            ? '<circle cx="12" cy="8" r="4"></circle><path d="M4.5 21c.7-5 3.2-7 7.5-7s6.8 2 7.5 7"></path>'
            : '<path d="M9.5 14.5 7 17a3.5 3.5 0 0 1-5-5l3-3a3.5 3.5 0 0 1 5 0"></path><path d="m14.5 9.5 2.5-2.5a3.5 3.5 0 0 1 5 5l-3 3a3.5 3.5 0 0 1-5 0"></path><path d="m8.5 15.5 7-7"></path>';
    return `
        <span class="tag-category-icon is-${category}" title="${label}" aria-label="${label}">
            <svg viewBox="0 0 24 24" aria-hidden="true">${paths}</svg>
        </span>
    `;
}

function sensitivitySelectorHtml(selected, attributes = "") {
    const levels = state.sensitivityLevels || ["neutral", "low", "medium", "high"];
    return `
        <span class="sensitivity-selector" role="radiogroup" ${attributes}>
            ${levels.map((level) => `
                <button type="button" role="radio"
                        class="sensitivity-step sensitivity-${level}${selected === level ? " is-selected" : ""}"
                        data-sensitivity-value="${level}"
                        aria-checked="${selected === level}"
                        title="${level}">${level.slice(0, 1).toUpperCase()}</button>
            `).join("")}
        </span>
    `;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

const LONG_PRESS_DELAY = 500;
const LONG_PRESS_MOVE_TOLERANCE = 10;
const LINKED_STRIP_IDLE_DELAY = 3000;
const SCAN_OPTIONS_STORAGE_KEY = "gallery.scanOptions.v1";

function selectedPhotoIds() {
    return Array.from(state.selectedPhotoIds);
}

function setSelectionStatus(message, isError = false) {
    const status = $("#selection-status");
    if (!status) {
        return;
    }
    status.textContent = message || "";
    status.hidden = !message;
    status.classList.toggle("error", Boolean(isError));
}

function closeSelectionActionsMenu() {
    const menu = $("#selection-actions-menu");
    const button = $("#selection-actions-button");
    if (menu) {
        menu.hidden = true;
    }
    if (button) {
        button.setAttribute("aria-expanded", "false");
    }
}

function toggleSelectionActionsMenu() {
    const menu = $("#selection-actions-menu");
    const button = $("#selection-actions-button");
    if (!menu || !button || !state.selectedPhotoIds.size) {
        return;
    }
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    button.setAttribute("aria-expanded", willOpen ? "true" : "false");
}

function renderSelectionState() {
    const gallery = $("#gallery-list");
    gallery?.classList.toggle("selection-mode", state.selectionMode);
    const actions = $("#selection-actions");
    if (actions) {
        actions.hidden = !state.selectionMode;
    }
    const count = $("#selection-count");
    if (count) {
        count.textContent = String(state.selectedPhotoIds.size);
    }
    const actionsButton = $("#selection-actions-button");
    if (actionsButton) {
        actionsButton.disabled = state.selectedPhotoIds.size === 0;
    }
    updateScanScopeControls();
    $$("[data-gallery-photo-id]").forEach((item) => {
        const photoId = Number(item.dataset.galleryPhotoId);
        const selected = state.selectedPhotoIds.has(photoId);
        item.classList.toggle("is-selected", selected);
        const checkbox = item.querySelector("[data-select-photo-id]");
        if (checkbox) {
            checkbox.checked = selected;
        }
    });
    if (!state.selectionMode) {
        closeSelectionActionsMenu();
    }
}

function enterSelectionMode(photoId) {
    state.selectionMode = true;
    state.selectedPhotoIds.add(photoId);
    setSelectionStatus("");
    renderSelectionState();
}

function exitSelectionMode() {
    state.selectionMode = false;
    state.selectedPhotoIds.clear();
    setSelectionStatus("");
    closeBatchTagModal();
    renderSelectionState();
}

function togglePhotoSelection(photoId) {
    if (!state.selectionMode) {
        enterSelectionMode(photoId);
        return;
    }
    if (state.selectedPhotoIds.has(photoId)) {
        state.selectedPhotoIds.delete(photoId);
        if (!state.selectedPhotoIds.size) {
            exitSelectionMode();
            return;
        }
    } else {
        state.selectedPhotoIds.add(photoId);
    }
    renderSelectionState();
}

function clearLongPress() {
    window.clearTimeout(state.longPressTimer);
    state.longPressTimer = null;
    state.longPressPointerId = null;
    state.longPressPhotoId = null;
}

function handleGalleryPointerDown(event) {
    const button = event.target.closest("[data-photo-id]");
    if (!button || state.selectionMode || event.button !== 0 || !event.isPrimary) {
        return;
    }
    clearLongPress();
    state.longPressPointerId = event.pointerId;
    state.longPressPhotoId = Number(button.dataset.photoId);
    state.longPressStartX = event.clientX;
    state.longPressStartY = event.clientY;
    state.longPressTimer = window.setTimeout(() => {
        const photoId = state.longPressPhotoId;
        state.suppressPhotoClickId = photoId;
        window.setTimeout(() => {
            if (state.suppressPhotoClickId === photoId) {
                state.suppressPhotoClickId = null;
            }
        }, 1000);
        enterSelectionMode(photoId);
        clearLongPress();
    }, LONG_PRESS_DELAY);
}

function handleGalleryPointerMove(event) {
    if (event.pointerId !== state.longPressPointerId || !state.longPressTimer) {
        return;
    }
    const moved = Math.hypot(event.clientX - state.longPressStartX, event.clientY - state.longPressStartY);
    if (moved > LONG_PRESS_MOVE_TOLERANCE) {
        clearLongPress();
    }
}

function handleGalleryPointerEnd(event) {
    if (event.pointerId === state.longPressPointerId) {
        clearLongPress();
    }
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
        const requestError = new Error(data.error || "Erreur serveur");
        requestError.status = response.status;
        requestError.data = data;
        throw requestError;
    }
    return data;
}

function setBusy(button, busyText, options = {}) {
    if (!button) {
        return () => {};
    }
    const previous = button.innerHTML;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "";
    if (options.spinner) {
        const spinner = document.createElement("span");
        spinner.className = "button-spinner";
        spinner.setAttribute("aria-hidden", "true");
        button.append(spinner);
    }
    button.append(document.createTextNode(busyText));
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
    clearLinkedStripIdleTimer();
    $("#linked-strip")?.classList.remove("is-hidden");
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    state.photoModalHistoryActive = false;
    if (state.detailsSheetState === "expanded") {
        setDetailsSheetState("compact", { persist: false });
    }
    state.detailsSheetDrag = null;
    $("#details-panel")?.style.removeProperty("top");
    $(".photo-viewer")?.classList.remove("details-sheet-dragging");
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
    $("#photo-tags-input").value = photo.tags
        .filter((tag) => tag.source === "manual")
        .map((tag) => tag.name)
        .join(", ");
    renderImageAnalysis(photo);
    renderPhotoFaces(photo.face_analysis);
    renderLinks(photo.links);
    renderLinkedStrip(photo.links);
    refreshComfyStatus();
}

function renderImageAnalysis(photo) {
    const badge = $("#detail-effective-sensitivity");
    const content = $("#detail-image-analysis-content");
    if (!badge || !content) {
        return;
    }
    const effective = photo.effective_sensitivity || "neutral";
    badge.textContent = effective;
    badge.dataset.sensitivity = effective;
    const analysis = photo.image_analysis;
    if (!analysis?.scanned) {
        content.innerHTML = '<p class="muted">Photo non analysée. Le niveau effectif provient uniquement de ses tags.</p>';
        return;
    }
    const scores = Object.entries(analysis.freepik_scores || {})
        .map(([level, score]) => `<li><span>${escapeHtml(level)}</span><strong>${(Number(score) * 100).toFixed(1)}%</strong></li>`)
        .join("");
    const detections = (analysis.nudenet_detections || [])
        .filter((item) => Number(item.score) >= 0.60)
        .map((item) => `<li>${escapeHtml(item.class)} <strong>${(Number(item.score) * 100).toFixed(1)}%</strong></li>`)
        .join("");
    const automaticTags = (analysis.automatic_tags || [])
        .map((item) => `<span class="tag" title="${(Number(item.score) * 100).toFixed(1)}%">${escapeHtml(item.display_name || item.name)}</span>`)
        .join("");
    content.innerHTML = `
        <p>Niveau modèles : <strong>${escapeHtml(analysis.analysis_level)}</strong>
           · Freepik : <strong>${escapeHtml(analysis.freepik_level)}</strong></p>
        <div class="analysis-detail-grid">
            <div><h4>Scores Freepik</h4><ul>${scores || "<li>Aucun</li>"}</ul></div>
            <div><h4>Détections NudeNet</h4><ul>${detections || "<li>Aucune ≥ 60%</li>"}</ul></div>
        </div>
        <h4>Tags automatiques</h4>
        <div class="tag-row">${automaticTags || '<span class="muted">Aucun</span>'}</div>
    `;
}

function renderPhotoFaces(analysis) {
    const container = $("#detail-faces");
    if (!container) {
        return;
    }
    if (!analysis?.scanned) {
        container.innerHTML = '<span class="muted">Photo non analysee.</span>';
        return;
    }
    if (!analysis.faces?.length) {
        container.innerHTML = '<span class="muted">Aucun visage detecte.</span>';
        return;
    }
    const identityOptions = state.faceIdentities.map((identity) =>
        `<option value="${identity.id}">${escapeHtml(identity.tag_name)}</option>`
    ).join("");
    container.innerHTML = analysis.faces.map((face) => {
        const match = face.match;
        const score = match ? Number(match.score).toFixed(3) : null;
        const referencePanelId = `face-reference-panel-${face.id}`;
        const stateLabel = match?.state === "automatic" ? "Tag automatique"
            : match?.state === "pending" ? "A confirmer"
            : match?.state === "confirmed" ? "Confirme"
            : match?.state === "rejected" ? "Rejete"
            : "Inconnu";
        return `
            <article class="face-detail-card" data-face-id="${face.id}">
                <img src="${face.crop_url}" alt="Visage ${face.face_index + 1}">
                <div class="face-detail-content">
                    <strong>${match ? escapeHtml(match.tag_name) : "Visage inconnu"}</strong>
                    <small class="face-match-state is-${escapeHtml(match?.state || "unknown")}">
                        ${stateLabel}${score ? ` · ${score}` : ""}
                    </small>
                    <div class="face-card-actions">
                        ${match && match.state !== "rejected" && match.state !== "confirmed" ? `
                            <button class="face-action-button face-action-confirm" type="button"
                                    data-face-decision="confirmed" data-identity-id="${match.identity_id}"
                                    title="Confirmer" aria-label="Confirmer ${escapeHtml(match.tag_name)}">
                                <span aria-hidden="true">&#10003;</span>
                            </button>
                        ` : ""}
                        ${match && match.state !== "rejected" ? `
                            <button class="face-action-button face-action-reject" type="button"
                                    data-face-decision="rejected" data-identity-id="${match.identity_id}"
                                    title="Rejeter" aria-label="Rejeter ${escapeHtml(match.tag_name)}">
                                <span aria-hidden="true">&#10005;</span>
                            </button>
                        ` : ""}
                        <button class="face-action-button face-reference-toggle" type="button"
                                data-toggle-face-references aria-expanded="false" aria-controls="${referencePanelId}"
                                title="Afficher les references" aria-label="Afficher les references">
                            <span class="face-reference-chevron" aria-hidden="true">&#9662;</span>
                        </button>
                    </div>
                    <div id="${referencePanelId}" class="face-reference-panel" hidden>
                        <div class="face-reference-action">
                            <select data-face-reference-identity aria-label="Identite de reference" ${identityOptions ? "" : "disabled"}>${identityOptions || '<option>Aucune identite</option>'}</select>
                            <button type="button" data-add-gallery-reference ${identityOptions ? "" : "disabled"}>Reference</button>
                        </div>
                    </div>
                </div>
            </article>
        `;
    }).join("");
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
        <li class="gallery-item" data-gallery-photo-id="${photo.id}">
            <button class="thumbnail" type="button" data-photo-id="${photo.id}">
                <img src="${photo.thumbnail_url}" alt="${escapeHtml(photo.filename)}" loading="lazy">
                ${photo.favorite ? `
                    <span class="favorite-badge" title="Present dans output et dans un album user">
                        *${photo.user_album_count > 1 ? `<small>${photo.user_album_count}</small>` : ""}
                    </span>
                ` : ""}
                <span class="filename">${escapeHtml(photo.filename)}</span>
            </button>
            <input class="selection-checkbox" type="checkbox" data-select-photo-id="${photo.id}"
                   aria-label="Selectionner ${escapeHtml(photo.filename)}">
        </li>
    `;
}

function applyDetailsVisibility() {
    const viewer = document.querySelector(".photo-viewer");
    const button = $("#details-toggle-button");
    const handle = $("#details-sheet-handle");
    const surface = $(".details-sheet-surface");
    if (!viewer) {
        return;
    }

    const mobile = MOBILE_DETAILS_MEDIA.matches;
    const sheetState = mobile
        ? state.detailsSheetState
        : state.detailsVisible ? "compact" : "hidden";
    const visible = sheetState !== "hidden";
    const expanded = mobile && sheetState === "expanded";

    viewer.classList.toggle("details-open", visible);
    viewer.classList.toggle("details-sheet-expanded", expanded);
    if (button) {
        button.setAttribute("aria-pressed", visible ? "true" : "false");
        button.title = visible ? "Masquer les détails" : "Afficher les détails";
    }
    if (handle) {
        const labels = {
            hidden: "Afficher les détails",
            compact: "Masquer les détails. Balayer vers le haut pour agrandir",
            expanded: "Masquer les détails. Balayer vers le bas pour réduire",
        };
        handle.dataset.sheetState = sheetState;
        handle.setAttribute("aria-expanded", visible ? "true" : "false");
        handle.setAttribute("aria-label", labels[sheetState]);
        handle.title = labels[sheetState];
    }
    if (surface) {
        const hiddenOnMobile = mobile && !visible;
        surface.toggleAttribute("inert", hiddenOnMobile);
        surface.setAttribute("aria-hidden", hiddenOnMobile ? "true" : "false");
    }
}

function setDetailsSheetState(nextState, options = {}) {
    if (!DETAILS_SHEET_STATES.includes(nextState)) {
        return;
    }
    const visible = nextState !== "hidden";
    state.detailsSheetState = nextState;
    state.detailsVisible = visible;
    if (options.persist !== false) {
        window.localStorage.setItem("gallery.detailsVisible", visible ? "true" : "false");
    }
    applyDetailsVisibility();
}

function toggleDetailsPanel() {
    if (MOBILE_DETAILS_MEDIA.matches) {
        setDetailsSheetState(state.detailsSheetState === "hidden" ? "compact" : "hidden");
        return;
    }
    state.detailsVisible = !state.detailsVisible;
    state.detailsSheetState = state.detailsVisible ? "compact" : "hidden";
    window.localStorage.setItem("gallery.detailsVisible", state.detailsVisible ? "true" : "false");
    applyDetailsVisibility();
}

function stepDetailsSheet(direction) {
    if (!MOBILE_DETAILS_MEDIA.matches) {
        return;
    }
    const currentIndex = DETAILS_SHEET_STATES.indexOf(state.detailsSheetState);
    const nextIndex = Math.max(0, Math.min(DETAILS_SHEET_STATES.length - 1, currentIndex + direction));
    setDetailsSheetState(DETAILS_SHEET_STATES[nextIndex]);
}

function detailsSheetOffsets() {
    const viewportHeight = $(".photo-viewer")?.clientHeight
        || window.visualViewport?.height
        || window.innerHeight;
    const handleHeight = $("#details-sheet-handle")?.offsetHeight || 58;
    return {
        expanded: viewportHeight * 0.15,
        compact: viewportHeight * 0.58,
        hidden: Math.max(0, viewportHeight - handleHeight),
    };
}

function handleDetailsSheetPointerDown(event) {
    if (
        !MOBILE_DETAILS_MEDIA.matches
        || !$("#photo-modal")?.classList.contains("open")
        || event.button !== 0
        || (event.currentTarget.matches(".details-sheet-header")
            && event.target.closest("button, input, select, textarea, a"))
    ) {
        return;
    }

    const panel = $("#details-panel");
    const viewer = $(".photo-viewer");
    if (!panel || !viewer) {
        return;
    }

    state.detailsSheetDrag = {
        pointerId: event.pointerId,
        owner: event.currentTarget,
        startY: event.clientY,
        startAt: Date.now(),
        startOffset: panel.offsetTop,
        offsets: detailsSheetOffsets(),
        dragged: false,
    };
    state.detailsSheetSuppressClick = false;
    viewer.classList.add("details-sheet-dragging");
    event.currentTarget.setPointerCapture?.(event.pointerId);
}

function handleDetailsSheetPointerMove(event) {
    const drag = state.detailsSheetDrag;
    const panel = $("#details-panel");
    if (!drag || drag.pointerId !== event.pointerId || !panel) {
        return;
    }

    const deltaY = event.clientY - drag.startY;
    if (Math.abs(deltaY) > 4) {
        drag.dragged = true;
    }
    const offset = Math.max(
        drag.offsets.expanded,
        Math.min(drag.offsets.hidden, drag.startOffset + deltaY),
    );
    panel.style.top = `${offset}px`;
    event.preventDefault();
}

function finishDetailsSheetPointer(event, cancelled = false) {
    const drag = state.detailsSheetDrag;
    if (!drag || drag.pointerId !== event.pointerId) {
        return;
    }

    const panel = $("#details-panel");
    const viewer = $(".photo-viewer");
    const deltaY = event.clientY - drag.startY;
    const elapsed = Math.max(1, Date.now() - drag.startAt);
    const velocity = deltaY / elapsed;
    let direction = 0;
    if (!cancelled && (
        Math.abs(deltaY) >= DETAILS_SHEET_SNAP_DISTANCE
        || Math.abs(velocity) >= DETAILS_SHEET_SNAP_VELOCITY
    )) {
        direction = deltaY < 0 ? 1 : -1;
    }

    state.detailsSheetSuppressClick = drag.dragged && drag.owner.matches(".details-sheet-handle");
    try {
        drag.owner.releasePointerCapture?.(drag.pointerId);
    } catch (_error) {
        // The pointer may already have been released by the browser.
    }
    state.detailsSheetDrag = null;

    const currentIndex = DETAILS_SHEET_STATES.indexOf(state.detailsSheetState);
    const nextIndex = Math.max(0, Math.min(DETAILS_SHEET_STATES.length - 1, currentIndex + direction));
    setDetailsSheetState(DETAILS_SHEET_STATES[nextIndex]);
    viewer?.classList.remove("details-sheet-dragging");
    window.requestAnimationFrame(() => panel?.style.removeProperty("top"));
}

function handleDetailsSheetClick(event) {
    if (state.detailsSheetSuppressClick) {
        state.detailsSheetSuppressClick = false;
        event.preventDefault();
        return;
    }
    toggleDetailsPanel();
}

function handleDetailsSheetKeydown(event) {
    if (event.key === "ArrowUp") {
        event.preventDefault();
        stepDetailsSheet(1);
    } else if (event.key === "ArrowDown") {
        event.preventDefault();
        stepDetailsSheet(-1);
    }
}

function handleDetailsSheetBreakpointChange(event) {
    state.detailsSheetDrag = null;
    $("#details-panel")?.style.removeProperty("top");
    $(".photo-viewer")?.classList.remove("details-sheet-dragging");
    if (event.matches) {
        state.detailsSheetState = state.detailsVisible ? "compact" : "hidden";
    } else if (state.detailsSheetState === "expanded") {
        state.detailsSheetState = "compact";
    }
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
        clearLinkedStripIdleTimer();
        strip.classList.remove("is-hidden");
        strip.innerHTML = "";
        return;
    }
    strip.innerHTML = links.map((link) => `
        <button type="button"
                class="linked-image--${link.type === "original" ? "origin" : "variant"}"
                data-open-linked="${link.linked_photo_id}"
                title="${escapeHtml(link.type)}">
            <img src="${link.thumbnail_url}" alt="${escapeHtml(link.filename)}">
        </button>
    `).join("");
    showLinkedStripTemporarily();
}

function clearLinkedStripIdleTimer() {
    if (state.linkedStripIdleTimer !== null) {
        window.clearTimeout(state.linkedStripIdleTimer);
        state.linkedStripIdleTimer = null;
    }
}

function showLinkedStripTemporarily() {
    const strip = $("#linked-strip");
    clearLinkedStripIdleTimer();
    if (!strip || !strip.children.length) {
        return;
    }
    strip.classList.remove("is-hidden");
    state.linkedStripIdleTimer = window.setTimeout(() => {
        strip.classList.add("is-hidden");
        state.linkedStripIdleTimer = null;
    }, LINKED_STRIP_IDLE_DELAY);
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

async function rescanCurrentImageAnalysis() {
    if (!state.currentPhoto) {
        return;
    }
    const photoId = state.currentPhoto.id;
    const done = setBusy($("#rescan-image-analysis-button"), "Analyse...", { spinner: true });
    try {
        const data = await fetchJson(`/api/photos/${photoId}/image-analysis/rescan`, {
            method: "POST",
            body: "{}",
        });
        syncPhotoInCurrentGallery(data.photo);
        if (state.currentPhoto?.id === photoId) {
            state.currentPhoto = data.photo;
            renderPhotoDetail(data.photo);
            const analysisDetail = $("#detail-image-analysis");
            if (analysisDetail) {
                analysisDetail.open = true;
            }
        }
    } catch (error) {
        alert(error.message);
    } finally {
        done();
    }
}

async function refreshCurrentThumbnail() {
    if (!state.currentPhoto) {
        return;
    }
    closePhotoActionsMenu();
    const photoId = state.currentPhoto.id;
    const done = setBusy($("#refresh-thumbnail-button"), "...");
    try {
        const data = await fetchJson(`/api/photos/${photoId}/thumbnail/refresh`, { method: "POST", body: "{}" });
        state.currentPhoto = data.photo;
        const galleryPhoto = state.photos.find((photo) => photo.id === photoId);
        if (galleryPhoto) {
            galleryPhoto.thumbnail_url = data.photo.thumbnail_url;
        }
        const thumbnail = document.querySelector(`[data-gallery-photo-id="${photoId}"] .thumbnail img`);
        if (thumbnail) {
            thumbnail.src = data.photo.thumbnail_url;
        }
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
    const jobActive = Boolean(state.comfyJob?.active);
    button.disabled = !state.comfyAvailable || !state.currentPhoto || jobActive;
    button.title = jobActive
        ? "Une generation est deja en cours"
        : state.comfyAvailable ? "Regenerer avec ComfyUI" : "ComfyUI indisponible";
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
    state.albumActionBatch = false;
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

function openBatchAlbumActionModal() {
    if (!state.selectedPhotoIds.size || !state.selectedAlbum) {
        return;
    }
    closeSelectionActionsMenu();
    state.albumActionBatch = true;
    state.albumActionMode = "copy";
    state.albumActionSource = { album_name: state.selectedAlbum.name };
    $("#album-action-title").textContent = `Ajouter ${state.selectedPhotoIds.size} photo(s) a l'album`;
    $("#album-action-submit").textContent = "Ajouter";
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
    state.albumActionBatch = false;
}

function renderAlbumActionOptions() {
    const select = $("#album-action-destination");
    const membershipNames = new Set((state.currentPhoto?.memberships || []).map((item) => item.album_name));
    const sourceName = state.albumActionSource?.album_name;
    const albums = state.albums.filter((album) => {
        if (album.available === false) {
            return false;
        }
        if (state.albumActionBatch) {
            return album.name !== sourceName;
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

function applyBatchMembershipResults(results) {
    results.forEach((result) => {
        if (result.status === "failed") {
            return;
        }
        const photo = state.photos.find((item) => item.id === result.photo_id);
        if (!photo) {
            return;
        }
        photo.favorite = Boolean(result.favorite);
        photo.album_count = result.album_count;
        photo.user_album_count = result.user_album_count;
        const item = document.querySelector(`[data-gallery-photo-id="${result.photo_id}"]`);
        if (item) {
            item.outerHTML = renderGalleryItem(photo);
        }
    });
    renderSelectionState();
}

function batchFailureSuffix(results) {
    const failures = results.filter((result) => result.status === "failed");
    if (!failures.length) {
        return "";
    }
    const details = failures.slice(0, 3).map((result) => `#${result.photo_id}: ${result.error}`).join(" | ");
    return ` ${details}${failures.length > 3 ? " | ..." : ""}`;
}

async function submitAlbumAction(event) {
    event.preventDefault();
    if (!state.albumActionMode || (!state.albumActionBatch && !state.currentPhoto)) {
        return;
    }
    const done = setBusy($("#album-action-submit"), state.albumActionBatch ? "Ajout..." : state.albumActionMode === "move" ? "Deplacement..." : "Copie...");
    $("#album-action-status").textContent = "";
    $("#album-action-status").classList.remove("error");
    try {
        if (state.albumActionBatch) {
            const data = await fetchJson("/api/photos/batch/album-copy", {
                method: "POST",
                body: JSON.stringify({
                    photo_ids: selectedPhotoIds(),
                    destination_album_name: $("#album-action-destination").value,
                    source_album_name: state.selectedAlbum?.name,
                }),
            });
            state.albums = data.albums || state.albums;
            applyBatchMembershipResults(data.results || []);
            const summary = data.summary;
            setSelectionStatus(
                `${summary.copied} copiee(s), ${summary.skipped} deja presente(s), ${summary.failed} erreur(s).${batchFailureSuffix(data.results || [])}`,
                summary.failed > 0,
            );
            closeAlbumActionModal();
            return;
        }
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
    if (state.comfyJob?.active) {
        await reopenComfyJob();
        return;
    }
    if (!state.currentPhoto) {
        return;
    }
    const done = setBusy($("#comfy-generate-button"), "Chargement JSON...", { spinner: true });
    try {
        state.comfySourcePhotoId = state.currentPhoto.id;
        await openComfyModalForPhoto(state.currentPhoto.id, { reset: true });
    } finally {
        done();
        updateComfyButton();
        closePhotoActionsMenu();
    }
}

async function openComfyModalForPhoto(photoId, options = {}) {
    const modal = $("#comfy-modal");
    setComfyStatus("Chargement des options...");
    if (options.reset) {
        state.comfyOptions = null;
        state.comfyReferences = [];
        state.comfyReferenceTarget = null;
        updateComfyFormJobControls(state.comfyJob);
    }
    try {
        const data = await fetchJson(`/api/photos/${photoId}/comfy/edit-options`);
        state.comfySourcePhotoId = photoId;
        state.comfyOptions = data.options;
        renderComfyOptions(data.options);
        setComfyStatus("");
    } catch (error) {
        setComfyStatus(error.message, true);
    }
    updateComfyFormJobControls(state.comfyJob);
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
}

async function reopenComfyJob() {
    const job = state.comfyJob;
    if (!job?.active) {
        return;
    }
    closePhotoActionsMenu();
    const needsOptions = state.comfySourcePhotoId !== job.photo_id || !state.comfyOptions;
    if (needsOptions) {
        await openComfyModalForPhoto(job.photo_id, { reset: true });
    } else {
        const modal = $("#comfy-modal");
        modal.classList.add("open");
        modal.setAttribute("aria-hidden", "false");
    }
    renderComfyJob(job);
    if (!state.comfyJobPollTimer) {
        startComfyJobPolling(job.id);
    }
}

function closeComfyModal() {
    $("#comfy-modal").classList.remove("open");
    $("#comfy-modal").setAttribute("aria-hidden", "true");
    const closeOnFinish = $("#comfy-close-on-finish");
    if (closeOnFinish) {
        closeOnFinish.checked = true;
    }
    setComfyReferenceAddOpen(false);
}

function setComfyStatus(message, isError = false) {
    const status = $("#comfy-status");
    status.textContent = message || "";
    status.classList.toggle("error", Boolean(isError));
}

function updateComfyFormJobControls(job) {
    const active = Boolean(job?.active);
    const cancelRequested = job?.state === "cancel_requested";
    $$("#comfy-form input, #comfy-form textarea, #comfy-form select, #comfy-form button").forEach((control) => {
        if (
            control.id !== "comfy-submit-button"
            && control.id !== "comfy-cancel-button"
            && control.id !== "comfy-close-on-finish"
        ) {
            control.disabled = active;
        }
    });
    const submit = $("#comfy-submit-button");
    if (submit) {
        submit.disabled = active || !state.comfyOptions;
        submit.textContent = active ? "Generation..." : "Lancer";
        submit.toggleAttribute("aria-busy", active);
    }
    const cancel = $("#comfy-cancel-button");
    if (cancel) {
        cancel.hidden = !active;
        cancel.disabled = !active || cancelRequested;
        cancel.textContent = cancelRequested ? "Annulation..." : "Annuler la generation";
    }
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
    state.comfyLoraCatalog = options.lora_catalog || [];
    state.comfyReferenceAddOpen = false;
    state.comfyReferenceTarget = null;
    state.comfyReferences = (options.references || []).map((reference) => ({
        ...reference,
        input_name: reference.image_name,
        photo_id: null,
        is_new: false,
    }));
    renderComfyLoras(options.loras || [], state.comfyLoraCatalog);
    renderComfyReferences();
}

function formatComfyLoraStrength(value) {
    const strength = Number(value);
    return Number.isFinite(strength) ? strength.toFixed(2) : "0.00";
}

function renderComfyLoras(loras, catalog) {
    const container = $("#comfy-loras");
    const rows = loras.map((lora) => {
        const names = catalog.map((item) => item.lora_name);
        if (!names.includes(lora.lora_name)) {
            names.unshift(lora.lora_name);
        }
        return `
            <div class="comfy-row comfy-lora-row" data-node-id="${escapeHtml(lora.node_id || "")}" data-new-lora="${lora.new ? "true" : "false"}">
                <label class="checkbox-field">
                    <input type="checkbox" data-comfy-lora-enabled aria-label="Activer le LoRA ${escapeHtml(lora.new ? "nouveau" : lora.node_id)}" ${lora.enabled ? "checked" : ""}>
                    <span>${lora.new ? "New" : escapeHtml(lora.node_id)}</span>
                </label>
                <select data-comfy-lora-name>
                    ${names.map((name) => `<option value="${escapeHtml(name)}" ${name === lora.lora_name ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}
                </select>
                <input type="number" data-comfy-lora-strength step="0.01" inputmode="decimal" value="${escapeHtml(formatComfyLoraStrength(lora.strength_model ?? 1))}" aria-label="Force LoRA">
                ${lora.new ? '<button type="button" class="comfy-row-remove" data-remove-comfy-lora title="Retirer">&times;</button>' : ""}
            </div>
        `;
    }).join("");
    container.innerHTML = `
        <div data-comfy-lora-rows>${rows || '<span class="muted">Aucun LoRA dans ce workflow</span>'}</div>
        <button type="button" data-add-comfy-lora ${catalog.length ? "" : "disabled"}>Ajouter un LoRA</button>
        ${catalog.length ? "" : '<small class="muted">Aucun LoRA disponible dans l’historique de la galerie.</small>'}
    `;
}

function addComfyLora() {
    const name = state.comfyLoraCatalog[0]?.lora_name;
    if (!name) {
        return;
    }
    const existing = $$(".comfy-lora-row").map((row) => ({
        node_id: row.dataset.nodeId || null,
        new: row.dataset.newLora === "true",
        enabled: row.querySelector("[data-comfy-lora-enabled]").checked,
        lora_name: row.querySelector("[data-comfy-lora-name]").value,
        strength_model: Number(row.querySelector("[data-comfy-lora-strength]").value || 0),
    }));
    existing.push({ node_id: null, new: true, enabled: true, lora_name: name, strength_model: 1.0 });
    renderComfyLoras(existing, state.comfyLoraCatalog);
}

function renderComfyReferences() {
    const container = $("#comfy-references");
    const cards = state.comfyReferences.map((reference, index) => `
        <article class="comfy-reference-thumb ${reference.enabled ? "" : "disabled"}" draggable="true" data-comfy-reference-index="${index}">
            <button type="button" class="comfy-reference-drag" data-comfy-ref-drag title="Réordonner" aria-label="Réordonner">&#x2630;</button>
            <img src="${escapeHtml(reference.thumbnail_url || `/api/comfy/input-preview?filename=${encodeURIComponent(reference.input_name || reference.image_name || "")}`)}" alt="${escapeHtml(reference.image_name || "Référence")}">
            <span class="comfy-reference-main">${index === 0 ? "Main" : `Ref ${index + 1}`}</span>
            <label class="comfy-reference-enabled">
                <input type="checkbox" data-comfy-ref-enabled ${reference.enabled ? "checked" : ""}>
                <span>Active</span>
            </label>
            <small title="${escapeHtml(reference.image_name || "")}">${escapeHtml(reference.image_name || reference.input_name || "Référence")}</small>
            <button type="button" data-comfy-ref-change>Changer</button>
            ${reference.is_new ? '<button type="button" class="danger" data-comfy-ref-remove>Retirer</button>' : ""}
        </article>
    `).join("");
    container.innerHTML = `
        <div class="comfy-reference-strip" data-comfy-reference-strip>
            ${cards || '<span class="muted">Aucune référence Qwen détectée</span>'}
            <button type="button" class="comfy-reference-add-button" data-comfy-ref-add aria-label="Ajouter une référence" aria-controls="comfy-reference-add-panel" aria-expanded="${state.comfyReferenceAddOpen ? "true" : "false"}">+</button>
        </div>
        <div class="comfy-reference-add" id="comfy-reference-add-panel" ${state.comfyReferenceAddOpen ? "" : "hidden"}>
            <div class="comfy-reference-add-header">
                <strong data-comfy-ref-add-title>Ajouter une référence</strong>
            </div>
            <input type="search" data-comfy-ref-search placeholder="Nom d’une image dans les galeries">
            <label class="comfy-reference-upload">
                <span>Ou envoyer un fichier</span>
                <input type="file" data-comfy-ref-upload accept="image/jpeg,image/png,image/webp">
            </label>
            <div class="search-results" data-comfy-ref-results></div>
        </div>
    `;
    updateComfyReferenceAddTitle();
}

function setComfyReferenceAddOpen(open, target = null) {
    state.comfyReferenceAddOpen = Boolean(open);
    state.comfyReferenceTarget = state.comfyReferenceAddOpen ? target : null;
    const panel = $("#comfy-reference-add-panel");
    const button = $("#comfy-references [data-comfy-ref-add]");
    if (panel) {
        panel.hidden = !state.comfyReferenceAddOpen;
    }
    if (button) {
        button.setAttribute("aria-expanded", state.comfyReferenceAddOpen ? "true" : "false");
    }
    updateComfyReferenceAddTitle();
    if (state.comfyReferenceAddOpen) {
        $("#comfy-references [data-comfy-ref-search]")?.focus();
    }
}

async function searchComfyReferences(event) {
    const input = event.target;
    const results = $("#comfy-references [data-comfy-ref-results]");
    const query = input.value.trim();
    if (query.length < 2) {
        results.innerHTML = "";
        return;
    }
    const data = await fetchJson(`/api/photos/search?q=${encodeURIComponent(query)}`);
    results.innerHTML = data.photos
        .filter((photo) => !state.currentPhoto || photo.id !== state.currentPhoto.id)
        .map((photo) => `
            <button type="button" class="search-result" data-comfy-ref-result="${photo.id}" data-filename="${escapeHtml(photo.filename)}" data-thumbnail-url="${escapeHtml(photo.thumbnail_url)}">
                <img src="${photo.thumbnail_url}" alt="">
                <span>${escapeHtml(photo.filename)}<small>${escapeHtml(photo.album_name)}</small></span>
            </button>
        `).join("");
}

const debouncedComfyReferenceSearch = debounce(searchComfyReferences, 250);

function selectComfyReference(button) {
    const replacement = {
        reference_id: null,
        enabled: true,
        image_name: button.dataset.filename,
        input_name: null,
        photo_id: Number(button.dataset.comfyRefResult),
        thumbnail_url: button.dataset.thumbnailUrl,
        is_new: true,
    };
    if (state.comfyReferenceTarget === null) {
        state.comfyReferences.push(replacement);
    } else {
        const current = state.comfyReferences[state.comfyReferenceTarget];
        state.comfyReferences[state.comfyReferenceTarget] = { ...current, ...replacement, reference_id: current.reference_id, is_new: current.is_new };
    }
    state.comfyReferenceAddOpen = false;
    state.comfyReferenceTarget = null;
    renderComfyReferences();
}

function updateComfyReferenceAddTitle() {
    const title = $("#comfy-references [data-comfy-ref-add-title]");
    if (title) {
        title.textContent = state.comfyReferenceTarget === null ? "Ajouter une référence" : `Remplacer la référence ${state.comfyReferenceTarget + 1}`;
    }
}

async function uploadComfyReference(input) {
    const file = input.files?.[0];
    if (!file) {
        return;
    }
    const form = new FormData();
    form.append("file", file);
    setComfyStatus("Envoi de la référence vers ComfyUI...");
    const response = await fetch("/api/comfy/references/upload", { method: "POST", body: form });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
        throw new Error(data.error || "Échec de l’upload");
    }
    const replacement = {
        reference_id: null,
        enabled: true,
        image_name: data.input_name,
        input_name: data.input_name,
        photo_id: null,
        thumbnail_url: data.thumbnail_url,
        is_new: true,
    };
    if (state.comfyReferenceTarget === null) {
        state.comfyReferences.push(replacement);
    } else {
        const current = state.comfyReferences[state.comfyReferenceTarget];
        state.comfyReferences[state.comfyReferenceTarget] = { ...current, ...replacement, reference_id: current.reference_id, is_new: current.is_new };
    }
    state.comfyReferenceAddOpen = false;
    state.comfyReferenceTarget = null;
    setComfyStatus("");
    renderComfyReferences();
}

function moveComfyReference(from, to) {
    if (from === to || from < 0 || to < 0 || from >= state.comfyReferences.length || to >= state.comfyReferences.length) {
        return;
    }
    const [reference] = state.comfyReferences.splice(from, 1);
    state.comfyReferences.splice(to, 0, reference);
    state.comfyReferenceAddOpen = false;
    state.comfyReferenceTarget = null;
    renderComfyReferences();
}

async function submitComfyGeneration(event) {
    event.preventDefault();
    if (!state.comfySourcePhotoId || !state.comfyOptions || state.comfyJob?.active) {
        return;
    }
    setComfyStatus("Envoi a ComfyUI...");
    const payload = {
        prompt: $("#comfy-prompt").value,
        seed_mode: $("#comfy-seed-mode").value,
        steps: Number($("#comfy-steps").value || state.comfyOptions.steps || 1),
        loras: $$(".comfy-lora-row").map((row) => ({
            node_id: row.dataset.nodeId || null,
            new: row.dataset.newLora === "true",
            enabled: row.querySelector("[data-comfy-lora-enabled]").checked,
            lora_name: row.querySelector("[data-comfy-lora-name]").value,
            strength_model: Number(row.querySelector("[data-comfy-lora-strength]").value || 0),
        })),
        references: state.comfyReferences.map((reference) => ({
            reference_id: reference.reference_id || null,
            enabled: Boolean(reference.enabled),
            ...(reference.photo_id ? { photo_id: reference.photo_id } : { input_name: reference.input_name || reference.image_name }),
        })),
    };
    if (!payload.references.some((reference) => reference.enabled)) {
        setComfyStatus("Activez au moins une référence.", true);
        return;
    }
    try {
        setComfyStatus("Lancement du job...");
        const data = await fetchJson(`/api/photos/${state.comfySourcePhotoId}/comfy/generate`, {
            method: "POST",
            body: JSON.stringify(payload),
        });
        state.comfyJob = data.job;
        renderComfyJob(data.job);
        startComfyJobPolling(data.job.id);
    } catch (error) {
        if (error.status === 409 && error.data?.job) {
            state.comfyJob = error.data.job;
            renderComfyJob(error.data.job);
            startComfyJobPolling(error.data.job.id);
            return;
        }
        setComfyStatus(error.message, true);
        updateComfyFormJobControls(null);
        refreshComfyStatus();
    }
}

function startComfyJobPolling(jobId) {
    stopComfyJobPolling();
    state.comfyPollFailures = 0;
    pollComfyJob(jobId, 1000);
}

function stopComfyJobPolling() {
    window.clearTimeout(state.comfyJobPollTimer);
    state.comfyJobPollTimer = null;
}

function pollComfyJob(jobId, delay) {
    state.comfyJobPollTimer = window.setTimeout(async () => {
        try {
            const data = await fetchJson(`/api/comfy/jobs/${jobId}`);
            state.comfyPollFailures = 0;
            state.comfyJob = data.job;
            renderComfyJob(data.job);
            if (!data.job.active) {
                stopComfyJobPolling();
                refreshComfyStatus();
                if (data.job.state === "done" && data.job.photo) {
                    addPhotoToCurrentGallery(data.job.photo);
                    if ($("#comfy-close-on-finish")?.checked !== false) {
                        closeComfyModal();
                        await openPhoto(data.job.photo.id);
                    }
                }
                return;
            }
            pollComfyJob(jobId, 1000);
        } catch (error) {
            state.comfyPollFailures += 1;
            if (state.comfyPollFailures >= 8) {
                stopComfyJobPolling();
                setComfyStatus(`Suivi interrompu: ${error.message}`, true);
                refreshComfyStatus();
                return;
            }
            const retryDelay = Math.min(1000 * Math.pow(1.7, state.comfyPollFailures), 10000);
            setComfyStatus(`Connexion temporairement perdue, nouvelle tentative ${state.comfyPollFailures}/8...`);
            pollComfyJob(jobId, retryDelay);
        }
    }, delay);
}

function renderComfyJob(job) {
    state.comfyJob = job;
    const pieces = [job.message || job.state || "Generation"];
    if (job.node_title || job.node) {
        pieces.push(job.node_title || `node ${job.node}`);
    }
    if (job.progress && job.progress_max) {
        pieces.push(`${job.progress}/${job.progress_max}`);
    }
    setComfyStatus(pieces.join(" | "), job.state === "error");
    renderComfyJobBanner(job);
    updateComfyFormJobControls(job);
    updateComfyButton();
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
    if (job.state === "cancelled") {
        setComfyStatus("Generation annulee");
    }
}

function renderComfyJobBanner(job) {
    const box = $("#comfy-job-status");
    if (!box) {
        return;
    }
    if (!job?.active) {
        box.hidden = true;
        return;
    }
    box.hidden = false;
    box.dataset.state = job.state || "running";
    $("#comfy-job-status-title").textContent = job.state === "cancel_requested"
        ? "Annulation en cours"
        : "Generation en cours";
    $("#comfy-job-status-message").textContent = job.message || job.state || "Generation...";
    const details = [];
    if (job.node_title || job.node) {
        details.push(job.node_title || `node ${job.node}`);
    }
    if (job.progress !== null && job.progress_max !== null) {
        details.push(`${job.progress}/${job.progress_max}`);
    }
    $("#comfy-job-status-detail").textContent = details.join(" | ");
}

async function cancelComfyGeneration() {
    const job = state.comfyJob;
    if (!job?.active || job.state === "cancel_requested") {
        return;
    }
    try {
        const data = await fetchJson(`/api/comfy/jobs/${job.id}/cancel`, { method: "POST", body: "{}" });
        renderComfyJob(data.job);
        startComfyJobPolling(data.job.id);
    } catch (error) {
        setComfyStatus(error.message, true);
    }
}

async function resumeComfyGenerationState() {
    try {
        const data = await fetchJson("/api/comfy/jobs/current");
        if (data.job?.active) {
            state.comfyJob = data.job;
            state.comfySourcePhotoId = data.job.photo_id;
            renderComfyJob(data.job);
            startComfyJobPolling(data.job.id);
        }
    } catch (_error) {
        // ComfyUI generation tracking is non-critical during initial page load.
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

async function scanSelectedMetadata() {
    const photoIds = selectedPhotoIds();
    if (!photoIds.length) {
        return;
    }
    closeSelectionActionsMenu();
    const done = setBusy($("#selection-actions-button"), "Scan...");
    setSelectionStatus(`Scan JSON de ${photoIds.length} photo(s)...`);
    try {
        const data = await fetchJson("/api/photos/batch/metadata/rescan", {
            method: "POST",
            body: JSON.stringify({ photo_ids: photoIds }),
        });
        const summary = data.summary;
        setSelectionStatus(
            `${summary.scanned} photo(s) scannee(s), ${summary.failed} erreur(s).${batchFailureSuffix(data.results || [])}`,
            summary.failed > 0,
        );
    } catch (error) {
        setSelectionStatus(error.message, true);
    } finally {
        done();
        renderSelectionState();
    }
}

async function scanSelectedFaces() {
    const photoIds = selectedPhotoIds();
    if (!photoIds.length) {
        return;
    }
    closeSelectionActionsMenu();
    try {
        const job = await startFaceJob("selection", { photo_ids: photoIds });
        setSelectionStatus(`Reconnaissance faciale lancee sur ${photoIds.length} photo(s).`);
        if (job) {
            pollFaceJob(job.id);
        }
    } catch (error) {
        setSelectionStatus(error.message, true);
    }
}

async function deleteSelectedPhotos() {
    const photoIds = selectedPhotoIds();
    if (!photoIds.length) {
        return;
    }
    closeSelectionActionsMenu();
    if (!window.confirm(`Supprimer definitivement ${photoIds.length} photo(s) de la base et du disque ?`)) {
        return;
    }
    const done = setBusy($("#selection-actions-button"), "Suppression...");
    setSelectionStatus(`Suppression de ${photoIds.length} photo(s)...`);
    try {
        const data = await fetchJson("/api/photos/batch", {
            method: "DELETE",
            body: JSON.stringify({ photo_ids: photoIds }),
        });
        state.albums = data.albums || state.albums;
        const deletedIds = (data.results || [])
            .filter((result) => result.status === "deleted")
            .map((result) => result.photo_id);
        deletedIds.forEach((photoId) => {
            state.selectedPhotoIds.delete(photoId);
            removePhotoFromCurrentGallery(photoId);
        });
        const failed = data.summary?.failed || 0;
        if (state.selectedPhotoIds.size) {
            setSelectionStatus(
                `${deletedIds.length} photo(s) supprimee(s), ${failed} erreur(s).${batchFailureSuffix(data.results || [])}`,
                true,
            );
            renderSelectionState();
        } else {
            exitSelectionMode();
        }
    } catch (error) {
        setSelectionStatus(error.message, true);
    } finally {
        done();
        renderSelectionState();
    }
}

function openBatchTagModal() {
    if (!state.selectedPhotoIds.size) {
        return;
    }
    closeSelectionActionsMenu();
    $("#batch-tag-title").textContent = `Edit tag - ${state.selectedPhotoIds.size} photo(s)`;
    $("#batch-tag-operation").value = "add";
    $("#batch-tag-input").value = "";
    $("#batch-tag-status").textContent = "";
    $("#batch-tag-status").classList.remove("error");
    $("#batch-tag-modal").classList.add("open");
    $("#batch-tag-modal").setAttribute("aria-hidden", "false");
    $("#batch-tag-input").focus();
}

function closeBatchTagModal() {
    const modal = $("#batch-tag-modal");
    if (!modal) {
        return;
    }
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
}

async function submitBatchTags(event) {
    event.preventDefault();
    const tags = tagsFromInput($("#batch-tag-input").value);
    const status = $("#batch-tag-status");
    status.classList.remove("error");
    if (!tags.length) {
        status.textContent = "Saisissez au moins un tag.";
        status.classList.add("error");
        return;
    }
    const done = setBusy($("#batch-tag-submit"), "Application...");
    status.textContent = "";
    try {
        await fetchJson("/api/photos/batch/tags", {
            method: "PATCH",
            body: JSON.stringify({
                photo_ids: selectedPhotoIds(),
                operation: $("#batch-tag-operation").value,
                tags,
            }),
        });
        window.location.reload();
    } catch (error) {
        status.textContent = error.message;
        status.classList.add("error");
        done();
    }
}

function handleBatchAction(action) {
    if (action === "clear") {
        exitSelectionMode();
        return;
    }
    if (!state.selectedPhotoIds.size) {
        return;
    }
    if (action === "album") {
        openBatchAlbumActionModal();
    } else if (action === "scan") {
        scanSelectedMetadata();
    } else if (action === "faces") {
        scanSelectedFaces();
    } else if (action === "tags") {
        openBatchTagModal();
    } else if (action === "delete") {
        deleteSelectedPhotos();
    }
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

function tagFilterDraftLists() {
    const include = [];
    const exclude = [];
    Object.entries(state.tagFilterDraft || {}).forEach(([tagName, value]) => {
        if (value === "true") {
            include.push(tagName);
        } else if (value === "false") {
            exclude.push(tagName);
        }
    });
    return { include, exclude };
}

function tagFilterMetadata(tagName) {
    return [...(state.activeTagStats || []), ...(state.albumTagStats || [])]
        .find((tag) => tag.name === tagName) || { name: tagName, category: null };
}

function renderActiveTagFilters() {
    const container = $("#tag-filter-active-list");
    if (!container) {
        return;
    }
    const { include, exclude } = tagFilterDraftLists();
    const active = [
        ...include.map((name) => ({ name, value: "true" })),
        ...exclude.map((name) => ({ name, value: "false" })),
    ];
    container.innerHTML = active.length ? active.map(({ name, value }) => {
        const tag = tagFilterMetadata(name);
        const escapedName = escapeHtml(name);
        const included = value === "true";
        return `
            <span class="tag-filter-active is-${included ? "include" : "exclude"}">
                ${tagCategoryIcon(tag.category)}
                <span class="tag-filter-active-name">${escapedName}</span>
                <button type="button" class="tag-filter-active-state"
                        data-active-filter-toggle="${escapedName}"
                        title="Basculer en ${included ? "exclu" : "inclus"}"
                        aria-label="Basculer ${escapedName} en ${included ? "exclu" : "inclus"}">
                    ${included ? "Inclus" : "Exclu"}
                </button>
                <button type="button" class="tag-filter-active-clear"
                        data-active-filter-clear="${escapedName}"
                        title="Retirer le filtre" aria-label="Retirer le filtre ${escapedName}">×</button>
            </span>
        `;
    }).join("") : '<span class="muted">Aucun filtre actif</span>';
}

function renderTagFilters() {
    const container = $("#tag-filter-list");
    const query = state.tagFilterQuery.trim().toLowerCase();
    const tagStats = (state.albumTagStats || []).filter((tag) => {
        const isActive = (state.tagFilterDraft[tag.name] || "any") !== "any";
        return !isActive && (!query || tag.name.toLowerCase().includes(query));
    });
    renderActiveTagFilters();
    const matchCount = $("#tag-filter-match-count");
    if (matchCount) {
        const count = Number(state.tagFacetMatchingCount ?? state.filteredPhotoCount ?? 0);
        matchCount.textContent = `${count} photo${count === 1 ? "" : "s"} correspondante${count === 1 ? "" : "s"}`;
    }
    if (!tagStats.length) {
        container.innerHTML = `<p class="muted tag-filter-empty">${
            query ? "Aucun tag ne correspond à la recherche." : "Aucun autre tag dans les photos filtrées."
        }</p>`;
        return;
    }
    const maxOccurrence = Math.max(...tagStats.map((tag) => Number(tag.occurrence_count) || 0), 1);
    container.innerHTML = tagStats.map((tag) => {
        const selected = state.tagFilterDraft[tag.name] || "any";
        const occurrence = Number(tag.occurrence_count) || 0;
        const progress = Math.min(100, Math.max(0, (occurrence / maxOccurrence) * 100));
        const escapedName = escapeHtml(tag.name);
        return `
            <div class="tag-filter-row">
                <span class="tag-filter-name" title="${escapedName}">
                    ${tagCategoryIcon(tag.category)}
                    <span>${escapedName}</span>
                </span>
                <span class="tag-filter-count" style="--tag-progress: ${progress}%" title="${occurrence} occurrence${occurrence > 1 ? "s" : ""}">
                    ${occurrence}
                </span>
                <div class="tag-filter-toggle" role="group" aria-label="Filtre ${escapedName}">
                    ${["any", "true", "false"].map((value) => `
                        <button type="button"
                                class="tag-filter-choice is-${value}${selected === value ? " is-selected" : ""}"
                                data-tag-filter-name="${escapedName}"
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

async function refreshTagFacets() {
    if (!state.selectedAlbum?.id) {
        return;
    }
    state.tagFacetAbortController?.abort();
    const controller = new AbortController();
    state.tagFacetAbortController = controller;
    const requestId = ++state.tagFacetRequestId;
    const status = $("#tag-filter-status");
    if (status) {
        status.textContent = "Mise à jour des tags...";
        status.classList.remove("error");
    }
    const { include, exclude } = tagFilterDraftLists();
    const url = new URL(
        `/api/albums/${state.selectedAlbum.id}/tag-facets`,
        window.location.origin,
    );
    include.forEach((name) => url.searchParams.append("include_tag", name));
    exclude.forEach((name) => url.searchParams.append("exclude_tag", name));
    url.searchParams.set("max_sensitivity", state.maxSensitivity || "neutral");
    try {
        const data = await fetchJson(url.toString(), { signal: controller.signal });
        if (requestId !== state.tagFacetRequestId) {
            return;
        }
        state.albumTagStats = data.tags || [];
        state.activeTagStats = data.active_tags || [];
        state.tagFacetMatchingCount = Number(data.matching_photo_count) || 0;
        if (status) {
            status.textContent = "";
        }
        renderTagFilters();
    } catch (error) {
        if (error.name === "AbortError" || requestId !== state.tagFacetRequestId) {
            return;
        }
        if (status) {
            status.textContent = error.message;
            status.classList.add("error");
        }
    }
}

function openTagFilter() {
    state.tagFilterDraft = {};
    (state.tagFilters?.include || []).forEach((name) => {
        state.tagFilterDraft[name] = "true";
    });
    (state.tagFilters?.exclude || []).forEach((name) => {
        if (!state.tagFilterDraft[name]) {
            state.tagFilterDraft[name] = "false";
        }
    });
    state.tagFilterQuery = "";
    state.tagFacetMatchingCount = state.filteredPhotoCount || 0;
    const search = $("#tag-filter-search");
    if (search) {
        search.value = "";
    }
    renderTagFilters();
    $("#tag-filter-modal").classList.add("open");
    $("#tag-filter-modal").setAttribute("aria-hidden", "false");
    refreshTagFacets();
    search?.focus();
}

function sameTagSet(left, right) {
    const leftSet = new Set(left || []);
    const rightSet = new Set(right || []);
    return leftSet.size === rightSet.size && [...leftSet].every((value) => rightSet.has(value));
}

function applyTagFilters() {
    const { include, exclude } = tagFilterDraftLists();
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
    state.tagFacetAbortController?.abort();
    state.tagFacetAbortController = null;
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    applyTagFilters();
}

function chooseTagFilter(event) {
    const clearButton = event.target.closest("[data-active-filter-clear]");
    if (clearButton) {
        state.tagFilterDraft[clearButton.dataset.activeFilterClear] = "any";
        renderTagFilters();
        refreshTagFacets();
        return;
    }
    const toggleButton = event.target.closest("[data-active-filter-toggle]");
    if (toggleButton) {
        const tagName = toggleButton.dataset.activeFilterToggle;
        state.tagFilterDraft[tagName] = state.tagFilterDraft[tagName] === "true" ? "false" : "true";
        renderTagFilters();
        refreshTagFacets();
        return;
    }
    const button = event.target.closest("[data-tag-filter-value]");
    if (!button) {
        return;
    }
    state.tagFilterDraft[button.dataset.tagFilterName] = button.dataset.tagFilterValue;
    renderTagFilters();
    refreshTagFacets();
}

async function loadFaceIdentities() {
    const data = await fetchJson("/api/face/identities");
    state.faceIdentities = data.identities || [];
    renderFaceIdentities();
    if (state.currentPhoto) {
        renderPhotoFaces(state.currentPhoto.face_analysis);
    }
    return state.faceIdentities;
}

async function refreshFaceStatus() {
    const data = await fetchJson("/api/face/status");
    state.faceStatus = data;
    const engine = data.engine || {};
    const engineStatus = $("#face-engine-status");
    if (engineStatus) {
        engineStatus.textContent = engine.configured
            ? `${engine.model_name} · ${engine.provider || "provider choisi au chargement"} · local`
            : !engine.model_present
                ? `Modele absent : ${engine.model_directory}`
                : "Dependances absentes : installez InsightFace et ONNX Runtime";
        engineStatus.classList.toggle("error", !engine.configured);
    }
    const automatic = $("#face-automatic-scan");
    if (automatic) {
        automatic.checked = Boolean(data.automatic_scan);
    }
    renderFaceJobStatus(data.job);
    return data;
}

function renderFaceJobStatus(job, options = {}) {
    const box = $("#face-job-status");
    if (!box || !job) {
        return;
    }
    if (job.params?.parent_scan_job_id) {
        box.hidden = true;
        return;
    }
    if (!options.force && !job.active && job.state !== "error") {
        return;
    }
    if (state.faceJobStatusClosed && !job.active) {
        return;
    }
    box.hidden = false;
    box.dataset.jobId = job.id;
    box.dataset.state = job.state;
    $("#face-job-status-title").textContent = job.state === "done" ? "Visages analyses"
        : job.state === "error" ? "Reconnaissance en erreur"
        : job.state === "cancelled" ? "Reconnaissance annulee"
        : "Reconnaissance faciale";
    $("#face-job-status-message").textContent = job.error || job.message || job.state;
    const percent = job.total ? Math.round((job.processed / job.total) * 100) : 0;
    $("#face-job-status-detail").textContent = `${job.processed}/${job.total} (${percent} %) · ${job.recognized} reconnu(s) · ${job.pending} a confirmer · ${job.errors_count} erreur(s)`;
    $("#face-job-status-close").hidden = Boolean(job.active);
    const cancelButtons = [$("#cancel-face-job-button"), $("#face-job-status-cancel")].filter(Boolean);
    const resume = $("#resume-face-job-button");
    cancelButtons.forEach((cancel) => {
        cancel.hidden = !job.active || job.state === "cancel_requested";
        cancel.dataset.jobId = job.id;
    });
    if (resume) {
        resume.hidden = !["error", "cancelled"].includes(job.state);
        resume.dataset.jobId = job.id;
    }
}

async function startFaceJob(scope, extra = {}, pollOptions = {}) {
    state.faceJobStatusClosed = false;
    const data = await fetchJson("/api/face/jobs", {
        method: "POST",
        body: JSON.stringify({ scope, mode: "detect", ...extra }),
    });
    renderFaceJobStatus(data.job, { force: true });
    pollFaceJob(data.job.id, pollOptions);
    return data.job;
}

function pollFaceJob(jobId, options = {}) {
    window.clearInterval(state.faceJobPollTimer);
    if (state.faceJobPollDone) {
        state.faceJobPollDone();
    }
    state.faceJobPollDone = options.onFinished || null;
    state.faceJobPollTimer = window.setInterval(async () => {
        try {
            const data = await fetchJson(`/api/face/jobs/${jobId}`);
            renderFaceJobStatus(data.job, { force: true });
            if (!data.job.active) {
                window.clearInterval(state.faceJobPollTimer);
                await refreshCurrentPhotoDetail();
                const onFinished = state.faceJobPollDone;
                state.faceJobPollDone = null;
                onFinished?.();
            }
        } catch (error) {
            window.clearInterval(state.faceJobPollTimer);
            const onFinished = state.faceJobPollDone;
            state.faceJobPollDone = null;
            onFinished?.();
            options.onError?.(error);
        }
    }, 1000);
}

async function refreshCurrentPhotoDetail() {
    if (!state.currentPhoto) {
        return;
    }
    const data = await fetchJson(`/api/photos/${state.currentPhoto.id}`);
    state.currentPhoto = data.photo;
    renderPhotoDetail(data.photo);
    syncPhotoInCurrentGallery(data.photo);
}

async function scanCurrentPhotoFaces() {
    if (!state.currentPhoto) {
        return;
    }
    const done = setBusy($("#scan-photo-faces-button"), "Analyse...", { spinner: true });
    try {
        await startFaceJob(
            "photo",
            { photo_ids: [state.currentPhoto.id] },
            { onFinished: done, onError: (error) => alert(error.message) },
        );
    } catch (error) {
        done();
        alert(error.message);
    }
}

async function setAutomaticFaceScan(event) {
    try {
        const data = await fetchJson("/api/face/settings", {
            method: "PATCH",
            body: JSON.stringify({ automatic_scan: event.target.checked }),
        });
        event.target.checked = Boolean(data.automatic_scan);
    } catch (error) {
        event.target.checked = !event.target.checked;
        alert(error.message);
    }
}

async function loadFaceAdmin() {
    try {
        await Promise.all([refreshFaceStatus(), loadFaceIdentities()]);
    } catch (error) {
        $("#face-engine-status").textContent = error.message;
        $("#face-engine-status").classList.add("error");
    }
}

function renderFaceIdentities() {
    const container = $("#face-identity-list");
    const target = $("#face-reference-identity");
    if (target) {
        target.innerHTML = state.faceIdentities.length
            ? state.faceIdentities.map((identity) => `<option value="${identity.id}">${escapeHtml(identity.tag_name)}</option>`).join("")
            : '<option value="">Creez une identite</option>';
        target.disabled = !state.faceIdentities.length;
    }
    if (!container) {
        return;
    }
    if (!state.faceIdentities.length) {
        container.innerHTML = '<p class="muted">Aucune identite configuree.</p>';
        return;
    }
    container.innerHTML = state.faceIdentities.map((identity) => `
        <form class="face-identity-card" data-face-identity-id="${identity.id}">
            <div class="face-identity-title">
                <input name="tag_name" value="${escapeHtml(identity.tag_name)}" aria-label="Tag de l'identite">
                <label class="checkbox-field"><input name="enabled" type="checkbox" ${identity.enabled ? "checked" : ""}> Active</label>
            </div>
            <div class="face-threshold-grid">
                <label>Revue <input name="review_threshold" type="number" min="0" max="1" step="0.01" value="${identity.review_threshold}"></label>
                <label>Auto <input name="automatic_threshold" type="number" min="0" max="1" step="0.01" value="${identity.automatic_threshold}"></label>
                <label>Marge <input name="margin_threshold" type="number" min="0" max="1" step="0.01" value="${identity.margin_threshold}"></label>
            </div>
            <div class="face-reference-summary ${identity.reference_count < 3 ? "warning" : ""}">
                ${identity.reference_count} reference(s)${identity.reference_count < 3 ? " · 3 minimum recommandees" : ""}
            </div>
            <div class="face-reference-grid">
                ${(identity.references || []).map((reference) => `
                    <div class="face-reference-thumb">
                        <img src="${reference.crop_url}" alt="Reference ${escapeHtml(identity.tag_name)}">
                        <button type="button" data-delete-face-reference="${reference.id}" title="Supprimer">&times;</button>
                    </div>
                `).join("")}
            </div>
            <div class="face-identity-actions">
                <button type="submit">Enregistrer</button>
                <button type="button" class="danger" data-delete-face-identity="${identity.id}">Supprimer</button>
            </div>
        </form>
    `).join("");
}

async function createFaceIdentity(event) {
    event.preventDefault();
    const tagName = $("#face-identity-tag").value.trim();
    if (!tagName) {
        return;
    }
    await fetchJson("/api/face/identities", {
        method: "POST",
        body: JSON.stringify({ tag_name: tagName }),
    });
    $("#face-identity-tag").value = "";
    await loadFaceIdentities();
}

async function saveFaceIdentity(event) {
    event.preventDefault();
    const form = event.target;
    const data = new FormData(form);
    const response = await fetchJson(`/api/face/identities/${form.dataset.faceIdentityId}`, {
        method: "PATCH",
        body: JSON.stringify({
            tag_name: data.get("tag_name"),
            enabled: data.get("enabled") === "on",
            review_threshold: Number(data.get("review_threshold")),
            automatic_threshold: Number(data.get("automatic_threshold")),
            margin_threshold: Number(data.get("margin_threshold")),
        }),
    });
    if (response.job) {
        renderFaceJobStatus(response.job, { force: true });
        pollFaceJob(response.job.id);
    }
    await loadFaceIdentities();
}

async function importFaceReference(event) {
    event.preventDefault();
    const file = $("#face-reference-file").files[0];
    if (!file || !$("#face-reference-identity").value) {
        return;
    }
    const status = $("#face-import-status");
    const button = event.target.querySelector('button[type="submit"]');
    const done = setBusy(button, "Detection...");
    status.textContent = "Analyse locale de la reference...";
    status.classList.remove("error");
    const body = new FormData();
    body.append("file", file);
    try {
        const response = await fetch("/api/face/imports", { method: "POST", body });
        const data = await response.json();
        if (!response.ok || data.ok === false) {
            throw new Error(data.error || "Erreur d'import");
        }
        state.faceImport = data.import;
        status.textContent = `${data.import.faces.length} visage(s) detecte(s). Choisissez la reference.`;
        renderFaceImportCandidates();
    } catch (error) {
        status.textContent = error.message;
        status.classList.add("error");
    } finally {
        done();
    }
}

function renderFaceImportCandidates() {
    const container = $("#face-import-candidates");
    const imported = state.faceImport;
    container.innerHTML = imported?.faces?.map((face) => `
        <button type="button" class="face-import-candidate" data-add-imported-face="${face.face_index}">
            <img src="${face.crop_url}" alt="Visage importe ${face.face_index + 1}">
            <span>Utiliser ce visage</span>
        </button>
    `).join("") || "";
}

async function addImportedFaceReference(faceIndex) {
    const identityId = Number($("#face-reference-identity").value);
    if (!identityId || !state.faceImport) {
        return;
    }
    const data = await fetchJson(`/api/face/identities/${identityId}/references/import`, {
        method: "POST",
        body: JSON.stringify({ token: state.faceImport.token, face_index: faceIndex }),
    });
    state.faceImport = null;
    $("#face-reference-file").value = "";
    $("#face-import-candidates").innerHTML = "";
    $("#face-import-status").textContent = "Reference ajoutee.";
    if (data.job) {
        renderFaceJobStatus(data.job, { force: true });
        pollFaceJob(data.job.id);
    }
    await loadFaceIdentities();
}

async function addGalleryFaceReference(faceCard) {
    const identityId = Number(faceCard.querySelector("[data-face-reference-identity]").value);
    const faceId = Number(faceCard.dataset.faceId);
    const data = await fetchJson(`/api/face/identities/${identityId}/references/gallery`, {
        method: "POST",
        body: JSON.stringify({ face_id: faceId }),
    });
    if (data.job) {
        renderFaceJobStatus(data.job, { force: true });
        pollFaceJob(data.job.id);
    }
    await loadFaceIdentities();
}

async function decideFace(faceId, identityId, decision) {
    const data = await fetchJson(`/api/photo-faces/${faceId}/decision`, {
        method: "POST",
        body: JSON.stringify({ identity_id: identityId, decision }),
    });
    state.currentPhoto = data.photo;
    renderPhotoDetail(data.photo);
    syncPhotoInCurrentGallery(data.photo);
}

async function deleteFaceIdentity(identityId) {
    if (!window.confirm("Supprimer cette identite faciale ? Le tag manuel sera conserve.")) {
        return;
    }
    const data = await fetchJson(`/api/face/identities/${identityId}`, { method: "DELETE" });
    if (data.job) {
        renderFaceJobStatus(data.job, { force: true });
        pollFaceJob(data.job.id);
    }
    await loadFaceIdentities();
}

async function deleteFaceReference(referenceId) {
    const data = await fetchJson(`/api/face/references/${referenceId}`, { method: "DELETE" });
    if (data.job) {
        renderFaceJobStatus(data.job, { force: true });
        pollFaceJob(data.job.id);
    }
    await loadFaceIdentities();
}

async function cancelFaceJob(jobId) {
    const data = await fetchJson(`/api/face/jobs/${jobId}/cancel`, { method: "POST", body: "{}" });
    renderFaceJobStatus(data.job, { force: true });
}

async function resumeFaceJob(jobId) {
    const data = await fetchJson(`/api/face/jobs/${jobId}/resume`, { method: "POST", body: "{}" });
    renderFaceJobStatus(data.job, { force: true });
    pollFaceJob(data.job.id);
}

function openAdmin(section = "albums") {
    renderAlbumAdmin();
    $("#admin-modal").classList.add("open");
    $("#admin-modal").setAttribute("aria-hidden", "false");
    switchConfigSection(section).catch((error) => alert(error.message));
}

function closeAdmin() {
    $("#admin-modal").classList.remove("open");
    $("#admin-modal").setAttribute("aria-hidden", "true");
}

async function switchConfigSection(section) {
    const descriptions = {
        albums: "Gérer les albums de la galerie.",
        tags: "Configurer la sensibilité des tags présents dans la galerie.",
        "lora-tags": "Configurer les tags ajoutés automatiquement depuis les LoRA.",
        faces: "Configurer l’analyse et l’identification des visages.",
    };
    if (!descriptions[section]) {
        section = "albums";
    }
    state.configSection = section;
    $$(".config-nav-button").forEach((button) => {
        const isActive = button.dataset.configSection === section;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-selected", isActive ? "true" : "false");
        button.tabIndex = isActive ? 0 : -1;
    });
    $$("[data-config-panel]").forEach((panel) => {
        const isActive = panel.dataset.configPanel === section;
        panel.hidden = !isActive;
        panel.classList.toggle("is-active", isActive);
    });
    $("#config-section-description").textContent = descriptions[section];
    if (section === "tags") {
        await loadTagSensitivities();
    } else if (section === "lora-tags") {
        await loadLoraTagMappings();
    } else if (section === "faces") {
        await loadFaceAdmin();
    }
}

async function loadTagSensitivities() {
    const data = await fetchJson("/api/tags");
    state.tagSensitivities = data.tags || [];
    renderTagSensitivities();
}

function tagCategorySelectHtml(tag) {
    const selected = tag.category || "";
    const options = [
        ["", "Aucune"],
        ["clothing", "Vêtement"],
        ["person", "Personne"],
        ["constraint", "Contrainte"],
    ];
    return `
        <label class="tag-category-field" title="${
            tag.is_face_tag ? "Les tags de visage restent dans la catégorie Personne" : "Catégorie du tag"
        }">
            <span class="sr-only">Catégorie de ${escapeHtml(tag.name)}</span>
            <select class="tag-category-select" data-tag-category ${tag.is_face_tag ? "disabled" : ""}>
                ${options.map(([value, label]) => `
                    <option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>
                `).join("")}
            </select>
        </label>
    `;
}

function renderTagSensitivities() {
    const container = $("#tag-sensitivity-list");
    if (!container) {
        return;
    }
    const query = state.tagSensitivityQuery.trim().toLowerCase();
    const tags = state.tagSensitivities.filter((tag) => !query || tag.name.toLowerCase().includes(query));
    container.innerHTML = tags.length ? tags.map((tag) => `
        <div class="tag-sensitivity-row" data-tag-sensitivity-id="${tag.id}">
            <span class="tag-sensitivity-name" title="${escapeHtml(tag.name)}">
                ${tagCategoryIcon(tag.category)}
                <span>${escapeHtml(tag.name)}</span>
            </span>
            <span class="tag-sensitivity-count">${tag.occurrence_count} photo${tag.occurrence_count === 1 ? "" : "s"}</span>
            ${tagCategorySelectHtml(tag)}
            ${sensitivitySelectorHtml(tag.sensitivity || "neutral", `aria-label="Sensibilité de ${escapeHtml(tag.name)}"`)}
        </div>
    `).join("") : '<p class="lora-tag-empty">Aucun tag trouvé.</p>';
}

async function updateTagSensitivity(button) {
    const row = button.closest("[data-tag-sensitivity-id]");
    if (!row) {
        return;
    }
    const tagId = Number(row.dataset.tagSensitivityId);
    const sensitivity = button.dataset.sensitivityValue;
    const status = $("#tag-sensitivity-status");
    row.classList.add("is-saving");
    try {
        const data = await fetchJson(`/api/tags/${tagId}`, {
            method: "PATCH",
            body: JSON.stringify({ sensitivity }),
        });
        const tag = state.tagSensitivities.find((item) => item.id === tagId);
        if (tag) {
            tag.sensitivity = data.tag.sensitivity;
        }
        status.textContent = `Sensibilité de « ${data.tag.name} » : ${data.tag.sensitivity}.`;
        status.classList.remove("error");
        renderTagSensitivities();
    } catch (error) {
        status.textContent = error.message;
        status.classList.add("error");
        row.classList.remove("is-saving");
    }
}

async function updateTagCategory(select) {
    const row = select.closest("[data-tag-sensitivity-id]");
    if (!row) {
        return;
    }
    const tagId = Number(row.dataset.tagSensitivityId);
    const category = select.value || null;
    const status = $("#tag-sensitivity-status");
    row.classList.add("is-saving");
    try {
        const data = await fetchJson(`/api/tags/${tagId}`, {
            method: "PATCH",
            body: JSON.stringify({ category }),
        });
        const tag = state.tagSensitivities.find((item) => item.id === tagId);
        if (tag) {
            tag.category = data.tag.category;
            tag.is_face_tag = Boolean(data.tag.is_face_tag);
        }
        const categoryLabel = TAG_CATEGORY_LABELS[data.tag.category] || "Aucune";
        status.textContent = `Catégorie de « ${data.tag.name} » : ${categoryLabel}.`;
        status.classList.remove("error");
        renderTagSensitivities();
    } catch (error) {
        status.textContent = error.message;
        status.classList.add("error");
        renderTagSensitivities();
    }
}

async function loadLoraTagMappings() {
    const data = await fetchJson("/api/lora-tag-mappings");
    state.loraTagMappings = data.mappings || [];
    state.loraTagCatalog = data.loras || [];
    renderLoraTagMappings();
}

function renderLoraTagMappings() {
    const select = $("#lora-tag-lora");
    const addButton = $("#lora-tag-add-button");
    const list = $("#lora-tag-mapping-list");
    const mappedNames = new Set(state.loraTagMappings.map((mapping) => mapping.lora_name));
    const availableLoras = state.loraTagCatalog.filter((lora) => !mappedNames.has(lora.lora_name));

    select.innerHTML = availableLoras.length
        ? availableLoras.map((lora) => `
            <option value="${escapeHtml(lora.lora_name)}">${escapeHtml(lora.lora_name)}</option>
        `).join("")
        : '<option value="">Aucun LoRA disponible</option>';
    select.disabled = !availableLoras.length;
    addButton.disabled = !availableLoras.length;

    if (!state.loraTagMappings.length) {
        list.innerHTML = '<p class="muted lora-tag-empty">Aucune affectation automatique configurée.</p>';
        return;
    }
    list.innerHTML = state.loraTagMappings.map((mapping) => {
        if (state.loraTagEditingId === mapping.id) {
            const tagNames = (mapping.tags || []).map((tag) => tag.name).join(", ");
            return `
                <article class="lora-tag-mapping-card is-editing">
                    <form class="lora-tag-edit-form" data-edit-lora-tag-mapping="${mapping.id}">
                        <strong>${escapeHtml(mapping.lora_name)}</strong>
                        <label class="form-field">
                            <span>Tags (séparés par des virgules)</span>
                            <input name="tag_names" type="text" value="${escapeHtml(tagNames)}" required autocomplete="off">
                        </label>
                        <div class="lora-tag-edit-actions">
                            <button type="submit">Enregistrer</button>
                            <button type="button" data-cancel-lora-tag-edit>Annuler</button>
                        </div>
                    </form>
                </article>
            `;
        }
        return `
        <article class="lora-tag-mapping-card">
            <div class="lora-tag-summary">
                <strong>${escapeHtml(mapping.lora_name)}</strong>
                <span class="lora-tag-arrow" aria-hidden="true">→</span>
                <span class="lora-tag-values">${renderTags(mapping.tags)}</span>
            </div>
            <div class="lora-tag-card-actions">
                <button class="lora-tag-edit-button" type="button" data-start-lora-tag-edit="${mapping.id}"
                        title="Éditer l'affectation"
                        aria-label="Éditer l'affectation de ${escapeHtml(mapping.lora_name)}">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M12 20h9"></path>
                        <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path>
                    </svg>
                </button>
                <button class="lora-tag-delete-button" type="button" data-delete-lora-tag-mapping="${mapping.id}"
                        title="Supprimer l'affectation" aria-label="Supprimer l'affectation de ${escapeHtml(mapping.lora_name)}">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"></path>
                    </svg>
                </button>
            </div>
        </article>
    `;
    }).join("");
}

function setLoraTagMappingStatus(message, isError = false) {
    const status = $("#lora-tag-mapping-status");
    status.textContent = message;
    status.classList.toggle("error", isError);
}

async function createLoraTagMapping(event) {
    event.preventDefault();
    const loraName = $("#lora-tag-lora").value;
    const tagNames = Array.from(new Set(tagsFromInput($("#lora-tag-name").value)));
    if (!loraName || !tagNames.length) {
        setLoraTagMappingStatus("Sélectionnez un LoRA et saisissez au moins un tag.", true);
        return;
    }
    const done = setBusy($("#lora-tag-add-button"), "Ajout...");
    try {
        await fetchJson("/api/lora-tag-mappings", {
            method: "POST",
            body: JSON.stringify({ lora_name: loraName, tag_names: tagNames }),
        });
        $("#lora-tag-name").value = "";
        await loadLoraTagMappings();
        setLoraTagMappingStatus("Affectation ajoutée. Elle sera appliquée au prochain Scan JSON.");
    } catch (error) {
        setLoraTagMappingStatus(error.message, true);
    } finally {
        done();
        renderLoraTagMappings();
    }
}

async function deleteLoraTagMapping(mappingId) {
    const button = $(`[data-delete-lora-tag-mapping="${mappingId}"]`);
    const done = setBusy(button, "…");
    try {
        await fetchJson(`/api/lora-tag-mappings/${mappingId}`, { method: "DELETE" });
        await loadLoraTagMappings();
        setLoraTagMappingStatus("Affectation supprimée. Les photos seront mises à jour au prochain Scan JSON.");
    } catch (error) {
        setLoraTagMappingStatus(error.message, true);
    } finally {
        done();
    }
}

function startLoraTagMappingEdit(mappingId) {
    state.loraTagEditingId = mappingId;
    renderLoraTagMappings();
    const input = $(`[data-edit-lora-tag-mapping="${mappingId}"] input[name="tag_names"]`);
    input?.focus();
    input?.select();
}

function cancelLoraTagMappingEdit() {
    state.loraTagEditingId = null;
    renderLoraTagMappings();
}

async function updateLoraTagMapping(event) {
    event.preventDefault();
    const form = event.target;
    const mappingId = Number(form.dataset.editLoraTagMapping);
    const tagNames = Array.from(new Set(tagsFromInput(form.elements.tag_names.value)));
    if (!tagNames.length) {
        setLoraTagMappingStatus("Saisissez au moins un tag.", true);
        return;
    }
    const submitButton = form.querySelector('button[type="submit"]');
    const done = setBusy(submitButton, "Enregistrement...");
    try {
        await fetchJson(`/api/lora-tag-mappings/${mappingId}`, {
            method: "PATCH",
            body: JSON.stringify({ tag_names: tagNames }),
        });
        state.loraTagEditingId = null;
        await loadLoraTagMappings();
        setLoraTagMappingStatus("Affectation modifiée. Elle sera appliquée au prochain Scan JSON.");
    } catch (error) {
        setLoraTagMappingStatus(error.message, true);
    } finally {
        done();
    }
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

function savedScanOptions() {
    try {
        return JSON.parse(window.localStorage.getItem(SCAN_OPTIONS_STORAGE_KEY) || "{}");
    } catch (_error) {
        return {};
    }
}

function setScanOptionsStatus(message, isError = false) {
    const status = $("#scan-options-status");
    if (!status) {
        return;
    }
    status.textContent = message || "";
    status.classList.toggle("error", Boolean(isError));
}

function updateScanForceOptionVisibility() {
    const fullScan = $('[name="scan-existing-mode"]:checked')?.value === "full";
    const faces = $("#scan-options-faces")?.checked;
    const row = $("#scan-options-force-faces-row");
    if (row) {
        row.hidden = !(fullScan && faces);
    }
}

function updateScanScopeControls() {
    const scope = $("#scan-options-scope");
    const selectionOption = $("#scan-options-selection-scope");
    if (!scope || !selectionOption) {
        return;
    }
    const selectionCount = state.selectedPhotoIds.size;
    selectionOption.textContent = `S\u00e9lection (${selectionCount} image${selectionCount > 1 ? "s" : ""})`;
    selectionOption.disabled = selectionCount === 0;
    if (!selectionCount && scope.value === "selection") {
        scope.value = state.selectedAlbum?.name ? "current" : "all";
    }
    const selectionScope = scope.value === "selection";
    const incremental = $('[name="scan-existing-mode"][value="incremental"]');
    if (incremental) {
        incremental.disabled = selectionScope;
        if (selectionScope && incremental.checked) {
            const missing = $('[name="scan-existing-mode"][value="missing"]');
            if (missing) {
                missing.checked = true;
            }
        }
    }
    updateScanForceOptionVisibility();
}

function openScanOptionsModal() {
    const modal = $("#scan-options-modal");
    if (!modal || $("#scan-button")?.disabled) {
        return;
    }
    const saved = savedScanOptions();
    const scope = $("#scan-options-scope");
    const currentAvailable = Boolean(state.selectedAlbum?.name);
    const selectionAvailable = state.selectedPhotoIds.size > 0;
    if (saved.scope === "selection" && selectionAvailable) {
        scope.value = "selection";
    } else {
        scope.value = saved.scope === "all" || !currentAvailable ? "all" : "current";
    }
    const mode = ["incremental", "missing", "full"].includes(saved.scan_mode)
        ? saved.scan_mode
        : saved.rescan_existing ? "full" : "incremental";
    const modeInput = $(`[name="scan-existing-mode"][value="${mode}"]`);
    if (modeInput) {
        modeInput.checked = true;
    }
    $("#scan-options-metadata").checked = Boolean(saved.metadata);
    $("#scan-options-faces").checked = Boolean(saved.face_recognition);
    $("#scan-options-force-faces").checked = Boolean(saved.force_face_rescan);
    $("#scan-options-image-analysis").checked = Boolean(saved.image_analysis);
    updateScanScopeControls();
    setScanOptionsStatus("");
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    scope.focus();
}

function closeScanOptionsModal() {
    const modal = $("#scan-options-modal");
    modal?.classList.remove("open");
    modal?.setAttribute("aria-hidden", "true");
}

function scanOptionsFromForm() {
    const scanMode = $('[name="scan-existing-mode"]:checked')?.value || "incremental";
    const faceRecognition = $("#scan-options-faces").checked;
    const scope = $("#scan-options-scope").value;
    return {
        scope,
        album: scope === "current" ? state.selectedAlbum?.name || null : null,
        photo_ids: scope === "selection" ? selectedPhotoIds() : undefined,
        scan_mode: scanMode,
        rescan_existing: scanMode === "full",
        metadata: $("#scan-options-metadata").checked,
        face_recognition: faceRecognition,
        force_face_rescan: Boolean(faceRecognition && scanMode === "full" && $("#scan-options-force-faces").checked),
        image_analysis: $("#scan-options-image-analysis").checked,
    };
}

async function submitScanOptions(event) {
    event.preventDefault();
    const options = scanOptionsFromForm();
    const done = setBusy($("#scan-options-submit"), "Lancement…", { spinner: true });
    setScanOptionsStatus("");
    try {
        state.scanStatusClosed = false;
        const data = await fetchJson("/api/scan", {
            method: "POST",
            body: JSON.stringify(options),
        });
        if (data.already_running) {
            state.scanJob = data.job;
            renderScanStatus(data.job, { force: true });
            startScanPolling();
            setScanOptionsStatus("Un scan est déjà en cours.", true);
            return;
        }
        try {
            window.localStorage.setItem(SCAN_OPTIONS_STORAGE_KEY, JSON.stringify(options));
        } catch (_error) {
            // A disabled or full local storage must not hide a successfully started job.
        }
        closeScanOptionsModal();
        renderScanStatus(data.job, { force: true });
        startScanPolling();
    } catch (error) {
        setScanOptionsStatus(error.message, true);
    } finally {
        done();
    }
}

function startScanPolling() {
    window.clearInterval(state.scanPollTimer);
    if (!state.scanBusyDone) {
        state.scanBusyDone = setBusy($("#scan-button"), "…");
    }
    state.scanPollTimer = window.setInterval(async () => {
        try {
            const data = await fetchJson("/api/scan/status");
            renderScanStatus(data.job, { force: true });
            if (!data.job.active) {
                window.clearInterval(state.scanPollTimer);
                state.scanBusyDone?.();
                state.scanBusyDone = null;
                if (data.job.state === "done") {
                    window.location.reload();
                }
            }
        } catch (error) {
            window.clearInterval(state.scanPollTimer);
            state.scanBusyDone?.();
            state.scanBusyDone = null;
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
    state.scanJob = job;
    box.hidden = false;
    box.dataset.state = job.state || "running";
    $("#scan-status-title").textContent = job.state === "done" ? "Scan terminé"
        : job.state === "error" ? "Scan en erreur"
        : job.state === "cancelled" ? "Scan annulé"
        : job.state === "cancel_requested" ? "Arrêt du scan"
        : job.stage === "faces" ? "Reconnaissance faciale"
        : job.stage === "metadata" ? "Scan JSON"
        : job.stage === "image_analysis" ? "Analyse d'image"
        : "Scan en cours";
    $("#scan-status-message").textContent = job.message || "Scan...";
    $("#scan-status-close").hidden = Boolean(job.active);
    const cancel = $("#scan-status-cancel");
    cancel.hidden = !job.active;
    cancel.disabled = job.state === "cancel_requested";
    cancel.textContent = job.state === "cancel_requested" ? "Arrêt…" : "Arrêter";
    const details = [];
    if (job.stage) {
        details.push(`étape: ${job.stage === "faces" ? "visages" : job.stage === "image_analysis" ? "analyse image" : job.stage}`);
    }
    if (job.album) {
        details.push(`album: ${job.album}`);
    }
    if (job.file) {
        details.push(`fichier: ${job.file}`);
    }
    if (job.processed || job.skipped) {
        details.push(`traitées: ${job.processed || 0} · ignorées: ${job.skipped || 0}`);
    } else if (job.album_photos || job.photos) {
        details.push(`images: ${job.album_photos || 0} album / ${job.photos || 0} total`);
    }
    if (job.stage === "image_analysis" && job.analysis_total) {
        details.push(
            `analyse: ${job.analysis_processed || 0} traitée(s), ${job.analysis_skipped || 0} en cache, ${job.analysis_errors || 0} erreur(s)`
        );
    }
    if (job.stage === "metadata" && job.metadata_total) {
        details.push(
            `JSON: ${job.metadata_processed || 0} traitee(s), ${job.metadata_skipped || 0} en cache, ${job.metadata_errors || 0} erreur(s)`
        );
    }
    if (job.face_job) {
        details.push(
            `visages: ${job.face_job.processed || 0}/${job.face_total || job.face_job.total || 0}`
            + (job.face_skipped ? `, ${job.face_skipped} en cache` : "")
        );
    }
    if (!job.active && job.summary) {
        const metadata = job.summary.metadata;
        const imageAnalysis = job.summary.image_analysis;
        const faces = job.summary.faces;
        if (metadata?.total) {
            details.push(`JSON: ${metadata.processed} executee(s), ${metadata.skipped} en cache, ${metadata.errors} erreur(s)`);
        }
        if (imageAnalysis?.total) {
            details.push(`IA: ${imageAnalysis.processed} executee(s), ${imageAnalysis.skipped} en cache, ${imageAnalysis.errors} erreur(s)`);
        }
        if (faces?.total) {
            details.push(`visages: ${faces.processed} executee(s), ${faces.skipped} en cache, ${faces.errors} erreur(s)`);
        }
    }
    if (job.errors && job.errors.length) {
        details.push(`erreurs: ${job.errors.length}`);
    }
    $("#scan-status-detail").textContent = details.join(" | ");
}

async function cancelScanJob() {
    const job = state.scanJob;
    if (!job?.active || job.state === "cancel_requested") {
        return;
    }
    try {
        const data = await fetchJson(`/api/scan/jobs/${job.job_id}/cancel`, {
            method: "POST",
            body: "{}",
        });
        renderScanStatus(data.job, { force: true });
    } catch (error) {
        alert(error.message);
    }
}

async function resumeScanStatusIfNeeded() {
    try {
        const data = await fetchJson("/api/scan/status");
        state.scanJob = data.job;
        if (data.job.active) {
            renderScanStatus(data.job, { force: true });
            startScanPolling();
        }
    } catch (_error) {
        // Scan status is non-critical during initial page load.
    }
}

function handleComfyReferencePointerMove(event) {
    if (state.comfyReferencePointerId !== event.pointerId || state.comfyReferenceDragIndex === null) {
        return;
    }
    const card = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-comfy-reference-index]");
    if (!card) {
        return;
    }
    const targetIndex = Number(card.dataset.comfyReferenceIndex);
    if (targetIndex !== state.comfyReferenceDragIndex) {
        const previous = state.comfyReferenceDragIndex;
        state.comfyReferenceDragIndex = targetIndex;
        moveComfyReference(previous, targetIndex);
    }
}

function endComfyReferencePointer(event) {
    if (state.comfyReferencePointerId === event.pointerId) {
        state.comfyReferencePointerId = null;
        state.comfyReferenceDragIndex = null;
    }
}

function navigateGallery({ album, maxSensitivity } = {}) {
    const url = new URL(window.location.href);
    if (album) {
        url.searchParams.set("album", album);
    }
    if (maxSensitivity) {
        url.searchParams.set("max_sensitivity", maxSensitivity);
    }
    url.searchParams.set("page", "1");
    window.location.href = url.toString();
}

function selectGallerySensitivity(event) {
    const button = event.target.closest("[data-gallery-sensitivity]");
    if (!button) {
        return;
    }
    const sensitivity = button.dataset.gallerySensitivity;
    document.cookie = `gallery_max_sensitivity=${encodeURIComponent(sensitivity)}; Max-Age=31536000; Path=/; SameSite=Lax`;
    navigateGallery({ maxSensitivity: sensitivity });
}

function bindEvents() {
    $("#album-select")?.addEventListener("change", (event) => {
        navigateGallery({ album: event.target.value });
    });
    $("#gallery-sensitivity-selector")?.addEventListener("click", selectGallerySensitivity);
    $("#filter-button")?.addEventListener("click", openTagFilter);
    $("#scan-button")?.addEventListener("click", openScanOptionsModal);
    $("#scan-options-form")?.addEventListener("submit", submitScanOptions);
    $$("[data-close-scan-options]").forEach((button) => button.addEventListener("click", closeScanOptionsModal));
    $$('[name="scan-existing-mode"]').forEach((input) => input.addEventListener("change", updateScanForceOptionVisibility));
    $("#scan-options-scope")?.addEventListener("change", updateScanScopeControls);
    $("#scan-options-faces")?.addEventListener("change", updateScanForceOptionVisibility);
    $("#scan-status-cancel")?.addEventListener("click", cancelScanJob);
    $("#scan-status-close")?.addEventListener("click", () => {
        state.scanStatusClosed = true;
        $("#scan-status").hidden = true;
    });
    $("#admin-button")?.addEventListener("click", () => openAdmin());
    $("#selection-actions-button")?.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleSelectionActionsMenu();
    });
    $("#rescan-metadata-button")?.addEventListener("click", rescanCurrentMetadata);
    $("#rescan-image-analysis-button")?.addEventListener("click", rescanCurrentImageAnalysis);
    $("#refresh-thumbnail-button")?.addEventListener("click", refreshCurrentThumbnail);
    $("#scan-photo-faces-button")?.addEventListener("click", scanCurrentPhotoFaces);
    $("#face-automatic-scan")?.addEventListener("change", setAutomaticFaceScan);
    $("#scan-album-faces-button")?.addEventListener("click", () => {
        if (state.selectedAlbum?.name) {
            startFaceJob("album", { album_name: state.selectedAlbum.name }).catch((error) => alert(error.message));
        }
    });
    $("#scan-all-faces-button")?.addEventListener("click", () => {
        startFaceJob("all").catch((error) => alert(error.message));
    });
    $("#cancel-face-job-button")?.addEventListener("click", (event) => {
        cancelFaceJob(event.currentTarget.dataset.jobId).catch((error) => alert(error.message));
    });
    $("#face-job-status-cancel")?.addEventListener("click", (event) => {
        cancelFaceJob(event.currentTarget.dataset.jobId).catch((error) => alert(error.message));
    });
    $("#resume-face-job-button")?.addEventListener("click", (event) => {
        resumeFaceJob(event.currentTarget.dataset.jobId).catch((error) => alert(error.message));
    });
    $("#face-job-status-close")?.addEventListener("click", () => {
        state.faceJobStatusClosed = true;
        $("#face-job-status").hidden = true;
    });
    $("#face-identity-create-form")?.addEventListener("submit", (event) => {
        createFaceIdentity(event).catch((error) => alert(error.message));
    });
    $("#face-reference-import-form")?.addEventListener("submit", importFaceReference);
    $("#comfy-generate-button")?.addEventListener("click", openComfyModal);
    $("#comfy-job-reopen-button")?.addEventListener("click", () => {
        reopenComfyJob().catch((error) => setComfyStatus(error.message, true));
    });
    $("#comfy-cancel-button")?.addEventListener("click", cancelComfyGeneration);
    $("#delete-photo-button")?.addEventListener("click", deleteCurrentPhoto);
    $("#photo-actions-button")?.addEventListener("click", (event) => {
        event.stopPropagation();
        togglePhotoActionsMenu();
    });
    $("#album-action-form")?.addEventListener("submit", submitAlbumAction);
    $("#comfy-form")?.addEventListener("submit", submitComfyGeneration);
    $("#comfy-loras")?.addEventListener("focusout", (event) => {
        if (event.target.matches("[data-comfy-lora-strength]")) {
            event.target.value = formatComfyLoraStrength(event.target.value);
        }
    });
    $("#comfy-references")?.addEventListener("input", (event) => {
        if (event.target.matches("[data-comfy-ref-search]")) {
            debouncedComfyReferenceSearch(event);
        }
    });
    $("#comfy-references")?.addEventListener("change", (event) => {
        const card = event.target.closest("[data-comfy-reference-index]");
        if (card && event.target.matches("[data-comfy-ref-enabled]")) {
            state.comfyReferences[Number(card.dataset.comfyReferenceIndex)].enabled = event.target.checked;
            card.classList.toggle("disabled", !event.target.checked);
        }
        if (event.target.matches("[data-comfy-ref-upload]")) {
            uploadComfyReference(event.target).catch((error) => setComfyStatus(error.message, true));
        }
    });
    $("#comfy-references")?.addEventListener("dragstart", (event) => {
        const card = event.target.closest("[data-comfy-reference-index]");
        if (card) {
            state.comfyReferenceDragIndex = Number(card.dataset.comfyReferenceIndex);
            event.dataTransfer.effectAllowed = "move";
        }
    });
    $("#comfy-references")?.addEventListener("dragover", (event) => {
        if (event.target.closest("[data-comfy-reference-index]")) {
            event.preventDefault();
        }
    });
    $("#comfy-references")?.addEventListener("drop", (event) => {
        const card = event.target.closest("[data-comfy-reference-index]");
        if (card && state.comfyReferenceDragIndex !== null) {
            event.preventDefault();
            moveComfyReference(state.comfyReferenceDragIndex, Number(card.dataset.comfyReferenceIndex));
        }
        state.comfyReferenceDragIndex = null;
    });
    $("#comfy-references")?.addEventListener("pointerdown", (event) => {
        const handle = event.target.closest("[data-comfy-ref-drag]");
        const card = handle?.closest("[data-comfy-reference-index]");
        if (card) {
            event.preventDefault();
            state.comfyReferencePointerId = event.pointerId;
            state.comfyReferenceDragIndex = Number(card.dataset.comfyReferenceIndex);
        }
    });
    document.addEventListener("pointermove", handleComfyReferencePointerMove);
    document.addEventListener("pointerup", endComfyReferencePointer);
    document.addEventListener("pointercancel", endComfyReferencePointer);
    $("#save-photo-tags-button")?.addEventListener("click", savePhotoTags);
    $("#batch-tag-form")?.addEventListener("submit", submitBatchTags);
    $("#link-search-input")?.addEventListener("input", debounce(searchLinkTargets, 250));
    $("#prev-button")?.addEventListener("click", () => navigate(-1));
    $("#next-button")?.addEventListener("click", () => navigate(1));
    $("#play-button")?.addEventListener("click", toggleSlideshow);
    $("#details-toggle-button")?.addEventListener("click", toggleDetailsPanel);
    $("#details-sheet-handle")?.addEventListener("click", handleDetailsSheetClick);
    $("#details-sheet-handle")?.addEventListener("keydown", handleDetailsSheetKeydown);
    $$("#details-sheet-handle, .details-sheet-header").forEach((dragTarget) => {
        dragTarget.addEventListener("pointerdown", handleDetailsSheetPointerDown);
    });
    document.addEventListener("pointermove", handleDetailsSheetPointerMove, { passive: false });
    document.addEventListener("pointerup", (event) => finishDetailsSheetPointer(event));
    document.addEventListener("pointercancel", (event) => finishDetailsSheetPointer(event, true));
    MOBILE_DETAILS_MEDIA.addEventListener("change", handleDetailsSheetBreakpointChange);
    const viewerStage = $(".viewer-stage");
    viewerStage?.addEventListener("pointerenter", showLinkedStripTemporarily);
    viewerStage?.addEventListener("pointermove", showLinkedStripTemporarily);
    viewerStage?.addEventListener("pointerdown", showLinkedStripTemporarily);
    viewerStage?.addEventListener("touchstart", handleViewerTouchStart, { passive: true });
    viewerStage?.addEventListener("touchmove", handleViewerTouchMove, { passive: false });
    viewerStage?.addEventListener("touchend", handleViewerTouchEnd, { passive: true });
    viewerStage?.addEventListener("touchcancel", handleViewerTouchCancel, { passive: true });
    $$("[data-close-modal]").forEach((button) => button.addEventListener("click", closePhotoModal));
    $$("[data-close-admin]").forEach((button) => button.addEventListener("click", closeAdmin));
    $$("[data-close-comfy]").forEach((button) => button.addEventListener("click", closeComfyModal));
    $$("[data-close-album-action]").forEach((button) => button.addEventListener("click", closeAlbumActionModal));
    $$("[data-close-batch-tags]").forEach((button) => button.addEventListener("click", closeBatchTagModal));

    const gallery = $("#gallery-list");
    gallery?.addEventListener("pointerdown", handleGalleryPointerDown);
    gallery?.addEventListener("pointermove", handleGalleryPointerMove);
    gallery?.addEventListener("pointerup", handleGalleryPointerEnd);
    gallery?.addEventListener("pointercancel", handleGalleryPointerEnd);
    gallery?.addEventListener("pointerleave", handleGalleryPointerEnd);
    gallery?.addEventListener("click", (event) => {
        const checkbox = event.target.closest("[data-select-photo-id]");
        if (checkbox) {
            togglePhotoSelection(Number(checkbox.dataset.selectPhotoId));
            return;
        }
        const button = event.target.closest("[data-photo-id]");
        if (button) {
            const photoId = Number(button.dataset.photoId);
            if (state.suppressPhotoClickId === photoId) {
                state.suppressPhotoClickId = null;
                event.preventDefault();
                return;
            }
            if (state.selectionMode) {
                event.preventDefault();
                togglePhotoSelection(photoId);
                return;
            }
            openPhoto(photoId).catch((error) => alert(error.message));
        }
    });

    document.body.addEventListener("click", (event) => {
        if (!event.target.closest(".viewer-menu")) {
            closePhotoActionsMenu();
        }
        if (!event.target.closest(".selection-actions")) {
            closeSelectionActionsMenu();
        }
        const batchAction = event.target.closest("[data-batch-action]");
        if (batchAction) {
            handleBatchAction(batchAction.dataset.batchAction);
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
        const addReference = event.target.closest("[data-comfy-ref-add]");
        if (addReference) {
            const shouldOpen = !state.comfyReferenceAddOpen || state.comfyReferenceTarget !== null;
            setComfyReferenceAddOpen(shouldOpen);
        }
        const changeReference = event.target.closest("[data-comfy-ref-change]");
        if (changeReference) {
            const card = changeReference.closest("[data-comfy-reference-index]");
            setComfyReferenceAddOpen(true, Number(card.dataset.comfyReferenceIndex));
        }
        const removeReference = event.target.closest("[data-comfy-ref-remove]");
        if (removeReference) {
            const card = removeReference.closest("[data-comfy-reference-index]");
            state.comfyReferences.splice(Number(card.dataset.comfyReferenceIndex), 1);
            state.comfyReferenceAddOpen = false;
            state.comfyReferenceTarget = null;
            renderComfyReferences();
        }
        if (event.target.closest("[data-add-comfy-lora]")) {
            addComfyLora();
        }
        const removeLora = event.target.closest("[data-remove-comfy-lora]");
        if (removeLora) {
            removeLora.closest(".comfy-lora-row")?.remove();
            if (!$(".comfy-lora-row")) {
                $("#comfy-loras [data-comfy-lora-rows]").innerHTML = '<span class="muted">Aucun LoRA dans ce workflow</span>';
            }
        }
        const faceDecision = event.target.closest("[data-face-decision]");
        if (faceDecision) {
            const card = faceDecision.closest("[data-face-id]");
            decideFace(
                Number(card.dataset.faceId),
                Number(faceDecision.dataset.identityId),
                faceDecision.dataset.faceDecision,
            ).catch((error) => alert(error.message));
        }
        const faceReferenceToggle = event.target.closest("[data-toggle-face-references]");
        if (faceReferenceToggle) {
            const card = faceReferenceToggle.closest("[data-face-id]");
            const panel = card?.querySelector(".face-reference-panel");
            if (panel) {
                const expanded = faceReferenceToggle.getAttribute("aria-expanded") !== "true";
                faceReferenceToggle.setAttribute("aria-expanded", String(expanded));
                faceReferenceToggle.setAttribute("title", expanded ? "Masquer les references" : "Afficher les references");
                faceReferenceToggle.setAttribute("aria-label", expanded ? "Masquer les references" : "Afficher les references");
                panel.hidden = !expanded;
            }
        }
        const galleryReference = event.target.closest("[data-add-gallery-reference]");
        if (galleryReference) {
            addGalleryFaceReference(galleryReference.closest("[data-face-id]")).catch((error) => alert(error.message));
        }
        const importedFace = event.target.closest("[data-add-imported-face]");
        if (importedFace) {
            addImportedFaceReference(Number(importedFace.dataset.addImportedFace)).catch((error) => alert(error.message));
        }
        const deleteIdentity = event.target.closest("[data-delete-face-identity]");
        if (deleteIdentity) {
            deleteFaceIdentity(Number(deleteIdentity.dataset.deleteFaceIdentity)).catch((error) => alert(error.message));
        }
        const deleteReference = event.target.closest("[data-delete-face-reference]");
        if (deleteReference) {
            deleteFaceReference(Number(deleteReference.dataset.deleteFaceReference)).catch((error) => alert(error.message));
        }
    });

    $("#album-admin-list")?.addEventListener("submit", (event) => {
        if (event.target.matches(".album-admin-card")) {
            saveAlbum(event).catch((error) => alert(error.message));
        }
    });
    $(".config-sidebar")?.addEventListener("click", (event) => {
        const button = event.target.closest("[data-config-section]");
        if (button) {
            switchConfigSection(button.dataset.configSection).catch((error) => alert(error.message));
        }
    });
    $("#lora-tag-mapping-form")?.addEventListener("submit", createLoraTagMapping);
    $("#lora-tag-mapping-list")?.addEventListener("click", (event) => {
        const editButton = event.target.closest("[data-start-lora-tag-edit]");
        if (editButton) {
            startLoraTagMappingEdit(Number(editButton.dataset.startLoraTagEdit));
            return;
        }
        if (event.target.closest("[data-cancel-lora-tag-edit]")) {
            cancelLoraTagMappingEdit();
            return;
        }
        const deleteButton = event.target.closest("[data-delete-lora-tag-mapping]");
        if (deleteButton) {
            deleteLoraTagMapping(Number(deleteButton.dataset.deleteLoraTagMapping));
        }
    });
    $("#lora-tag-mapping-list")?.addEventListener("submit", (event) => {
        if (event.target.matches("[data-edit-lora-tag-mapping]")) {
            updateLoraTagMapping(event);
        }
    });
    $("#tag-sensitivity-search")?.addEventListener("input", (event) => {
        state.tagSensitivityQuery = event.target.value;
        renderTagSensitivities();
    });
    $("#tag-sensitivity-list")?.addEventListener("click", (event) => {
        const button = event.target.closest("[data-sensitivity-value]");
        if (button) {
            updateTagSensitivity(button).catch((error) => {
                $("#tag-sensitivity-status").textContent = error.message;
                $("#tag-sensitivity-status").classList.add("error");
            });
        }
    });
    $("#tag-sensitivity-list")?.addEventListener("change", (event) => {
        if (event.target.matches("[data-tag-category]")) {
            updateTagCategory(event.target).catch((error) => {
                $("#tag-sensitivity-status").textContent = error.message;
                $("#tag-sensitivity-status").classList.add("error");
            });
        }
    });
    $("#tag-filter-search")?.addEventListener("input", (event) => {
        state.tagFilterQuery = event.target.value;
        renderTagFilters();
    });
    $("#tag-filter-list")?.addEventListener("click", chooseTagFilter);
    $("#tag-filter-active-list")?.addEventListener("click", chooseTagFilter);
    $$('[data-close-tag-filter]').forEach((button) => button.addEventListener("click", closeTagFilter));
    $("#face-identity-list")?.addEventListener("submit", (event) => {
        if (event.target.matches(".face-identity-card")) {
            saveFaceIdentity(event).catch((error) => alert(error.message));
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            if ($("#scan-options-modal")?.classList.contains("open")) {
                closeScanOptionsModal();
                return;
            }
            if (!$("#selection-actions-menu")?.hidden) {
                closeSelectionActionsMenu();
                return;
            }
            if ($("#batch-tag-modal")?.classList.contains("open")) {
                closeBatchTagModal();
                return;
            }
            if ($("#album-action-modal")?.classList.contains("open")) {
                closeAlbumActionModal();
                return;
            }
            if (state.selectionMode) {
                exitSelectionMode();
                return;
            }
            closePhotoActionsMenu();
            closePhotoModal();
            closeAdmin();
            closeComfyModal();
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

async function resumeFaceRecognitionState() {
    try {
        await loadFaceIdentities();
        const data = await refreshFaceStatus();
        if (data.job?.active) {
            renderFaceJobStatus(data.job, { force: true });
            pollFaceJob(data.job.id);
        }
    } catch (_error) {
        // Face recognition remains optional until its local model is configured.
    }
}

function debounce(fn, delay) {
    let timer = null;
    return (...args) => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => fn(...args), delay);
    };
}

bindEvents();
renderSelectionState();
resumeScanStatusIfNeeded();
refreshComfyStatus();
resumeComfyGenerationState();
resumeFaceRecognitionState();
