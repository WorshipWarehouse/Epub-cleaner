"use strict";

const fs = require("fs");
const path = require("path");
const { test, expect } = require("@playwright/test");

const fixture = path.join(__dirname, ".tmp", "branded.epub");

test("multiple dropped EPUBs are inspected as separate queue entries", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  await page.goto("/");
  await expect(page.locator("#file-queue")).toBeHidden();
  await expect(page.locator("#file-input")).toHaveAttribute("multiple", "");

  const payload = fs.readFileSync(fixture).toString("base64");

  await page.evaluate(({ payload }) => {
    const binary = atob(payload);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], "first.epub", { type: "application/epub+zip" }));
    transfer.items.add(new File([bytes], "second.epub", { type: "application/epub+zip" }));
    const target = document.querySelector("main");
    target.dispatchEvent(
      new DragEvent("drop", {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }),
    );
  }, { payload });

  await expect(page.locator("#file-queue .queue-item")).toHaveCount(2, { timeout: 10_000 });
  await expect(page.locator('#file-queue .queue-item[data-status="ready"]')).toHaveCount(2, {
    timeout: 30_000,
  });
  await expect(page.locator("#file-queue .queue-item.active")).toHaveCount(1);
  await expect(page.locator("#file-queue")).toContainText("first.epub");
  await expect(page.locator("#file-queue")).toContainText("second.epub");
  await expect(page.locator("#results")).toBeVisible();
  await expect(page.locator("#status")).toHaveText("2 files inspected.");
  expect(pageErrors).toEqual([]);
});

test("EPUB files without an extension are accepted by MIME type", async ({ page }) => {
  await page.goto("/");
  const payload = fs.readFileSync(fixture).toString("base64");

  await page.setInputFiles("#file-input", [
    {
      name: "downloaded-book",
      mimeType: "application/epub+zip",
      buffer: Buffer.from(payload, "base64"),
    },
  ]);

  await expect(page.locator("#file-queue .queue-item")).toHaveCount(1);
  await expect(page.locator("#status")).not.toContainText("not .epub");
});

test("EPUB filenames with surrounding whitespace are accepted", async ({ page }) => {
  await page.goto("/");
  const payload = fs.readFileSync(fixture).toString("base64");

  await page.setInputFiles("#file-input", [
    {
      name: "  spaced-book.epub  ",
      mimeType: "application/octet-stream",
      buffer: Buffer.from(payload, "base64"),
    },
  ]);

  await expect(page.locator("#file-queue .queue-item")).toHaveCount(1);
  await expect(page.locator("#status")).not.toContainText("not .epub");
});

test("more than ten EPUBs can be queued at once", async ({ page }) => {
  await page.goto("/");
  const payload = fs.readFileSync(fixture).toString("base64");
  const buffer = Buffer.from(payload, "base64");

  await page.setInputFiles(
    "#file-input",
    Array.from({ length: 11 }, (_, index) => ({
      name: `book-${index + 1}.epub`,
      mimeType: "application/epub+zip",
      buffer,
    })),
  );

  await expect(page.locator("#file-queue .queue-item")).toHaveCount(11);
});

test("cleaning multiple inspected EPUBs offers one ZIP download", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  await page.goto("/");
  const payload = fs.readFileSync(fixture).toString("base64");
  await page.setInputFiles("#file-input", [
    { name: "first.epub", mimeType: "application/epub+zip", buffer: Buffer.from(payload, "base64") },
    { name: "second.epub", mimeType: "application/epub+zip", buffer: Buffer.from(payload, "base64") },
  ]);

  await expect(page.locator("#file-queue .queue-item")).toHaveCount(2);
  await page.locator("#inspect-btn").click();
  await expect(page.locator('#file-queue .queue-item[data-status="ready"]')).toHaveCount(2, {
    timeout: 30_000,
  });

  await expect(page.locator("#clean-all-btn")).toBeVisible();
  await expect(page.locator("#clean-all-btn")).toContainText("Clean 2 files");
  await page.locator("#clean-all-btn").click();

  await expect(page.locator('#file-queue .queue-item[data-status="cleaned"]')).toHaveCount(2, {
    timeout: 30_000,
  });
  await expect(page.locator("#batch-download")).toBeVisible();
  const href = await page.locator("#batch-download-link").getAttribute("href");
  expect(href).toContain("/api/download/batch?job_ids=");
  expect(pageErrors).toEqual([]);
});
