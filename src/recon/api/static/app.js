const form = document.querySelector("#question-form");
const question = document.querySelector("#question");
const submitButton = document.querySelector("#submit-button");
const buttonLabel = submitButton.querySelector(".button-label");
const characterCount = document.querySelector("#character-count");
const answerCard = document.querySelector(".answer-card");
const answerContent = document.querySelector("#answer-content");
const runState = document.querySelector("#run-state");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setState(state, label) {
  runState.dataset.state = state;
  runState.lastChild.textContent = " " + label;
  answerCard.setAttribute("aria-busy", state === "running" ? "true" : "false");
}

function describeError(payload, fallback) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (payload?.detail) return JSON.stringify(payload.detail);
  if (payload?.fields?.length) {
    return "Check the question fields: " + payload.fields.join(", ") + ".";
  }
  return fallback;
}

function renderEmpty(message) {
  answerContent.replaceChildren(element("div", "error-box", message));
}

function renderResult(result) {
  const wrapper = element("div", "result");
  const meta = element("div", "result-meta");
  const confidence = element(
    "span",
    "confidence",
    (result.confidence ?? "unknown") + " confidence",
  );
  confidence.dataset.confidence = result.confidence ?? "unknown";
  meta.append(
    confidence,
    element("span", "run-reference", "Run " + (result.case_id ?? "unknown")),
  );
  wrapper.append(meta);

  if (result.error) {
    wrapper.append(
      element("p", "result-warning", "The agent reported an issue: " + result.error),
    );
  }
  wrapper.append(element("p", "answer-text", result.answer || "The agent returned no answer."));
  wrapper.append(element("hr", "result-divider"));

  const claims = Array.isArray(result.claims) ? result.claims : [];
  if (claims.length) {
    renderClaims(wrapper, claims, result.evidence_items ?? []);
  } else {
    renderEvidenceStrings(wrapper, Array.isArray(result.evidence) ? result.evidence : []);
  }

  wrapper.append(element("hr", "result-divider"));
  const calls = Array.isArray(result.tool_calls) ? result.tool_calls : [];
  const toolsHeading = element("h3", "detail-heading", "Research trace");
  toolsHeading.append(
    element("small", "", calls.length + " tool call" + (calls.length === 1 ? "" : "s")),
  );
  wrapper.append(toolsHeading);
  if (calls.length) {
    const list = element("div", "tool-list");
    calls.forEach((call, index) => list.append(renderToolCall(call, index)));
    wrapper.append(list);
  } else {
    wrapper.append(element("p", "no-data", "No data tools were called."));
  }

  const stats = element("div", "result-stats");
  stats.append(stat("Elapsed", formatDuration(result.elapsed_ms)));
  stats.append(stat("Cost", formatCost(result.cost_eur)));
  wrapper.append(stats);
  answerContent.replaceChildren(wrapper);
}

// Claims with their sources (ADR 0030). The source details come from the
// rows the tools returned; an unverified citation is shown as such.
function renderClaims(wrapper, claims, evidenceItems) {
  const byRef = new Map(evidenceItems.map((item) => [item.ref, item]));
  const heading = element("h3", "detail-heading", "Claims and sources");
  heading.append(element("small", "", claims.length + " claim" + (claims.length === 1 ? "" : "s")));
  wrapper.append(heading);
  const list = element("ol", "claim-list");
  for (const claim of claims) {
    const item = element("li", "claim-item");
    item.dataset.importance = claim.importance;
    item.append(element("p", "claim-text", claim.text));
    const refs = claim.evidence_refs ?? [];
    if (!refs.length) item.append(element("p", "no-data", "No source cited."));
    for (const ref of refs) item.append(renderSource(byRef.get(ref) ?? { ref, verified: false }));
    list.append(item);
  }
  wrapper.append(list);
}

function renderSource(evidence) {
  const details = element("details", "source-item");
  const summary = element("summary");
  const badge = element("span", "source-badge", evidence.verified ? "verified" : "unverified");
  badge.dataset.verified = String(Boolean(evidence.verified));
  const parts = [
    evidence.company_id,
    evidence.form,
    evidence.filed ? "filed " + evidence.filed : null,
    evidence.section,
  ].filter(Boolean);
  summary.append(badge, element("span", "source-label", parts.join(" · ") || evidence.ref));
  details.append(summary);
  if (evidence.verified) {
    details.append(element("blockquote", "source-excerpt", evidence.excerpt ?? ""));
    if (evidence.accession) details.append(element("p", "source-meta", "Accession " + evidence.accession));
  } else {
    details.append(element("p", "source-meta", "No tool returned " + evidence.ref + " in this run."));
  }
  return details;
}

function renderEvidenceStrings(wrapper, evidence) {
  const evidenceHeading = element("h3", "detail-heading", "Evidence");
  evidenceHeading.append(
    element("small", "", evidence.length + " item" + (evidence.length === 1 ? "" : "s")),
  );
  wrapper.append(evidenceHeading);
  if (evidence.length) {
    const list = element("ul", "evidence-list");
    for (const item of evidence) {
      list.append(element("li", "evidence-item", String(item)));
    }
    wrapper.append(list);
  } else {
    wrapper.append(element("p", "no-data", "No evidence was returned for this answer."));
  }
}

function renderToolCall(call, index) {
  const details = element("details", "tool-item");
  const summary = element("summary");
  const status = String(call.status ?? "unknown");
  const statusBadge = element("span", "tool-status", status.replaceAll("_", " "));
  statusBadge.dataset.status = status;
  summary.append(
    element("span", "tool-name", String(call.tool ?? "tool") + " · " + (index + 1)),
    statusBadge,
    element("span", "tool-time", formatDuration(call.elapsed_ms)),
  );
  const args = element("pre", "tool-arguments", JSON.stringify(call.arguments ?? {}, null, 2));
  details.append(summary, args);
  return details;
}

function stat(label, value) {
  const item = element("span");
  item.append(element("strong", "", label + " "), document.createTextNode(value));
  return item;
}

function formatDuration(milliseconds) {
  const value = Number(milliseconds);
  if (!Number.isFinite(value)) return "—";
  return value >= 1000 ? (value / 1000).toFixed(1) + "s" : Math.round(value) + "ms";
}

function formatCost(euros) {
  const value = Number(euros);
  if (!Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 4,
  }).format(value);
}

question.addEventListener("input", () => {
  characterCount.textContent = question.value.length + " / 2000";
});

document.querySelectorAll(".example-chip").forEach((button) => {
  button.addEventListener("click", () => {
    question.value = button.dataset.question ?? "";
    question.dispatchEvent(new Event("input", { bubbles: true }));
    question.focus();
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = question.value.trim();
  if (!value) {
    renderEmpty("Enter a research question before starting.");
    setState("error", "Question required");
    question.focus();
    return;
  }

  submitButton.disabled = true;
  buttonLabel.textContent = "Researching…";
  setState("running", "Researching");
  answerContent.replaceChildren(
    element("div", "empty-state loading-state", "Running the research agent…"),
  );

  try {
    const response = await fetch("/investigate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: value, context: {} }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        describeError(payload, "The request failed (" + response.status + "). Try again."),
      );
    }
    renderResult(payload);
    setState(
      payload.error ? "error" : "complete",
      payload.error ? "Completed with issue" : "Complete",
    );
  } catch (error) {
    const message = error instanceof TypeError
      ? "Could not reach the local research service. Make sure the app is running, then try again."
      : error.message || "The research request failed. Try again.";
    renderEmpty(message);
    setState("error", "Request failed");
  } finally {
    submitButton.disabled = false;
    buttonLabel.textContent = "Run research";
  }
});
