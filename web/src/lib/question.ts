// Putting a company picked from the dropdown into the research question.

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** `question` with `ticker` in front ("WDAY: …"). A ticker picked earlier
 * is swapped out, and a question that already names the ticker is left as
 * it is. An empty `ticker` removes an earlier pick. `known` lists the
 * tickers the dropdown offers, so only an earlier pick is ever removed. */
export function withTicker(question: string, ticker: string, known?: readonly string[]): string {
  const prefix = /^([A-Z][A-Z0-9.-]{0,9}): /.exec(question);
  const picked = prefix && (!known || known.includes(prefix[1] ?? "")) ? prefix[0] : "";
  const rest = question.slice(picked.length);
  if (!ticker) return rest;
  if (new RegExp(`\\b${escape(ticker)}\\b`).test(rest)) return rest;
  return `${ticker}: ${rest}`;
}
