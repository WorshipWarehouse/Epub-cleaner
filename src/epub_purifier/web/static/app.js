"use strict";

// All values from the server are rendered with textContent (never innerHTML),
// because a malicious EPUB can put arbitrary strings in its metadata.

const MAX_BATCH_FILES = 10;
const INSPECT_TIMEOUT_MS = 60_000;
let queueSeq = 0;

const state = {
  queue: [],
  activeId: null,
  busy: false,
  inspectPending: false,
};

const $ = (id) => document.getElementById(id);

const fileInput = $("file-input");
const dropzone = $("dropzone");
const fileQueue = $("file-queue");
const inspectBtn = $("inspect-btn");
const resetBtn = $("reset-btn");
const cleanBtn = $("clean-btn");
const cleanAllBtn = $("clean-all-btn");
const batchDownload = $("batch-download");
const batchDownloadLink = $("batch-download-link");
const batchDownloadMeta = $("batch-download-meta");
const status = $("status");

function activeItem() {
  return state.queue.find((item) => item.id === state.activeId) || null;
}

function pendingCount() {
  return state.queue.filter((item) => item.status === "queued").length;
}

function statusLabel(item) {
  switch (item.status) {
    case "queued": return "waiting";
    case "inspecting": return "inspecting…";
    case "ready": return "report ready";
    case "cleaning": return "cleaning…";
    case "cleaned": return "cleaned";
    case "error": return "failed";
    default: return item.status;
  }
}

function updateInspectButton() {
  const pending = pendingCount();
  inspectBtn.textContent = pending > 1 ? `Inspect ${pending} files` : "Inspect file";
  inspectBtn.disabled = state.busy || pending === 0;
}

/** Refresh Clean all and batch ZIP controls from the current queue. */
function updateBatchControls() {
  const ready = state.queue.filter((item) => item.status === "ready");
  cleanAllBtn.hidden = ready.length < 2;
  cleanAllBtn.disabled = state.busy || ready.length < 2;
  if (!cleanAllBtn.hidden) cleanAllBtn.textContent = `Clean ${ready.length} files`;

  const cleaned = state.queue.filter((item) => item.status === "cleaned" && item.jobId);
  if (state.busy || cleaned.length < 2) {
    batchDownload.hidden = true;
    batchDownloadLink.removeAttribute("href");
    batchDownloadMeta.textContent = "";
    return;
  }
  const query = new URLSearchParams({ job_ids: cleaned.map((item) => item.jobId).join(",") });
  batchDownloadLink.href = `/api/download/batch?${query.toString()}`;
  batchDownloadMeta.textContent = `${cleaned.length} cleaned files in one ZIP`;
  batchDownload.hidden = false;
}

/** Render the upload queue while preserving keyboard focus and batch controls. */
function renderQueue() {
  const focused = fileQueue.contains(document.activeElement) ? document.activeElement.dataset : null;
  const focusKey = focused ? { id: focused.queueId, action: focused.action } : null;
  fileQueue.replaceChildren();
  fileQueue.hidden = state.queue.length === 0;
  for (const item of state.queue) {
    const row = make("li", {
      class: `queue-item${item.id === state.activeId ? " active" : ""}`,
      dataset: { status: item.status },
    });
    const select = make("button", { type: "button", class: "queue-select", dataset: { queueId: item.id, action: "select" } }, [
      make("span", { class: "queue-name", text: item.file.name }),
      make("span", {
        class: "queue-meta",
        text: `${formatBytes(item.file.size)} · ${statusLabel(item)}`,
      }),
    ]);
    select.addEventListener("click", () => selectItem(item.id));
    row.append(select);
    if (item.status === "error") {
      for (const action of ["Retry", "Remove"]) {
        const button = make("button", {
          type: "button", text: action,
          dataset: { queueId: item.id, action: action.toLowerCase() },
        });
        button.addEventListener("click", () => {
          if (action === "Retry") {
            item.status = "queued";
            item.error = "";
          } else {
            state.queue = state.queue.filter((candidate) => candidate.id !== item.id);
            if (state.activeId === item.id) state.activeId = state.queue[0]?.id ?? null;
          }
          renderQueue();
          renderActive();
          updateInspectButton();
        });
        row.append(button);
      }
    }
    if (item.error) row.append(make("p", { class: "queue-error", text: item.error }));
    fileQueue.append(row);
  }
  if (focusKey) {
    const replacement = Array.from(fileQueue.querySelectorAll("button")).find(
      (button) => button.dataset.queueId === focusKey.id && button.dataset.action === focusKey.action,
    );
    replacement?.focus({ preventScroll: true });
  }
  updateBatchControls();
}

