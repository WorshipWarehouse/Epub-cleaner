"use strict";

// All values from the server are rendered with textContent (never innerHTML),
// because a malicious EPUB can put arbitrary strings in its metadata.

const state = {
  file: null,
  jobId: null,
  report: null,
  selected: new Set(),
  busy: false,
};

const $ = (id) => document.getElementById(id);

const fileInput = $("file-input");
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
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".epub")) {
    setStatus("That is not an .epub file. Pick the book you want to inspect.", true);
    return;
  }
  state.file = file;
  fileName.textContent = `${file.name} · ${formatBytes(file.size)}`;
  fileName.hidden = false;
  inspectBtn.disabled = false;
  setStatus("");
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));

for (const eventName of ["dragenter", "dragover"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("dragover");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragover");
  });
}
dropzone.addEventListener("drop", (event) => {
  const file = event.dataTransfer && event.dataTransfer.files[0];
  chooseFile(file);
});

// -- inspect -------------------------------------------------------------
inspectBtn.addEventListener("click", async () => {
  if (!state.file || state.busy) return;
  state.busy = true;
  inspectBtn.disabled = true;
  setStatus("Reading the archive and scanning every resource…");

  const body = new FormData();
  body.append("file", state.file);

  try {
    const response = await fetch("/api/inspect", { method: "POST", body });
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
  state.jobId = null;
  state.report = null;
  state.selected = new Set();
  fileInput.value = "";
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
            ? `${source.result_count} result${source.result_count === 1 ? "" : "s"}`
            : "unavailable",
        }),
      ]);
      if (source.error) row.append(make("p", { class: "meta", text: source.error }));
      sourceList.append(row);
    }
  }

  function renderListing(listing) {
    const coverPath = safeCover(listing.cover_url);
    const initial = (listing.title || "?").trim().charAt(0).toUpperCase() || "?";
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
    if (listing.authors && listing.authors.length) meta.push(listing.authors.join(", "));
    if (listing.formats && listing.formats.length) meta.push(listing.formats.join(" · "));
    if (listing.languages && listing.languages.length) meta.push(listing.languages.join(", "));

    const children = [
      make("p", { class: "result-source", text: listing.source_label }),
      make("h3", { class: "result-title", text: listing.title }),
    ];
    if (meta.length) children.push(make("p", { class: "meta", text: meta.join(" · ") }));
    if (listing.download_note) children.push(make("p", { class: "meta", text: listing.download_note }));

    if (listing.security && listing.security.status) {
      const badgeText =
        listing.security.status === "scan_on_fetch" ? "scan on fetch" : "search link only";
      children.push(
        make("p", { class: "meta security" }, [
          make("span", { class: `badge ${listing.downloadable ? "good" : "muted"}`, text: badgeText }),
          ` ${listing.security.headline || ""}`,
        ]),
      );
    }

    if (listing.downloadable) {
      const button = make("button", {
        class: "button primary small",
        type: "button",
        text: "Fetch, scan and clean",
      });
      button.addEventListener("click", () => startTransfer(listing, button));
      children.push(make("div", { class: "result-actions" }, [button]));
    } else {
      const href = safeHref(listing.details_url);
      if (href) {
        children.push(
          make("div", { class: "result-actions" }, [
            make("a", {
              class: "button ghost small",
              href,
              target: "_blank",
              rel: "noopener noreferrer",
              text: listing.is_search_link
                ? `Search ${listing.source_label}`
                : "Open source page",
            }),
          ]),
        );
      }
    }

    return make("li", { class: "result-card" }, [
      cover,
      make("div", { class: "result-body" }, children),
    ]);
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

    transferBody.replaceChildren(
      make("p", { class: "verdict", text: security.headline || "Cleaned and verified." }),
      list,
      make("p", {
        class: "meta",
        text: `Removed ${summary.removed_count || 0} item(s); ${security.needs_review || 0} ambiguous item(s) left in place.`,
      }),
      make("p", { class: "download-line" }, [download]),
      make("p", { class: "meta", text: security.disclaimer || "" }),
      make("p", { class: "meta", text: security.safety_notice || "" }),
    );
    transferPanel.hidden = false;
    transferPanel.scrollIntoView({ behavior: "smooth", block: "start" });
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

    try {
      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Search failed.");
      renderSourceStatuses(payload.sources);
      searchResults.replaceChildren();
      for (const listing of payload.results) searchResults.append(renderListing(listing));
      if (!payload.results.length) {
        searchResults.append(
          make("li", { class: "result-empty", text: "No listings matched that search." }),
        );
      }
      setSearchStatus(`${payload.results.length} listing(s) from ${payload.sources.length} source(s).`);
    } catch (error) {
      setSearchStatus(error.message, true);
    } finally {
      state.busy = false;
    }
  });
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
