import { getDocument, GlobalWorkerOptions } from "/static/vendor/pdf.min.mjs";

GlobalWorkerOptions.workerSrc = "/static/vendor/pdf.worker.min.mjs";

const params = new URLSearchParams(window.location.search);
const token = params.get("token") || "";
const viewer = document.querySelector("#viewer");
const statusElement = document.querySelector("#status");
const pageNumber = document.querySelector("#page-number");
const pageCount = document.querySelector("#page-count");
const zoomLabel = document.querySelector("#zoom-label");
const help = document.querySelector("#help");
const searchPanel = document.querySelector("#search-panel");
const searchInput = document.querySelector("#search-input");
const searchStatus = document.querySelector("#search-status");

let documentHandle = null;
let loadingTask = null;
let revision = 0;
let loadedRevision = -1;
let scale = 1.25;
let scaleMode = "width";
let renderGeneration = 0;
let pendingForward = null;
let loading = false;
let searchResults = [];
let searchIndex = -1;
let searchActive = false;
let searchGeneration = 0;
let lastSearch = "";
const pageViewports = new Map();
const measureContext = document.createElement("canvas").getContext("2d");
const destinationCache = new Map();
const backPositions = [];
const forwardPositions = [];
let pendingG = false;
let pendingGTimer = null;
let events = null;
let lastEventAt = 0;
let reconnectTimer = null;
let reconnectDelay = 1000;
// The agent pings every 4 s; a much longer silence means the connection is
// dead even when the browser has not noticed yet.
const HEARTBEAT_TIMEOUT = 20000;

function apiUrl(path) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("token", token);
  return url.toString();
}

function setStatus(message, isError = false) {
  statusElement.textContent = message;
  statusElement.classList.toggle("error", isError);
}

function currentPage() {
  const toolbarBottom = document.querySelector("#toolbar").getBoundingClientRect().bottom;
  let closest = 1;
  let distance = Number.POSITIVE_INFINITY;
  for (const page of document.querySelectorAll(".page")) {
    const delta = Math.abs(page.getBoundingClientRect().top - toolbarBottom - 8);
    if (delta < distance) {
      closest = Number(page.dataset.page);
      distance = delta;
    }
  }
  return closest;
}

function captureAnchor() {
  const page = document.querySelector(`.page[data-page="${currentPage()}"]`);
  if (!page) return { page: 1, ratio: 0 };
  const top = page.getBoundingClientRect().top + window.scrollY;
  return {
    page: Number(page.dataset.page),
    ratio: Math.max(0, (window.scrollY + 50 - top) / Math.max(page.offsetHeight, 1)),
  };
}

function restoreAnchor(anchor) {
  const page = document.querySelector(`.page[data-page="${anchor.page}"]`);
  if (!page) return;
  const top = page.getBoundingClientRect().top + window.scrollY;
  window.scrollTo({ top: top + anchor.ratio * page.offsetHeight - 50 });
}

async function calculateScale(pdf) {
  if (scaleMode === "manual") return scale;
  const first = await pdf.getPage(1);
  const base = first.getViewport({ scale: 1 });
  const widthScale = Math.max(0.25, (window.innerWidth - 48) / base.width);
  if (scaleMode === "page") {
    const heightScale = Math.max(0.25, (window.innerHeight - 78) / base.height);
    return Math.min(widthScale, heightScale);
  }
  return widthScale;
}

async function renderPage(pdf, number, generation) {
  const page = await pdf.getPage(number);
  if (generation !== renderGeneration) return;
  const viewport = page.getViewport({ scale });
  const outputScale = window.devicePixelRatio || 1;
  const wrapper = document.createElement("section");
  wrapper.className = "page";
  wrapper.dataset.page = String(number);
  wrapper.dataset.scale = String(scale);
  wrapper.style.width = `${viewport.width}px`;
  wrapper.style.height = `${viewport.height}px`;
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(viewport.width * outputScale);
  canvas.height = Math.floor(viewport.height * outputScale);
  canvas.style.width = `${viewport.width}px`;
  canvas.style.height = `${viewport.height}px`;
  wrapper.addEventListener("click", inverseSearch);
  wrapper.append(canvas);
  viewer.append(wrapper);
  pageViewports.set(number, viewport);
  const context = canvas.getContext("2d", { alpha: false });
  const rendering = page.render({
    canvasContext: context,
    viewport,
    transform: outputScale === 1 ? null : [outputScale, 0, 0, outputScale, 0, 0],
  }).promise;
  // Links are a convenience; a broken annotation must not block the page.
  const links = addLinks(pdf, page, wrapper, viewport).catch(() => {});
  await rendering;
  await links;
}

