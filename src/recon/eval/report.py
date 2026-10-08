"""Markdown summary for an `EvalRun`, written alongside its JSON record."""

from recon.contracts import EvalRun
from recon.eval import claim_gate
from recon.eval.gate import incomplete_reasons
from recon.eval.intervals import run_interval

_INTERVAL_METRICS = (
    ("task_completion_rate", "Wilson score"),
    ("answer_score_mean", "Student t over cases"),
    ("cost_per_correct_answer_eur", "bootstrap over cases"),
)


def _uncertainty(run: EvalRun, correct_answer_score: float | None) -> list[str]:
    """The run's 95% intervals as a markdown section (ADR 0036)."""
    lines = [
        "",
        "## Uncertainty",
        "",
        (
            f"95% intervals over this run's {len(run.case_scores)} cases. They show "
            "how far a score could move on other cases, not how much a rerun of "
            "these cases moves (docs/eval-noise.md)."
        ),
        "",
        "| Metric | Value | 95% interval | Method |",
        "|---|---|---|---|",
    ]
    for metric, method in _INTERVAL_METRICS:
        interval = run_interval(run, metric, correct_answer_score)
        value = run.aggregate.get(metric)
        if interval is None or value is None:
            continue
        lines.append(
            f"| {metric} | {value:.3f} | {interval.low:.3f} to {interval.high:.3f} "
            f"| {method} |"
        )
    if run_interval(run, "answer_score_mean") is None:
        lines += ["", "No interval for the answer score: it needs at least two cases."]
    return lines


def _claims(run: EvalRun) -> list[str]:
    """How the run's claims were verified (#139, ADR 0038)."""
    counts = claim_gate.verdict_counts(run)
    if counts is None:
        return ["", "## Claims", "", "Not verified in this run."]
    rate = claim_gate.bad_rate(run)
    share = "no claim could be checked" if rate is None else f"{rate:.1%}"
    lines = [
        "",
        "## Claims",
        "",
        (
            f"Verifier: {run.verifier_version}. Unsupported or contradicted: "
            f"{share} of the claims checked."
        ),
        "",
        "| Verdict | Claims |",
        "|---|---|",
        *[f"| {verdict} | {count} |" for verdict, count in counts.items()],
    ]
    contradicted = [
        f'- {case.case_id}: "{v.text}"'
        for case in run.case_scores
        for v in case.verifications or []
        if v.verdict == "CONTRADICTED"
    ]
    if contradicted:
        lines += ["", "Contradicted claims:", "", *contradicted]
    return lines


def render_markdown(run: EvalRun, correct_answer_score: float | None = None) -> str:
    lines = [
        f"# Evaluation run {run.run_id}",
        "",
        f"- Dataset: {run.dataset} ({run.dataset_license})",
        f"- Runtime: {run.runtime} / mode: {run.mode}",
        f"- Timestamp: {run.timestamp_utc.isoformat()}",
        f"- Rubric version: {run.rubric_version}",
        f"- Tool data: {run.tool_data_snapshot or 'not recorded'}",
        f"- Retrieval labels: {run.retrieval_labels_hash or 'not scored'}",
        f"- Routing: {'decompose on the local model' if run.routing else 'off'}",
        f"- Model config hash: {run.model_config_hash}",
        "- Prompt hashes: "
        + ", ".join(
            f"{role} {digest}" for role, digest in sorted(run.prompt_hashes.items())
        ),
        f"- Cases: {len(run.case_scores)}",
        f"- Total cost: €{run.total_cost_eur:.4f}",
        "",
        "## Aggregate",
        "",
        *[f"Not comparable: {reason}." for reason in incomplete_reasons(run)],
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for key in sorted(run.aggregate):
        lines.append(f"| {key} | {run.aggregate[key]:.3f} |")

    lines += _uncertainty(run, correct_answer_score)
    lines += _claims(run)
    lines += [
        "",
        "## Per-case",
        "",
        (
            "| case_id | task_completion | answer_score | failure_class "
            "| tool_call_accuracy | cost_eur | tools | notes |"
        ),
        "|---|---|---|---|---|---|---|---|",
    ]
    for score in run.case_scores:
        notes = score.notes.replace("|", "/") or "-"
        tools = ", ".join(score.tool_names) or "-"
        answer = "unscored" if score.judge_failed else f"{score.answer_score:.2f}"
        lines.append(
            f"| {score.case_id} | {score.task_completion} | {answer} | "
            f"{score.failure_class or '-'} | {score.tool_call_accuracy:.2f} | "
            f"{score.cost_eur:.4f} | {tools} | {notes} |"
        )

    return "\n".join(lines) + "\n"
