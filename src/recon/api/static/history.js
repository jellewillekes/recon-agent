// Saved runs (GET /runs): reopen one without a live call, export it, and
// leave a 👍/👎 with a note (POST /runs/{id}/feedback).
const historyList = document.querySelector("#history-list");
let runHistory = false;

function historyEnabled() {
  return runHistory;
}

function setHistoryEnabled(enabled) {
  runHistory = enabled;
  if (enabled) {
    loadHistory();
  } else {
    historyList.replaceChildren(
      element("li", "no-data", "Run history is off: the API has no DATABASE_URL."),
    );
  }
}

// /capabilities couldn't be read, so whether history is on is unknown. Try
// /runs anyway: the saved runs are the demo's fallback, and an error there
// says why instead of leaving the list silently empty.
function loadHistoryWithoutCapabilities() {
  runHistory = true;
  loadHistory();
}

async function loadHistory() {
  if (!runHistory) return;
  try {
    const response = await fetch("/runs?limit=20");
    const payload = await response.json();
    if (!response.ok) throw new Error(describeError(payload, "Couldn't load saved runs."));
    if (!payload.runs.length) {
      historyList.replaceChildren(element("li", "no-data", "No saved runs yet."));
      return;
    }
    historyList.replaceChildren(...payload.runs.map(renderHistoryItem));
  } catch (error) {
    historyList.replaceChildren(element("li", "no-data", error.message));
  }
}

function renderHistoryItem(run) {
  const item = element("li");
  const button = element("button", "history-item");
  button.type = "button";
  button.append(element("span", "history-question", run.question));
  const meta = [
    run.mode,
    new Date(run.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
    formatCost(run.cost_eur),
    run.verified_evidence_count + " verified",
  ];
  if (run.feedback) meta.push(run.feedback === "up" ? "👍" : "👎");
  if (run.failed) meta.push("issue");
  button.append(element("span", "history-meta", meta.join(" · ")));
  button.addEventListener("click", () => openRun(run.run_id));
  item.append(button);
  return item;
}

async function openRun(runId) {
  showView("research");
  try {
    const response = await fetch("/runs/" + encodeURIComponent(runId));
    const run = await response.json();
    if (!response.ok) throw new Error(describeError(run, "Couldn't open that run."));
    question.value = run.question;
    question.dispatchEvent(new Event("input", { bubbles: true }));
    renderResult(run.result, {
      run_id: run.run_id,
      created_at: run.created_at,
      feedback: run.feedback,
      replayed: true,
    });
    setState("complete", "Saved run");
  } catch (error) {
    renderEmpty(error.message);
    setState("error", "Couldn't open run");
  }
}

function renderRunActions(saved) {
  const actions = element("div", "run-actions");
  const exportLink = element("a", "link-button", "Export JSON");
  exportLink.href = "/runs/" + encodeURIComponent(saved.run_id) + "/export";
  exportLink.setAttribute("download", "");

  const status = element("span", "feedback-status");
  const note = element("input", "feedback-note");
  note.type = "text";
  note.maxLength = 2000;
  note.placeholder = "Optional note for the reviewers";
  note.setAttribute("aria-label", "Feedback note");
  if (saved.feedback) {
    note.value = saved.feedback.note ?? "";
    status.textContent = "Rated " + (saved.feedback.rating === "up" ? "👍" : "👎");
  }

  const rate = (rating) => async () => {
    status.textContent = "Saving…";
    try {
      const response = await fetch("/runs/" + encodeURIComponent(saved.run_id) + "/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rating, note: note.value }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(describeError(payload, "Feedback wasn't saved."));
      status.textContent = "Saved " + (rating === "up" ? "👍" : "👎");
      loadHistory();
    } catch (error) {
      status.textContent = error.message;
    }
  };
  const up = element("button", "feedback-button", "👍");
  up.type = "button";
  up.setAttribute("aria-label", "Good answer");
  up.addEventListener("click", rate("up"));
  const down = element("button", "feedback-button", "👎");
  down.type = "button";
  down.setAttribute("aria-label", "Bad answer");
  down.addEventListener("click", rate("down"));

  actions.append(up, down, note, exportLink, status);
  return actions;
}

document.querySelector("#history-refresh").addEventListener("click", loadHistory);