async function addLinks(pdf, page, wrapper, viewport) {
  const annotations = await page.getAnnotations({ intent: "display" });
  for (const annotation of annotations) {
    if (annotation.subtype !== "Link" || !(annotation.url || annotation.dest)) continue;
    const [x1, y1] = viewport.convertToViewportPoint(annotation.rect[0], annotation.rect[1]);
    const [x2, y2] = viewport.convertToViewportPoint(annotation.rect[2], annotation.rect[3]);
    const anchor = document.createElement("a");
    anchor.className = "link";
    anchor.style.left = `${Math.min(x1, x2)}px`;
    anchor.style.top = `${Math.min(y1, y2)}px`;
    anchor.style.width = `${Math.abs(x2 - x1)}px`;
    anchor.style.height = `${Math.abs(y2 - y1)}px`;
    if (annotation.url) {
      anchor.href = annotation.url;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      anchor.title = annotation.url;
    } else {
      anchor.href = "#";
      if (typeof annotation.dest === "string") anchor.title = annotation.dest;
      anchor.addEventListener("click", (event) => {
        // Ctrl-click stays inverse SyncTeX; the page handler takes it.
        if (event.ctrlKey) return;
        event.preventDefault();
        void followLink(pdf, annotation.dest);
      });
    }
    wrapper.append(anchor);
  }
}

async function resolveDestination(pdf, dest) {
  const key = typeof dest === "string" ? dest : JSON.stringify(dest);
  if (destinationCache.has(key)) return destinationCache.get(key);
  const explicit = typeof dest === "string" ? await pdf.getDestination(dest) : dest;
  let target = null;
  if (Array.isArray(explicit) && explicit.length) {
    const [ref, kind, ...args] = explicit;
    const index = ref !== null && typeof ref === "object" ? await pdf.getPageIndex(ref) : Number(ref);
    let top = null;
    if (kind?.name === "XYZ") top = args[1];
    else if (kind?.name === "FitH" || kind?.name === "FitBH") top = args[0];
    else if (kind?.name === "FitR") top = args[3];
    target = { page: index + 1, top: typeof top === "number" ? top : null };
  }
  destinationCache.set(key, target);
  return target;
}

async function followLink(pdf, dest) {
  if (pdf !== documentHandle) return;
  try {
    const target = await resolveDestination(pdf, dest);
    if (!target) {
      setStatus("Link target not found", true);
      return;
    }
    backPositions.push(captureAnchor());
    forwardPositions.length = 0;
    scrollToDestination(target);
    setStatus(`Page ${target.page} (Ctrl-o goes back)`);
  } catch (error) {
    setStatus(String(error?.message || error), true);
  }
}

function scrollToDestination({ page: number, top }) {
  const page = document.querySelector(`.page[data-page="${number}"]`);
  const viewport = pageViewports.get(number);
  if (!page || !viewport) {
    scrollToPage(number);
    return;
  }
  const offset = typeof top === "number" ? Math.max(0, viewport.convertToViewportPoint(0, top)[1]) : 0;
  const pageTop = page.getBoundingClientRect().top + window.scrollY;
  const toolbarBottom = document.querySelector("#toolbar").getBoundingClientRect().bottom;
  window.scrollTo({ top: pageTop + offset - toolbarBottom - 8 });
  pageNumber.value = String(number);
}

function goBack() {
  if (!backPositions.length) {
    setStatus("No earlier position");
    return;
  }
  forwardPositions.push(captureAnchor());
  restoreAnchor(backPositions.pop());
  updateCurrentPage();
}

function goForward() {
  if (!forwardPositions.length) {
    setStatus("No later position");
    return;
  }
  backPositions.push(captureAnchor());
  restoreAnchor(forwardPositions.pop());
  updateCurrentPage();
}

