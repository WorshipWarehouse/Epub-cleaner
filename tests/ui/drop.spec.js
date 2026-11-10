"use strict";

const fs = require("fs");
const path = require("path");
const { test, expect } = require("@playwright/test");

const fixture = path.join(__dirname, ".tmp", "branded.epub");

test("a book dropped anywhere starts and completes inspection", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  await page.goto("/");
  await expect(page.locator("#results")).toBeHidden();

  const payload = fs.readFileSync(fixture).toString("base64");

  const inspectResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/api/inspect") && response.request().method() === "POST",
    { timeout: 30_000 },
  );

  // A real File in a real DataTransfer, dropped on <main> rather than the
  // dashed dropzone, so this proves the window-wide handler.
  await page.evaluate(({ payload }) => {
    const binary = atob(payload);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    const file = new File([bytes], "branded.epub", { type: "application/epub+zip" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    const target = document.querySelector("main");
    target.dispatchEvent(
      new DragEvent("drop", {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }),
    );
  }, { payload });

  const response = await inspectResponse;
  expect(response.status()).toBe(200);

  await expect(page.locator("#results")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("#findings")).toContainText("security.script");
  await expect(page.locator("#status")).toHaveText("");
  expect(pageErrors).toEqual([]);
});