function setStatus(message, isError = false) {
  status.textContent = message || "";
  status.classList.toggle("error", Boolean(isError));
}

function make(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// -- file selection ------------------------------------------------------
function chooseFiles(fileList) {
  const incoming = Array.from(fileList || []);
  if (!incoming.length) return 0;

  const epubFiles = incoming.filter((file) => file.name.toLowerCase().endsWith(".epub"));
  let skipped = incoming.length - epubFiles.length;
  if (!epubFiles.length) {
    setStatus("Those are not .epub files. Pick the books you want to inspect.", true);
    return 0;
  }

  let added = 0;
  for (const file of epubFiles) {
    if (state.queue.length >= MAX_BATCH_FILES) {
      skipped += 1;
      continue;
    }
    const duplicate = state.queue.some(
      (item) =>
        item.file.name === file.name &&
        item.file.size === file.size &&
        item.file.lastModified === file.lastModified,
    );
    if (duplicate) {
      skipped += 1;
      continue;
    }
    state.queue.push({
      id: (queueSeq += 1),
      file,
      status: "queued",
      jobId: null,
      report: null,
      reviewable: [],
      selected: new Set(),
      error: "",
    });
    added += 1;
  }

  if (added) {
    state.activeId = state.queue[state.queue.length - added].id;
    renderActive();
    resetBtn.hidden = false;
    setStatus(skipped ? `${added} EPUB(s) queued; ${skipped} file(s) skipped.` : "");
  } else {
    setStatus(
      state.queue.length >= MAX_BATCH_FILES
        ? `You can queue up to ${MAX_BATCH_FILES} EPUBs at a time.`
        : "Those files are already in the queue.",
      true,
    );
  }
  renderQueue();
  updateInspectButton();
  return added;
}

function selectItem(itemId) {
  const item = state.queue.find((candidate) => candidate.id === itemId);
  if (!item) return;
  state.activeId = item.id;
  renderQueue();
  renderActive();
}

function renderActive() {
  const item = activeItem();
  if (!item) {
    $("results").hidden = true;
    $("cleaned").hidden = true;
    cleanBtn.disabled = true;
    return;
  }
  if (item.report && (item.status === "ready" || item.status === "cleaned")) {
    if (item.status === "cleaned") renderCleaned(item.report);
    else renderReport(item);
    cleanBtn.disabled = item.status !== "ready" || state.busy;
    if (item.error) setStatus(item.error, true);
    else setStatus("");
    return;
  }
  $("results").hidden = true;
  $("cleaned").hidden = true;
  cleanBtn.disabled = true;
  if (item.error) setStatus(item.error, true);
  else if (item.status === "inspecting") setStatus(`Inspecting ${item.file.name}…`);
  else setStatus("");
}

function resumePendingInspection() {
  if (state.inspectPending && !state.busy) {
    state.inspectPending = false;
    void inspectQueue();
  }
}

async function inspectQueue() {
  if (state.busy) {
    state.inspectPending = true;
    return;
  }
  const pending = state.queue.filter((item) => item.status === "queued");
  if (!pending.length) return;

  state.busy = true;
  resetBtn.disabled = true;
  updateInspectButton();
  let completed = 0;
  try {
    for (const item of pending) {
      item.status = "inspecting";
      item.error = "";
      renderQueue();
      setStatus(`Inspecting ${completed + 1} of ${pending.length}: ${item.file.name}`);
      const controller = new AbortController();
      const deadline = setTimeout(() => controller.abort(), INSPECT_TIMEOUT_MS);
      try {
        const body = new FormData();
        body.append("file", item.file);
        const response = await fetch("/api/inspect", { method: "POST", body, signal: controller.signal });
        const payload = await response.json().catch((error) => {
          if (controller.signal.aborted) throw error;
          return {};
        });
        if (!response.ok) throw new Error(payload.detail || "The file could not be inspected.");

        item.jobId = payload.job_id;
        item.report = payload.report;
        item.reviewable = payload.reviewable || [];
        item.status = "ready";
      } catch (error) {
        item.status = "error";
        item.error = controller.signal.aborted ? "Inspection timed out. Retry this file." : error.message;
      } finally {
        clearTimeout(deadline);
      }
      completed += 1;
      renderQueue();
      if (state.activeId === item.id) renderActive();
      updateInspectButton();
    }
    const failed = pending.filter((item) => item.status === "error").length;
    if (failed) setStatus(`${completed - failed} inspected; ${failed} failed.`, true);
    else setStatus(pending.length === 1 ? "" : `${pending.length} files inspected.`);
  } finally {
    state.busy = false;
    resetBtn.disabled = false;
    updateInspectButton();
    renderQueue();
    const current = activeItem();
    cleanBtn.disabled = !current || current.status !== "ready";
    resumePendingInspection();
  }
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
fileInput.addEventListener("change", () => {
  chooseFiles(fileInput.files);
  fileInput.value = "";
});

// A downloaded book can be dropped anywhere on the page, not only on the
// dashed box. Only file drags are intercepted, so dropping selected text or a
// link keeps the browser's normal behaviour.
function hasFiles(event) {
  const types = event.dataTransfer && event.dataTransfer.types;
  return Boolean(types && Array.from(types).includes("Files"));
}

let dragDepth = 0;

function setDragActive(active) {
  dropzone.classList.toggle("dragover", active);
  document.body.classList.toggle("file-dragging", active);
}

window.addEventListener("dragenter", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth += 1;
  setDragActive(true);
});

window.addEventListener("dragover", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  setDragActive(true);
});