async function loadPdf(nextRevision = revision, preservePosition = true) {
  const generation = ++renderGeneration;
  const anchor = preservePosition ? captureAnchor() : { page: 1, ratio: 0 };
  revision = nextRevision;
  loading = true;
  setStatus("Loading PDF…");
  try {
    const previous = loadingTask;
    loadingTask = null;
    // The old task is unusable either way; a failure while tearing it down
    // must not keep every later reload from starting.
    if (previous) await previous.destroy().catch(() => {});
    // A reload requested while the teardown was pending owns the next load.
    if (generation !== renderGeneration) return;
    loadingTask = getDocument({
      url: apiUrl(`/document.pdf?revision=${revision}`),
      cMapUrl: "/static/cmaps/",
      cMapPacked: true,
    });
    const pdf = await loadingTask.promise;
    if (generation !== renderGeneration) return;
    documentHandle = pdf;
    destinationCache.clear();
    scale = await calculateScale(pdf);
    zoomLabel.textContent = `${Math.round(scale * 100)}%`;
    pageCount.textContent = String(pdf.numPages);
    pageViewports.clear();
    viewer.replaceChildren();
    for (let number = 1; number <= pdf.numPages; number += 1) {
      await renderPage(pdf, number, generation);
      if (generation !== renderGeneration) return;
    }
    restoreAnchor(anchor);
    updateCurrentPage();
    loadedRevision = nextRevision;
    setStatus("Ready");
    loading = false;
    if (pendingForward) showForward(pendingForward);
    if (searchActive && lastSearch) void runSearch(lastSearch, { jump: false });
  } catch (error) {
    if (generation !== renderGeneration) return;
    loading = false;
    const message = String(error?.message || error);
    if (message.includes("404") || message.includes("Missing PDF")) {
      showWaiting();
    } else {
      setStatus(message, true);
    }
  }
}

function showWaiting() {
  documentHandle = null;
  pageCount.textContent = "0";
  viewer.innerHTML = `
    <section id="empty-state">
      <div class="spinner"></div>
      <h1>Waiting for the first build</h1>
      <p>Paperhere will load the PDF after compilation succeeds.</p>
    </section>`;
  setStatus("Waiting for PDF");
}

function scrollToPage(number, behavior = "instant") {
  if (!documentHandle) return;
  const bounded = Math.max(1, Math.min(documentHandle.numPages, Number(number) || 1));
  const page = document.querySelector(`.page[data-page="${bounded}"]`);
  page?.scrollIntoView({ behavior, block: "start" });
  pageNumber.value = String(bounded);
}

function showForward(position) {
  const page = document.querySelector(`.page[data-page="${position.page}"]`);
  // Keep the request only until the document has finished rendering, so a
  // jump requested during a load still happens. Later reloads restore the
  // reading position instead of replaying the last forward search.
  pendingForward = !page || loading ? position : null;
  if (!page) return;
  document.querySelector(".synctex-highlight")?.remove();
  const pageScale = Number(page.dataset.scale);
  const marker = document.createElement("div");
  marker.className = "synctex-highlight";
  marker.style.left = `${position.h * pageScale}px`;
  marker.style.top = `${Math.max(0, position.v - Math.abs(position.height)) * pageScale}px`;
  marker.style.width = `${Math.max(8, position.width * pageScale)}px`;
  marker.style.height = `${Math.max(8, Math.abs(position.height) * pageScale)}px`;
  page.append(marker);
  marker.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
  pageNumber.value = String(position.page);
  setStatus("Forward SyncTeX");
}

