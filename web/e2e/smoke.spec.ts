// Smoke tests for #121: ask a question, open a citation, reopen a saved run,
// compare two evaluation runs. Every API call is answered from synthetic
// fixtures, so nothing runs an agent or costs credit.
import { expect, test, type Page } from "@playwright/test";
import { capabilities, comparison, evalRun, evals, result, run, run2, runs } from "./fixtures";

async function stubApi(page: Page) {
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/capabilities", (route) => route.fulfill(json(capabilities)));
  await page.route("**/runs?limit=20", (route) => route.fulfill(json(runs)));
  await page.route("**/runs/api-saved1", (route) => route.fulfill(json(run)));
  await page.route("**/runs/api-saved2", (route) => route.fulfill(json(run2)));
  await page.route("**/runs/*/feedback", (route) => route.fulfill(json({ status: "ok" })));
  await page.route("**/investigate", (route) => route.fulfill(json(result)));
  await page.route("**/evals", (route) => route.fulfill(json(evals)));
  await page.route("**/evals/eval-on", (route) => route.fulfill(json(evalRun)));
  await page.route("**/evals/compare?*", (route) => route.fulfill(json(comparison)));
}

test.beforeEach(async ({ page }) => {
  await stubApi(page);
});

test("ask a question and open a citation", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Synthetic fixtures", { exact: true })).toBeVisible();

  await page.getByLabel("Research question").fill("How did FIRM-001's revenue change?");
  await page.getByRole("tabpanel", { name: "Research" }).getByLabel("Mode").selectOption("multi");
  const request = page.waitForRequest("**/investigate");
  await page.getByRole("button", { name: /Run research/ }).click();
  expect((await request).postDataJSON()).toEqual({
    question: "How did FIRM-001's revenue change?",
    context: {},
    mode: "multi",
  });

  await expect(page.getByText("Revenue was 120 million in FY2024.")).toBeVisible();
  const source = page.locator(".source-item").filter({ hasText: "FIRM-001 · 10-K" });
  await source.locator("summary").click();
  await expect(source.getByText("Revenue FY2024 = 120000000")).toBeVisible();
  await expect(page.getByText("No tool returned E000000000bb2 in this run.")).toBeHidden();
});

test("reopen a saved run without a new agent call", async ({ page }) => {
  let investigated = false;
  await page.route("**/investigate", (route) => {
    investigated = true;
    return route.abort();
  });
  await page.goto("/");

  await page.getByRole("button", { name: /How did FIRM-001's revenue change\?/ }).click();

  await expect(page.getByText(/reopened from the run store, no new agent call/)).toBeVisible();
  await expect(page.getByLabel("Research question")).toHaveValue("How did FIRM-001's revenue change?");
  await page.getByRole("button", { name: "Good answer" }).click();
  await expect(page.getByRole("status")).toHaveText("Saved 👍");
  expect(investigated).toBe(false);
});

test("each reopened run gets its own feedback box", async ({ page }) => {
  // Review of #121: the first run's rating and note carried over to the next.
  await page.goto("/");
  await page.getByRole("button", { name: /How did FIRM-001's revenue change\?/ }).click();
  await expect(page.getByRole("status")).toHaveText("Rated 👍");
  await expect(page.getByLabel("Feedback note")).toHaveValue("wrong ticker");

  await page.getByRole("button", { name: /What risks does FIRM-001 list\?/ }).click();
  await expect(page.getByLabel("Research question")).toHaveValue("What risks does FIRM-001 list?");
  await expect(page.getByRole("status")).toHaveText("");
  await expect(page.getByLabel("Feedback note")).toHaveValue("");
  const posted = page.waitForRequest("**/runs/api-saved2/feedback");
  await page.getByRole("button", { name: "Bad answer" }).click();
  expect((await posted).postDataJSON()).toEqual({ rating: "down", note: "" });
});

test("saved runs still load when capabilities can't be read", async ({ page }) => {
  await page.route("**/capabilities", (route) => route.fulfill({ status: 500, body: "{}" }));
  await page.goto("/");

  await expect(page.getByText("Data source unknown")).toBeVisible();
  await expect(page.getByRole("button", { name: /How did FIRM-001's revenue change\?/ })).toBeVisible();
});

test("an empty question asks for one instead of calling the agent", async ({ page }) => {
  let investigated = false;
  await page.route("**/investigate", (route) => {
    investigated = true;
    return route.abort();
  });
  await page.goto("/");

  await page.getByRole("button", { name: /Run research/ }).click();

  await expect(page.getByText("Enter a research question before starting.")).toBeVisible();
  expect(investigated).toBe(false);
});

test("the evaluation view retries after a failed load", async ({ page }) => {
  let calls = 0;
  await page.route("**/evals", (route) =>
    ++calls === 1 ? route.fulfill({ status: 500, body: "{}" }) : route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(evals) }),
  );
  await page.goto("/#evaluation");
  await expect(page.getByText(/Couldn't load eval runs/)).toBeVisible();

  await page.getByRole("tab", { name: "Research" }).click();
  await page.getByRole("tab", { name: "Evaluation" }).click();

  await expect(page.getByRole("button", { name: "eval-on" })).toBeVisible();
});

test("compare two evaluation runs and open one", async ({ page }) => {
  await page.goto("/#evaluation");

  await expect(page.getByRole("tab", { name: "Evaluation" })).toHaveAttribute("aria-selected", "true");
  // Opens on routing off against routing on, the step 14 comparison.
  await expect(page.getByLabel("Baseline run")).toHaveValue("eval-off");
  await expect(page.getByLabel("Candidate run")).toHaveValue("eval-on");
  await expect(page.locator("#compare-result .verdict[data-verdict='better']")).toBeVisible();
  await expect(page.locator("tr", { hasText: "eval-old" }).getByText("incomplete")).toBeVisible();

  await page.getByLabel("Filter by rubric").selectOption("3");
  await expect(page.getByRole("button", { name: "eval-on" })).toBeHidden();
  await page.getByLabel("Filter by rubric").selectOption("");

  await page.getByRole("button", { name: "eval-on" }).click();
  await expect(page.getByText("Not every case was scored.")).toBeVisible();
  await expect(page.getByRole("cell", { name: "unscored" })).toBeVisible();
});

test("the tabs work from the keyboard", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("tab", { name: "Research" }).focus();

  await page.keyboard.press("ArrowRight");

  await expect(page.getByRole("tab", { name: "Evaluation" })).toBeFocused();
  await expect(page.getByRole("tabpanel", { name: "Evaluation" })).toBeVisible();
  await expect(page.getByRole("tabpanel", { name: "Research" })).toBeHidden();
});
