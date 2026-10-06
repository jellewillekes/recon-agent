"""Markdown summary for an `EvalRun`, written alongside its JSON record."""

from recon.contracts import EvalRun


def render_markdown(run: EvalRun) -> str:
    lines = [
        f"# Evaluation run {run.run_id}",
        "",
        f"- Dataset: {run.dataset} ({run.dataset_license})",
        f"- Runtime: {run.runtime} / mode: {run.mode}",
        f"- Timestamp: {run.timestamp_utc.isoformat()}",
        f"- Rubric version: {run.rubric_version}",
        f"- Tool data: {run.tool_data_snapshot or 'not recorded'}",
        f"- Retrieval labels: {run.retrieval_labels_hash or 'not scored'}",
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
        "| Metric | Value |",
        "|---|---|",
    ]
    for key in sorted(run.aggregate):
        lines.append(f"| {key} | {run.aggregate[key]:.3f} |")

    lines += [
        "",
        "## Per-case",
        "",
        (
            "| case_id | task_completion | answer_score | tool_call_accuracy "
            "| cost_eur | tools | notes |"
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for score in run.case_scores:
        notes = score.notes.replace("|", "/") or "-"
        tools = ", ".join(score.tool_names) or "-"
        lines.append(
            f"| {score.case_id} | {score.task_completion} | {score.answer_score:.2f} | "
            f"{score.tool_call_accuracy:.2f} | {score.cost_eur:.4f} | {tools} | {notes} |"
        )

    return "\n".join(lines) + "\n"