async function inverseSearch(event) {
  if (!event.ctrlKey) return;
  event.preventDefault();
  const page = event.currentTarget;
  const rect = page.getBoundingClientRect();
  const pageScale = Number(page.dataset.scale);
  setStatus("Inverse SyncTeX…");
  try {
    const response = await fetch(apiUrl("/api/inverse"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        page: Number(page.dataset.page),
        x: (event.clientX - rect.left) / pageScale,
        y: (event.clientY - rect.top) / pageScale,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Inverse search failed");
    setStatus(`${result.path.split("/").pop()}:${result.line}`);
  } catch (error) {
    setStatus(String(error?.message || error), true);
  }
}

function updateCurrentPage() {
  if (documentHandle) pageNumber.value = String(currentPage());
}

async function setScale(nextScale, mode = "manual") {
  scale = Math.max(0.25, Math.min(5, nextScale));
  scaleMode = mode;
  if (documentHandle) await loadPdf(revision, true);
}

function foldCharacter(character) {
  const lower = character.toLowerCase();
  return lower.length === 1 ? lower : character;
}

function normalizeQuery(query) {
  let text = "";
  for (const character of query) {
    if (/\s/.test(character)) {
      if (text && !text.endsWith(" ")) text += " ";
    } else {
      text += foldCharacter(character);
    }
  }
  return text.trim();
}

function pageSearchText(items) {
  const characters = [];
  const origins = [];
  const append = (character, origin) => {
    characters.push(character);
    origins.push(origin);
  };
  items.forEach((item, itemIndex) => {
    for (let offset = 0; offset < item.str.length; offset += 1) {
      const character = item.str[offset];
      if (/\s/.test(character)) {
        if (characters.length && characters.at(-1) !== " ") append(" ", null);
      } else {
        append(foldCharacter(character), { item: itemIndex, offset });
      }
    }
    if (item.hasEOL && characters.length && characters.at(-1) !== " ") append(" ", null);
  });
  return { text: characters.join(""), origins };
}

function textWidth(text, fontFamily) {
  measureContext.font = `100px ${fontFamily}`;
  return measureContext.measureText(text).width;
}

function matchRectangle(item, style, start, end) {
  const [a, b, c, d, e, f] = item.transform;
  const unit = Math.hypot(a, b) || 1;
  const family = style?.fontFamily || "sans-serif";
  const total = textWidth(item.str, family) || 1;
  const from = (textWidth(item.str.slice(0, start), family) / total) * (item.width / unit);
  const to = (textWidth(item.str.slice(0, end), family) / total) * (item.width / unit);
  const ascent = style?.ascent > 0 ? Math.min(style.ascent, 1.2) : 0.8;
  const descent = style?.descent < 0 ? Math.max(style.descent, -0.5) : -0.2;
  const corners = [[from, descent], [to, descent], [from, ascent], [to, ascent]]
    .map(([u, v]) => [a * u + c * v + e, b * u + d * v + f]);
  const xs = corners.map((point) => point[0]);
  const ys = corners.map((point) => point[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

async function searchPage(pdf, number, needle) {
  const page = await pdf.getPage(number);
  const content = await page.getTextContent();
  const items = content.items.filter((item) => typeof item.str === "string");
  const { text, origins } = pageSearchText(items);
  const matches = [];
  let from = 0;
  for (let start = text.indexOf(needle); start >= 0; start = text.indexOf(needle, from)) {
    const end = start + needle.length;
    from = end;
    const ranges = new Map();
    for (let index = start; index < end; index += 1) {
      const origin = origins[index];
      if (!origin) continue;
      const range = ranges.get(origin.item);
      if (range) {
        range[0] = Math.min(range[0], origin.offset);
        range[1] = Math.max(range[1], origin.offset);
      } else {
        ranges.set(origin.item, [origin.offset, origin.offset]);
      }
    }
    const rects = [];
    for (const [itemIndex, [first, last]] of ranges) {
      const item = items[itemIndex];
      rects.push(matchRectangle(item, content.styles[item.fontName], first, last + 1));
    }
    if (rects.length) matches.push({ page: number, rects });
  }
  return matches;
}

function clearSearchHighlights() {
  for (const element of document.querySelectorAll(".search-highlight")) element.remove();
}

function markCurrentMatch() {
  for (const element of document.querySelectorAll(".search-highlight")) {
    element.classList.toggle("current", Number(element.dataset.match) === searchIndex);
  }
}

function applySearchHighlights() {
  clearSearchHighlights();
  searchResults.forEach((match, index) => {
    const page = document.querySelector(`.page[data-page="${match.page}"]`);
    const viewport = pageViewports.get(match.page);
    if (!page || !viewport) return;
    for (const [x1, y1, x2, y2] of match.rects) {
      const points = [[x1, y1], [x2, y1], [x1, y2], [x2, y2]]
        .map(([x, y]) => viewport.convertToViewportPoint(x, y));
      const left = Math.min(...points.map((point) => point[0]));
      const top = Math.min(...points.map((point) => point[1]));
      const right = Math.max(...points.map((point) => point[0]));
      const bottom = Math.max(...points.map((point) => point[1]));
      const marker = document.createElement("div");
      marker.className = "search-highlight";
      marker.dataset.match = String(index);
      marker.style.left = `${left}px`;
      marker.style.top = `${top}px`;
      marker.style.width = `${Math.max(2, right - left)}px`;
      marker.style.height = `${Math.max(2, bottom - top)}px`;
      page.append(marker);
    }
  });
  markCurrentMatch();
}

function selectMatch(index, scroll = true) {
  const count = searchResults.length;
  searchIndex = ((index % count) + count) % count;
  markCurrentMatch();
  const match = searchResults[searchIndex];
  const label = `${searchIndex + 1}/${count}`;
  searchStatus.textContent = label;
  setStatus(`Match ${label}`);
  if (!scroll) return;
  const marker = document.querySelector(`.search-highlight[data-match="${searchIndex}"]`);
  if (marker) marker.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  else scrollToPage(match.page);
  pageNumber.value = String(match.page);
}

function nearestMatch(direction) {
  const page = currentPage();
  if (direction < 0) {
    for (let index = searchResults.length - 1; index >= 0; index -= 1) {
      if (searchResults[index].page <= page) return index;
    }
    return searchResults.length - 1;
  }
  const index = searchResults.findIndex((match) => match.page >= page);
  return index < 0 ? 0 : index;
}

async function runSearch(query, { jump = true, direction = 1 } = {}) {
  const needle = normalizeQuery(query);
  if (!documentHandle || !needle) return;
  lastSearch = query;
  searchActive = true;
  const generation = ++searchGeneration;
  const pdf = documentHandle;
  const previousIndex = searchIndex;
  searchStatus.textContent = "searching…";
  const results = [];
  for (let number = 1; number <= pdf.numPages; number += 1) {
    let matches;
    try {
      matches = await searchPage(pdf, number, needle);
    } catch (error) {
      if (generation !== searchGeneration || pdf.loadingTask?.destroyed) return;
      searchStatus.textContent = "error";
      setStatus(String(error?.message || error), true);
      return;
    }
    if (generation !== searchGeneration) return;
    results.push(...matches);
  }
  searchResults = results;
  searchIndex = -1;
  applySearchHighlights();
  if (!results.length) {
    searchStatus.textContent = "no matches";
    setStatus(`No matches for "${query}"`);
    return;
  }
  if (jump) selectMatch(nearestMatch(direction));
  else selectMatch(Math.min(Math.max(previousIndex, 0), results.length - 1), false);
}

function repeatSearch(direction) {
  if (!searchResults.length) {
    if (lastSearch) void runSearch(lastSearch, { direction });
    return;
  }
  selectMatch(searchIndex + direction);
}

function clearSearch() {
  searchActive = false;
  searchResults = [];
  searchIndex = -1;
  searchGeneration += 1;
  clearSearchHighlights();
  searchStatus.textContent = "";
}

function openSearch() {
  searchPanel.classList.remove("hidden");
  searchInput.focus();
  searchInput.select();
}

function overlaysOpen() {
  return !help.classList.contains("hidden") || !searchPanel.classList.contains("hidden");
}

function closeOverlays() {
  help.classList.add("hidden");
  if (!searchPanel.classList.contains("hidden")) {
    searchPanel.classList.add("hidden");
    clearSearch();
  }
  searchInput.blur();
}

function handleKey(event) {
  const target = event.target;
  if (target instanceof HTMLInputElement) {
    if (event.key === "Escape") {
      closeOverlays();
      event.preventDefault();
    }
    return;
  }
  const halfPage = window.innerHeight * 0.48;
  const fullPage = window.innerHeight * 0.88;
  let handled = true;
  if (event.key === "j") window.scrollBy({ top: 90 });
  else if (event.key === "k") window.scrollBy({ top: -90 });
  else if (event.key === "h") window.scrollBy({ left: -90 });
  else if (event.key === "l") window.scrollBy({ left: 90 });
  else if (event.key === "J") scrollToPage(currentPage() + 1);
  else if (event.key === "K") scrollToPage(currentPage() - 1);
  else if (event.ctrlKey && event.key === "d") window.scrollBy({ top: halfPage });
  else if (event.ctrlKey && event.key === "u") window.scrollBy({ top: -halfPage });
  else if (event.ctrlKey && event.key === "f") window.scrollBy({ top: fullPage });
  else if (event.ctrlKey && event.key === "b") window.scrollBy({ top: -fullPage });
  else if (event.ctrlKey && event.key === "o") goBack();
  else if (event.ctrlKey && event.key === "i") goForward();
  else if (event.key === "G") scrollToPage(documentHandle?.numPages || 1);
  else if (event.key === "g") {
    if (pendingG) {
      clearTimeout(pendingGTimer);
      pendingG = false;
      scrollToPage(1);
    } else {
      pendingG = true;
      pendingGTimer = setTimeout(() => { pendingG = false; }, 500);
    }
  }
  else if (["+", "="].includes(event.key)) void setScale(scale * 1.1);
  else if (event.key === "-") void setScale(scale / 1.1);
  else if (event.key === "s") void setScale(scale, "width");
  else if (event.key === "a") void setScale(scale, "page");
  else if (event.key === "r") void loadPdf(revision, true);
  else if (event.key === "/") openSearch();
  else if (event.key === "n") repeatSearch(1);
  else if (event.key === "N") repeatSearch(-1);
  else if (event.key === "?") help.classList.toggle("hidden");
  else if (event.key === "Escape") {
    if (overlaysOpen()) closeOverlays();
    else clearSearch();
  }
  else handled = false;
  if (handled) event.preventDefault();
}

function handleEvent(event) {
  if (event.type === "status") {
    revision = event.revision;
    if (!event.pdfReady) {
      showWaiting();
    } else if (!documentHandle || event.revision !== loadedRevision) {
      void loadPdf(event.revision, true);
    } else {
      // Reconnected while the rendered document is still current: keep the
      // reading position instead of starting over at page 1.
      setStatus("Ready");
    }
  } else if (event.type === "reload") {
    void loadPdf(event.revision, true);
  } else if (event.type === "forward") {
    showForward(event);
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectEvents();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 10000);
}

function connectEvents() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  events?.close();
  const source = new EventSource(apiUrl("/events"));
  events = source;
  lastEventAt = Date.now();
  source.onopen = () => {
    if (source !== events) return;
    reconnectDelay = 1000;
    lastEventAt = Date.now();
    setStatus("Connected");
  };
  source.onerror = () => {
    if (source !== events) return;
    setStatus("Reconnecting…", true);
    // Browsers retry dropped connections on their own, but give up for good
    // when the request was aborted rather than reset. Firefox aborts
    // connections that stayed silent for a few seconds after a network
    // change, which used to leave the viewer on "Reconnecting…" forever.
    if (source.readyState === EventSource.CLOSED) scheduleReconnect();
  };
  source.onmessage = (message) => {
    if (source !== events) return;
    lastEventAt = Date.now();
    handleEvent(JSON.parse(message.data));
  };
}

function checkConnection() {
  if (!events) return;
  const silent = Date.now() - lastEventAt > HEARTBEAT_TIMEOUT;
  if (events.readyState === EventSource.CLOSED || silent) {
    setStatus("Reconnecting…", true);
    connectEvents();
  }
}

document.querySelector("#previous").addEventListener("click", () => scrollToPage(currentPage() - 1));
document.querySelector("#next").addEventListener("click", () => scrollToPage(currentPage() + 1));
document.querySelector("#zoom-in").addEventListener("click", () => void setScale(scale * 1.1));
document.querySelector("#zoom-out").addEventListener("click", () => void setScale(scale / 1.1));
document.querySelector("#fit-width").addEventListener("click", () => void setScale(scale, "width"));
document.querySelector("#fit-page").addEventListener("click", () => void setScale(scale, "page"));
document.querySelector("#help-button").addEventListener("click", () => help.classList.toggle("hidden"));
pageNumber.addEventListener("change", () => scrollToPage(pageNumber.value));
searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    void runSearch(searchInput.value);
    event.preventDefault();
  }
});
window.addEventListener("keydown", handleKey);
window.addEventListener("scroll", updateCurrentPage, { passive: true });
window.addEventListener("resize", () => {
  if (scaleMode !== "manual" && documentHandle) void loadPdf(revision, true);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) checkConnection();
});
window.addEventListener("online", checkConnection);
setInterval(checkConnection, 5000);

connectEvents();
