// Example questions per data source. The EDGAR ones are benchmark questions
// the agent answered well on the text cases (evals/text-cases.txt).
export const EXAMPLES: Record<string, [string, string][]> = {
  edgar: [
    ["Retention metric", "Does Workday (NASDAQ: WDAY) report a gross or net retention metric in its annual or quarterly reporting? If so, provide the definition"],
    ["Regulatory risks", "Summarize the regulatory risks Paylocity's (NASDAQ: PCTY) lists in its FY 2024 10-K."],
    ["New facility", "When is production expected to begin in J M Smucker's (NYSE: SJ) new distribution center in McCalla, Alabama?"],
  ],
  fixture: [
    ["Revenue trend", "How has Aurora Robotics Corp's revenue changed from 2022 to 2024?"],
    ["Filing risks", "What risks appear in Aurora Robotics Corp's recent filings?"],
  ],
};

export const DATA_NOTES: Record<string, string> = {
  edgar: "Tools query a pinned snapshot of SEC EDGAR filings and XBRL facts. Answers cite the rows the tools returned.",
  fixture: "Tools query synthetic fixtures. Companies and filings are fictional and only illustrate the workflow.",
};
