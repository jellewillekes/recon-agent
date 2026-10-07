# 0031: One failure class per case, checked in a fixed order

## Context

#116 asks for each case to say why it failed: `retrieval`, `reasoning`, `tool_use`,
`budget`, `runtime_error` or `none`. A case often shows more than one sign at once. A run
that hit its token budget may also have a failed tool call, and a wrong answer may follow
both an empty search and an unverified citation. Counting every sign would make the
per-run counts add up to more than the cases, and a reader couldn't tell the main cause.
The signals also have to come from the recorded trace and the existing scores, since a
model-based diagnosis would cost credit on every run.

## Decision

Each case gets at most one class: the first check below that applies.

1. `budget`: the runtime reports a breached budget ("budget of … exceeded"), even if the
   answer is complete. The run didn't finish within its limits.
2. `runtime_error`: any other error, with no answer.
3. No class, with a reason, when the case wasn't scored: the judge failed (#123) or no
   `correct_answer_score` cutoff is set.
4. `none`: the answer reached the cutoff in `config/thresholds.yaml`.
5. `tool_use`: a failed tool call was never followed by a usable call to the same tool,
   or the agent answered without calling any tool.
6. `retrieval`: on a labelled case, none of the labelled passages were retrieved, or
   every lookup came back empty.
7. `reasoning`: everything else. The tools returned data, but the answer was wrong.

The case's `trajectory` keeps every signal, so a reader who disagrees with the order can
still see the rest.

## Consequences

- The order favours causes earlier in the run. A wrong answer after an unrecovered tool
  error counts as `tool_use`, even if the reasoning was also poor.
- `retrieval` is only measured well on labelled cases (`evals/retrieval-labels.yaml`).
  Elsewhere it relies on every lookup coming back empty, so a search that returned the
  wrong rows counts as `reasoning`.
- "Answered without calling a tool" counts as `tool_use`, which is wrong for a question
  that needs no lookup. None of the current cases are like that.
- The classes rest on the cutoff the user set (0.5), so changing it moves cases between
  `none` and the failure classes.