window.addEventListener("dragleave", () => {
  if (dragDepth === 0) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) setDragActive(false);
});

window.addEventListener("drop", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  setDragActive(false);
  const files = Array.from(event.dataTransfer.files || []);
  // Dropped books go straight to inspection: one drag from the download
  // shelf to finished reports for every EPUB in the batch.
  if (chooseFiles(files)) void inspectQueue();
});

// -- inspect -------------------------------------------------------------
inspectBtn.addEventListener("click", () => {
  void inspectQueue();
});

function renderReport(item) {
  const report = item.report;
  const reviewable = item.reviewable;
  $("results").hidden = false;
  $("cleaned").hidden = true;
  resetBtn.hidden = false;

  $("verdict").textContent = report.summary.verdict;

  const counts = $("counts");
  counts.replaceChildren(
    ...["critical", "high", "medium", "low", "informational"].map((severity) => {
      const value = (report.summary.counts || {})[severity] || 0;
      return make("div", {}, [
        make("dt", { text: severity === "informational" ? "info" : severity }),
        make("dd", { text: String(value) }),
      ]);
    }),
  );

  const findings = $("findings");
  findings.replaceChildren();
  for (const finding of report.findings) {
    findings.append(renderFinding(finding));
  }
  if (!report.findings.length) {
    findings.append(
      make("li", { class: "finding informational" }, [
        make("p", { text: "No findings. That does not prove the file is safe — it means the checks had nothing to report." }),
      ]),
    );
  }

  const reviewBlock = $("review-block");
  const reviewList = $("review-list");
  reviewList.replaceChildren();
  if (reviewable.length) {
    reviewBlock.hidden = false;
    for (const finding of reviewable) {
      const checkbox = make("input", { type: "checkbox", value: finding.id });
      checkbox.checked = item.selected.has(finding.id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) item.selected.add(finding.id);
        else item.selected.delete(finding.id);
      });
      reviewList.append(
        make("li", { class: `review-item ${finding.severity}` }, [
          make("label", {}, [
            checkbox,
            make("span", {}, [
              make("span", { class: "rule", text: finding.rule }),
              make("p", { text: finding.message }),
              make("p", { class: "meta", text: describe(finding) }),
            ]),
          ]),
        ]),
      );
    }
  } else {
    reviewBlock.hidden = true;
  }

  cleanBtn.disabled = item.status !== "ready" || state.busy;
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderFinding(finding) {
  const head = make("div", { class: "finding-head" }, [
    make("span", { class: `severity ${finding.severity}`, text: finding.severity_label }),
    make("span", { class: "rule", text: finding.rule }),
  ]);
  const children = [head, make("p", { text: finding.message })];
  const location = describe(finding);
  if (location) children.push(make("p", { class: "meta", text: location }));
  if (finding.evidence) children.push(make("p", { class: "evidence", text: finding.evidence }));
  if (finding.remediation === "review") {
    children.push(make("p", { class: "meta", text: `Needs confirmation · id ${finding.id}` }));
  }
  return make("li", { class: `finding ${finding.severity}` }, children);
}

