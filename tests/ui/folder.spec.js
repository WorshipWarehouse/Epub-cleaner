"use strict";

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { test, expect } = require("@playwright/test");

const fixture = path.join(__dirname, ".tmp", "branded.epub");
const bookFolder = path.join(__dirname, ".tmp", "Unzipped Book.epub");

// A book that arrived extracted: the same members as the fixture, on disk,
// instead of a ZIP.
test.beforeAll(() => {
  fs.rmSync(bookFolder, { recursive: true, force: true });
  execFileSync(
    path.join(__dirname, "..", "..", ".venv", "bin", "python"),
    [
      "-c",
      "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])",
      fixture,
      bookFolder,
    ],
  );
});

test("choosing an unzipped book folder inspects and cleans it", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  await page.goto("/");
  await expect(page.locator("#results")).toBeHidden();

  // Playwright populates webkitRelativePath for a directory, which is exactly
  // what the page sends to the server.
  await page.setInputFiles("#folder-input", bookFolder);
  await expect(page.locator("#file-name")).toContainText("unzipped book");

  const inspectResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/api/inspect-folder") && response.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.click("#inspect-btn");

  const response = await inspectResponse;
  expect(response.status()).toBe(200);
  expect((await response.json()).file_name).toBe("Unzipped Book.epub");

  await expect(page.locator("#results")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#findings")).toContainText("security.script");

  await page.click("#clean-btn");
  await expect(page.locator("#cleaned")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#download-link")).toHaveAttribute("href", /\/api\/download\//);
  expect(pageErrors).toEqual([]);
});
