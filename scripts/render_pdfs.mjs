import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

function parseArgs(argv) {
  const args = { targets: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--target") {
      args.targets.push(argv[index + 1]);
      index += 1;
      continue;
    }
    if (value === "--date") {
      args.date = argv[index + 1];
      index += 1;
    }
  }
  return args;
}

async function fileExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function collectTargets(rootDir, archiveDate) {
  const explicit = [];
  if (archiveDate) {
    const snapshotMeta = path.join(rootDir, "snapshots", archiveDate, "meta.json");
    if (await fileExists(snapshotMeta)) {
      const meta = JSON.parse(await fs.readFile(snapshotMeta, "utf8"));
      explicit.push(path.join(rootDir, "snapshots", archiveDate, "offline", "index.html"));
      if (meta.day >= 1 && meta.day <= 30) {
        explicit.push(
          path.join(rootDir, "days", String(meta.day).padStart(2, "0"), "offline", "index.html"),
        );
      }
    }
  }

  return [...new Set(explicit)];
}

async function renderPdf(browser, htmlPath) {
  const page = await browser.newPage({
    viewport: { width: 1400, height: 2000 },
    deviceScaleFactor: 1,
  });

  try {
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
    await page.emulateMedia({ media: "screen" });
    await page.pdf({
      path: path.join(path.dirname(htmlPath), "page.pdf"),
      format: "A4",
      printBackground: true,
      margin: {
        top: "12mm",
        right: "10mm",
        bottom: "12mm",
        left: "10mm",
      },
    });
  } finally {
    await page.close();
  }
}

async function main() {
  const scriptPath = fileURLToPath(import.meta.url);
  const rootDir = path.resolve(path.dirname(scriptPath), "..");
  const args = parseArgs(process.argv.slice(2));
  const targets = [
    ...args.targets.map((item) => path.resolve(item)),
    ...(await collectTargets(rootDir, args.date)),
  ];
  const dedupedTargets = [...new Set(targets)];

  if (dedupedTargets.length === 0) {
    throw new Error("No offline HTML targets found for PDF rendering.");
  }

  const browser = await chromium.launch({ headless: true });
  try {
    for (const htmlPath of dedupedTargets) {
      await renderPdf(browser, htmlPath);
      console.log(`Rendered PDF for ${htmlPath}`);
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