function describe(finding) {
  const parts = [];
  if (finding.target) parts.push(finding.target);
  if (finding.location) parts.push(finding.location);
  if (typeof finding.confidence === "number") parts.push(`confidence ${Math.round(finding.confidence * 100)}%`);
  return parts.join(" · ");
}

// -- clean ---------------------------------------------------------------
cleanAllBtn.addEventListener("click", () => {
  void cleanAllReady();
});

/** Clean every ready queue item, preserving each file's review selections. */
async function cleanAllReady() {
  if (state.busy) return;
  const ready = state.queue.filter((item) => item.status === "ready");
  if (ready.length < 2) return;

  state.busy = true;
  resetBtn.disabled = true;
  updateInspectButton();
  renderQueue();
  let completed = 0;
  let failed = 0;
  try {
    for (const item of ready) {
      item.status = "cleaning";
      item.error = "";
      renderQueue();
      setStatus(`Cleaning ${completed + 1} of ${ready.length}: ${item.file.name}`);
      try {
        const response = await fetch("/api/clean", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_id: item.jobId,
            approved: Array.from(item.selected),
            approve_all: $("opt-approve").checked,
            remove_remote_resources: $("opt-remote").checked,
          }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || "Cleaning failed.");
        item.report = payload.report;
        item.status = "cleaned";
      } catch (error) {
        item.status = "ready";
        item.error = error.message;
        failed += 1;
      }
      completed += 1;
      renderQueue();
      if (state.activeId === item.id) renderActive();
    }
  } finally {
    state.busy = false;
    resetBtn.disabled = false;
    updateInspectButton();
    renderQueue();
    const firstCleaned = state.queue.find((candidate) => candidate.status === "cleaned");
    if (firstCleaned) {
      state.activeId = firstCleaned.id;
    }
    renderActive();
    setStatus(
      failed ? `${completed - failed} cleaned; ${failed} failed.` : `${completed} files cleaned.`,
      failed > 0,
    );
    updateBatchControls();
    resumePendingInspection();
  }
}

