"use strict";

// All values from the server are rendered with textContent (never innerHTML),
// because a malicious EPUB can put arbitrary strings in its metadata.

const state = {
  file: null,
  folder: null,
  jobId: null,
  report: null,
  selected: new Set(),
  busy: false,
};

const $ = (id) => document.getElementById(id);

const fileInput = $("file-input");
const folderInput = $("folder-input");
const folderBtn = $("folder-btn");
const dropzone = $("dropzone");
const fileName = $("file-name");
const inspectBtn = $("inspect-btn");
const resetBtn = $("reset-btn");
const cleanBtn = $("clean-btn");
const status = $("status");

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
function chooseFile(file) {
  if (!file) return false;
  if (!file.name.toLowerCase().endsWith(".epub")) {
    setStatus("That is not an .epub file. Pick the book you want to inspect.", true);
    return false;
  }
  state.file = file;
  state.folder = null;
  folderInput.value = "";
  fileName.textContent = `${file.name} · ${formatBytes(file.size)}`;
  fileName.hidden = false;
  inspectBtn.disabled = false;
  setStatus("");
  return true;
}

// A book can also arrive as an extracted folder. The browser sends each file
// with its path inside the folder, and the server packs the tree back into an
// EPUB before the ordinary scan runs.
const TREE_MARKERS = ["mimetype", "META-INF/container.xml"];

function chooseFolder(files) {
  const list = Array.from(files || []);
  if (!list.length) return false;

  const paths = list.map((file) => file.webkitRelativePath || file.name);
  const root = String(paths[0].split("/")[0] || "book").trim() || "book";
  const relative = paths.map((path) => path.split("/").slice(1).join("/"));
  const missing = TREE_MARKERS.filter((marker) => !relative.includes(marker));
  if (missing.length) {
    setStatus(
      `That folder is not an unzipped EPUB: it has no ${missing.join(" or ")}.`,
      true,
    );
    return false;
  }

  const size = list.reduce((total, file) => total + file.size, 0);
  state.folder = { root, files: list, paths };
  state.file = null;
  fileInput.value = "";
  fileName.textContent = `${root} · unzipped book · ${list.length} files · ${formatBytes(size)}`;
  fileName.hidden = false;
  inspectBtn.disabled = false;
  setStatus("");
  return true;
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));
folderBtn.addEventListener("click", () => folderInput.click());
folderInput.addEventListener("change", () => chooseFolder(folderInput.files));

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
  const item = event.dataTransfer.items && event.dataTransfer.items[0];
  const entry = item && item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
  if (entry && entry.isDirectory) {
    setStatus("That is an unzipped book folder — use “Choose unzipped folder” so it can be packed first.", true);
    return;
  }
  const file = event.dataTransfer.files[0];
  // A dropped book goes straight to inspection: one drag from the download
  // shelf to a finished report.
  if (chooseFile(file)) inspectBtn.click();
});

// -- inspect -------------------------------------------------------------
inspectBtn.addEventListener("click", async () => {
  if ((!state.file && !state.folder) || state.busy) return;
  state.busy = true;
  inspectBtn.disabled = true;

  const body = new FormData();
  let endpoint = "/api/inspect";
  if (state.folder) {
    endpoint = "/api/inspect-folder";
    setStatus("Packing the folder and scanning every resource…");
    body.append("folder", state.folder.root);
    state.folder.files.forEach((file, index) => {
      body.append("files", file, state.folder.paths[index]);
    });
  } else {
    setStatus("Reading the archive and scanning every resource…");
    body.append("file", state.file);
  }

  try {
    const response = await fetch(endpoint, { method: "POST", body });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "The file could not be inspected.");

    state.jobId = payload.job_id;
    state.report = payload.report;
    state.selected = new Set();
    renderReport(payload.report, payload.reviewable || []);
    setStatus("");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    state.busy = false;
    inspectBtn.disabled = false;
  }
});

function renderReport(report, reviewable) {
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
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selected.add(finding.id);
        else state.selected.delete(finding.id);
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

  cleanBtn.disabled = false;
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
cleanBtn.addEventListener("click", async () => {
  if (!state.jobId || state.busy) return;
  state.busy = true;
  cleanBtn.disabled = true;
  setStatus("Rebuilding the book…");

  try {
    const response = await fetch("/api/clean", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: state.jobId,
        approved: Array.from(state.selected),
        approve_all: $("opt-approve").checked,
        remove_remote_resources: $("opt-remote").checked,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Cleaning failed.");

    renderCleaned(payload.report);
    setStatus("");
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    state.busy = false;
    cleanBtn.disabled = false;
  }
});

function renderCleaned(report) {
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
  state.file = null;
  state.folder = null;
  state.jobId = null;
  state.report = null;
  state.selected = new Set();
  fileInput.value = "";
  folderInput.value = "";
  fileName.hidden = true;
  inspectBtn.disabled = true;
  resetBtn.hidden = true;
  $("results").hidden = true;
  $("cleaned").hidden = true;
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
