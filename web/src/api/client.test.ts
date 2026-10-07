import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";

function stubFetch(status: number, body: unknown) {
  const fetch = vi.fn(
    async (_input: string, _init?: RequestInit) => new Response(JSON.stringify(body), { status }),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => vi.unstubAllGlobals());

describe("api", () => {
  it("posts the question with an empty context and the chosen mode", async () => {
    const fetch = stubFetch(200, { case_id: "api-1" });

    await api.investigate("What was revenue?", "multi");

    expect(fetch).toHaveBeenCalledWith("/investigate", expect.objectContaining({ method: "POST" }));
    const init = fetch.mock.calls[0]?.[1];
    expect(JSON.parse(String(init?.body))).toEqual({ question: "What was revenue?", context: {}, mode: "multi" });
  });

  it("raises the API's own message on an error status", async () => {
    stubFetch(404, { detail: "No eval run 'x'. List eval runs at /evals." });

    await expect(api.evalRun("x")).rejects.toThrow(new ApiError("No eval run 'x'. List eval runs at /evals."));
  });

  it("encodes ids and the compare pair into the URL", async () => {
    const fetch = stubFetch(200, {});

    await api.compare("eval a", "eval/b");
    await api.run("run/1");

    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      "/evals/compare?baseline=eval+a&candidate=eval%2Fb",
      "/runs/run%2F1",
    ]);
  });
});