cleanBtn.addEventListener("click", async () => {
  const item = activeItem();
  if (!item || !item.jobId || state.busy || item.status !== "ready") return;
  state.busy = true;
  item.status = "cleaning";
  item.error = "";
  cleanBtn.disabled = true;
  renderActive();
  updateInspectButton();
  renderQueue();
  setStatus(`Rebuilding ${item.file.name}…`);

  try {
    const response = await fetch("/api/clean", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: item.jobId,
        approved: Array.from(item.selected),
        approve_all: $("opt-approve").checked,
        remove_remote_resources: $("opt-remote").checked,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Cleaning failed.");

    item.report = payload.report;
    item.status = "cleaned";
  } catch (error) {
    item.status = "ready";
    item.error = error.message;
  } finally {
    state.busy = false;
    renderQueue();
    const current = activeItem();
    if (current && current.id === item.id) {
      renderActive();
    } else {
      // A background file finished while another file was selected. Its
      // result stays on that queue row; do not replace the active report.
      cleanBtn.disabled = !current || current.status !== "ready";
    }
    updateInspectButton();
    resumePendingInspection();
  }
});

function renderCleaned(report) {
  $("results").hidden = true;
  $("cleaned").hidden = false;
  $("cleaned-summary").textContent = report.summary.verdict;

  const link = $("download-link");
  link.href = report.output.download_url;
  link.setAttribute("download", report.output.name || "cleaned.epub");
  $("download-line").hidden = false;

  const removed = $("removed");
  removed.replaceChildren();
  for (const item of report.removed || []) {
    removed.append(
      make("li", { class: `finding ${item.severity}` }, [
        make("div", { class: "finding-head" }, [
          make("span", { class: `severity ${item.severity}`, text: item.action.replace(/_/g, " ") }),
          make("span", { class: "rule", text: item.target || "package" }),
        ]),
        item.detail ? make("p", { text: item.detail }) : null,
      ]),
    );
  }
  if (!removed.children.length) {
    removed.append(make("li", { class: "finding informational" }, [make("p", { text: "Nothing needed removing." })]));
  }

  const stats = $("stats");
  stats.replaceChildren();
  const entries = Object.entries(report.stats || {});
  for (const [key, value] of entries) {
    const rendered = Array.isArray(value) ? value.join(", ") || "—" : String(value);
    stats.append(make("dt", { text: key.replace(/_/g, " ") }), make("dd", { text: rendered }));
  }
  stats.append(
    make("dt", { text: "output sha256" }),
    make("dd", { text: (report.output && report.output.sha256) || "—" }),
  );

  $("cleaned").scrollIntoView({ behavior: "smooth", block: "start" });
}

// -- reset ---------------------------------------------------------------
resetBtn.addEventListener("click", () => {
  if (state.busy) return;
  state.queue = [];
  state.activeId = null;
  fileInput.value = "";
  fileQueue.replaceChildren();
  fileQueue.hidden = true;
  inspectBtn.disabled = true;
  inspectBtn.textContent = "Inspect file";
  resetBtn.hidden = true;
  $("results").hidden = true;
  $("cleaned").hidden = true;
  cleanBtn.disabled = true;
  cleanAllBtn.hidden = true;
  batchDownload.hidden = true;
  batchDownloadLink.removeAttribute("href");
  batchDownloadMeta.textContent = "";
  setStatus("");
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// -- book search ---------------------------------------------------------
const searchForm = $("search-form");

if (searchForm) {
  const searchInput = $("search-input");
  const searchStatus = $("search-status");
  const sourceList = $("source-list");
  const searchResults = $("search-results");
  const transferPanel = $("transfer-panel");
  const transferBody = $("transfer-body");

  const setSearchStatus = (message, isError = false) => {
    searchStatus.textContent = message || "";
    searchStatus.classList.toggle("error", Boolean(isError));
  };

  const safeHref = (value) => {
    try {
      const parsed = new URL(String(value), window.location.origin);
      return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
    } catch (error) {
      return null;
    }
  };

  const safeCover = (value) =>
    typeof value === "string" && /^\/api\/cover\/[a-z0-9_-]+\/[A-Za-z0-9._-]+$/.test(value)
      ? value
      : null;

  function renderSourceStatuses(sources) {
    sourceList.replaceChildren();
    for (const source of sources || []) {
      const row = make("div", { class: `source-row${source.ok ? "" : " error"}` }, [
        make("span", { class: "source-name", text: source.label }),
        make("span", {
          class: `badge ${source.supports_download ? "good" : "muted"}`,
          text: source.supports_download ? "downloads + cleaning" : "search link only",
        }),
        make("span", {
          class: "meta",
          text: source.ok
            ? source.supports_download
              ? `${source.result_count} match${source.result_count === 1 ? "" : "es"}`
              : "search link"
            : "unavailable",
        }),
      ]);
      if (source.error) row.append(make("p", { class: "meta", text: source.error }));
      sourceList.append(row);
    }
  }

  const matchLabels = {
    isbn: "ISBN match",
    exact_title: "Exact title match",
    title: "Title match",
    title_author: "Title and author match",
    title_contains: "Close title match",
    fuzzy_title: "Fuzzy title match",
    author: "Author match",
  };

  function renderSourceEntry(listing) {
    const row = make("div", { class: "result-source-row" });
    row.append(make("span", { class: "source-name", text: listing.source_label }));

    if (listing.is_search_link) {
      row.append(make("span", { class: "badge muted", text: "search link" }));
      const href = safeHref(listing.details_url);
      if (href) {
        row.append(
          make("a", {
            class: "button ghost small",
            href,
            target: "_blank",
            rel: "noopener noreferrer",
            text: `Search ${listing.source_label}`,
          }),
        );
      }
    } else if (listing.downloadable) {
      row.append(make("span", { class: "badge good", text: "scan on fetch" }));
      const button = make("button", {
        class: "button primary small",
        type: "button",
        text: `Fetch from ${listing.source_label}`,
      });
      button.addEventListener("click", () => startTransfer(listing, button));
      row.append(button);
    } else {
      row.append(make("span", { class: "badge muted", text: "source page" }));
      const href = safeHref(listing.details_url);
      if (href) {
        row.append(
          make("a", {
            class: "button ghost small",
            href,
            target: "_blank",
            rel: "noopener noreferrer",
            text: "Open source page",
          }),
        );
      }
    }

    if (listing.download_note) {
      row.append(make("p", { class: "meta", text: listing.download_note }));
    }
    return row;
  }

  function renderResult(result) {
    const listings = Array.isArray(result.listings) ? result.listings : [result];
    const primary = listings.find((item) => !item.is_search_link) || result;
    const coverPath = safeCover(result.cover_url);
    const initial = (result.title || "?").trim().charAt(0).toUpperCase() || "?";
    const placeholder = () =>
      make("div", { class: "cover placeholder", "aria-hidden": "true", text: initial });

    let cover = placeholder();
    if (coverPath) {
      const image = make("img", {
        class: "cover",
        src: coverPath,
        alt: "",
        loading: "lazy",
        width: "72",
        height: "108",
      });
      image.addEventListener("error", () => image.replaceWith(placeholder()), { once: true });
      cover = image;
    }

    const meta = [];
    if (result.authors && result.authors.length) meta.push(result.authors.join(", "));
    if (result.formats && result.formats.length) meta.push(result.formats.join(" · "));
    if (result.languages && result.languages.length) meta.push(result.languages.join(", "));
    if (result.isbn && result.isbn.length) meta.push(`ISBN ${result.isbn.join(", ")}`);

    const children = [];
    const match = result.match || {};
    if (matchLabels[match.type]) {
      children.push(make("p", { class: "result-match", text: matchLabels[match.type] }));
    }
    children.push(make("h3", { class: "result-title", text: result.title }));
    children.push(
      result.downloadable
        ? make("span", { class: "badge good", text: "download available" })
        : make("span", { class: "badge muted", text: "available elsewhere" }),
    );
    if (meta.length) children.push(make("p", { class: "meta", text: meta.join(" · ") }));

    const sourceBlock = make("div", { class: "result-sources" });
    for (const listing of listings) sourceBlock.append(renderSourceEntry(listing));
    children.push(sourceBlock);

    if (primary.security && primary.security.disclaimer) {
      children.push(make("p", { class: "meta", text: primary.security.disclaimer }));
    }

    return make("li", { class: "result-card" }, [
      cover,
      make("div", { class: "result-body" }, children),
    ]);
  }

  function renderSourceLinks(links) {
    const block = make("div", { class: "source-search" });
    block.append(make("p", { class: "meta", text: "No matching book was confirmed. You can still search the link-only sites directly:" }));
    for (const listing of links || []) {
      const href = safeHref(listing.details_url);
      if (!href) continue;
      block.append(
        make("a", {
          class: "button ghost small",
          href,
          target: "_blank",
          rel: "noopener noreferrer",
          text: `Search ${listing.source_label}`,
        }),
      );
    }
    return block;
  }

  async function startTransfer(listing, button) {
    if (state.busy) return;
    state.busy = true;
    button.disabled = true;
    transferPanel.hidden = true;
    setSearchStatus(`Fetching from ${listing.source_label}…`);

    try {
      const response = await fetch("/api/transfer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: listing.source, book_id: listing.book_id }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "That download could not be processed.");
      renderTransfer(payload);
      setSearchStatus("");
    } catch (error) {
      setSearchStatus(error.message, true);
    } finally {
      state.busy = false;
      button.disabled = false;
      resumePendingInspection();
    }
  }

  function renderTransfer(payload) {
    const report = payload.report || {};
    const summary = report.summary || {};
    const security = payload.security || {};
    const counts = security.counts || summary.counts || {};

    const list = make("dl", { class: "counts" });
    for (const severity of ["critical", "high", "medium", "low", "informational"]) {
      list.append(
        make("div", {}, [
          make("dt", { text: severity === "informational" ? "info" : severity }),
          make("dd", { text: String(counts[severity] || 0) }),
        ]),
      );
    }

    const download = make("a", {
      class: "button primary",
      href: payload.download_url,
      download: payload.file_name || "cleaned.epub",
      text: "Download cleaned EPUB",
    });

    const countdown = make("span", {
      class: "countdown",
      dataset: { expires: String(payload.expires_at_epoch || "") },
      text: payload.expires_in ? `deletes in ${payload.expires_in}` : "",
    });
    const deleteButton = make("button", {
      class: "button ghost small",
      type: "button",
      text: "Delete now",
    });
    deleteButton.addEventListener("click", () =>
      deleteTransfer(payload.transfer_id, deleteButton, transferPanel),
    );

    transferBody.replaceChildren(
      make("p", { class: "verdict", text: security.headline || "Cleaned and verified." }),
      list,
      make("p", {
        class: "meta",
        text: `Removed ${summary.removed_count || 0} item(s); ${security.needs_review || 0} ambiguous item(s) left in place.`,
      }),
      make("p", { class: "download-line" }, [download, " ", deleteButton]),
      make("p", { class: "meta" }, [
        "Temporary copy in private storage · ",
        countdown,
        " · it is deleted whether or not you download it.",
      ]),
      make("p", { class: "meta", text: security.disclaimer || "" }),
      make("p", { class: "meta", text: security.safety_notice || "" }),
    );
    transferPanel.hidden = false;
    transferPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    refreshLibrary();
  }

  searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = searchInput.value.trim();
    if (query.length < 2) {
      setSearchStatus("Enter at least 2 characters.", true);
      return;
    }
    if (state.busy) return;
    state.busy = true;
    transferPanel.hidden = true;
    setSearchStatus("Searching every source…");

    const body = { query };
    for (const [key, element] of Object.entries({
      title: $("filter-title"),
      author: $("filter-author"),
      isbn: $("filter-isbn"),
      language: $("filter-language"),
      format: $("filter-format"),
    })) {
      const value = element ? element.value.trim() : "";
      if (value) body[key] = value;
    }

    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Search failed.");
      renderSourceStatuses(payload.sources);
      searchResults.replaceChildren();
      for (const result of payload.results) searchResults.append(renderResult(result));
      if (!payload.results.length) {
        const empty = make("li", { class: "result-empty" });
        empty.append(make("p", { text: "No matching books found." }));
        if (payload.source_links && payload.source_links.length) {
          empty.append(renderSourceLinks(payload.source_links));
        }
        searchResults.append(empty);
        setSearchStatus("No matching books found.");
      } else {
        const sourceCount = new Set(
          payload.results.flatMap((result) =>
            (result.listings || []).filter((listing) => !listing.is_search_link).map((listing) => listing.source),
          ),
        ).size;
        setSearchStatus(`${payload.results.length} matching book(s) across ${sourceCount} source(s).`);
      }
    } catch (error) {
      setSearchStatus(error.message, true);
    } finally {
      state.busy = false;
      resumePendingInspection();
    }
  });
}

// -- temporary downloads -------------------------------------------------
// Cleaned copies live on the server for five hours. This panel shows what is
// stored right now, how long each copy has left, and lets the user delete one
// immediately. Every value is still rendered as text, never as markup.
const libraryPanel = $("library-panel");
const libraryList = $("library-list");
const libraryStorage = $("library-storage");
const libraryNotice = $("library-notice");

function formatRemaining(seconds) {
  const value = Math.max(0, Math.round(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = value % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

function safeTransferUrl(value) {
  return typeof value === "string" && /^\/api\/transfer\/[0-9a-f]{32}\/download$/.test(value)
    ? value
    : null;
}

async function deleteTransfer(transferId, button, panel) {
  if (!/^[0-9a-f]{32}$/.test(String(transferId || ""))) return;
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/transfer/${transferId}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "That file could not be deleted.");
    if (panel) panel.hidden = true;
    if (libraryNotice) {
      libraryNotice.textContent = "Temporary copy deleted.";
      libraryNotice.classList.remove("error");
    }
    await refreshLibrary();
  } catch (error) {
    if (libraryNotice) {
      libraryNotice.textContent = error.message;
      libraryNotice.classList.add("error");
    }
    if (button) button.disabled = false;
  }
}

function renderLibrary(payload) {
  if (!libraryPanel) return;
  const transfers = payload.transfers || [];
  const storage = payload.storage || {};

  libraryList.replaceChildren();
  for (const item of transfers) {
    const href = safeTransferUrl(item.download_url);
    const countdown = make("span", {
      class: "countdown",
      dataset: { expires: String(item.expires_at_epoch || "") },
      text: item.expires_in ? `deletes in ${item.expires_in}` : "",
    });
    const actions = [];
    if (href) {
      actions.push(
        make("a", {
          class: "button primary small",
          href,
          download: item.file_name || "cleaned.epub",
          text: "Download",
        }),
      );
    }
    const remove = make("button", {
      class: "button ghost small",
      type: "button",
      text: "Delete now",
    });
    remove.addEventListener("click", () => deleteTransfer(item.transfer_id, remove, null));
    actions.push(remove);

    libraryList.append(
      make("li", { class: "library-item" }, [
        make("div", { class: "library-body" }, [
          make("p", { class: "result-title", text: item.file_name || "cleaned.epub" }),
          make("p", { class: "meta" }, [
            `${item.source} · ${formatBytes(item.size_bytes || 0)} · `,
            countdown,
          ]),
        ]),
        make("div", { class: "result-actions" }, actions),
      ]),
    );
  }

  const used = formatBytes(storage.used_bytes || 0);
  const max = formatBytes(storage.max_bytes || 0);
  libraryStorage.textContent = transfers.length
    ? `${used} of ${max} temporary storage in use · ${transfers.length} cleaned copy(s) · auto-delete ${formatRemaining(storage.ttl_seconds || 18000)} after download starts`
    : `No temporary copies are stored. ${max} of temporary storage available.`;
  libraryPanel.hidden = transfers.length === 0;
}

async function refreshLibrary() {
  if (!libraryPanel) return;
  try {
    const response = await fetch("/api/transfers");
    if (!response.ok) return;
    renderLibrary(await response.json());
  } catch (error) {
    // The panel is an enhancement; a failed refresh must not break the page.
  }
}

function tickCountdowns() {
  const now = Date.now() / 1000;
  for (const node of document.querySelectorAll("[data-expires]")) {
    const expires = Number(node.dataset.expires);
    if (!Number.isFinite(expires) || expires <= 0) continue;
    const remaining = Math.max(0, expires - now);
    node.textContent = remaining <= 0 ? "deleting now…" : `deletes in ${formatRemaining(remaining)}`;
  }
}

if (libraryPanel) {
  refreshLibrary();
  setInterval(tickCountdowns, 1000);
  setInterval(refreshLibrary, 30000);
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}
