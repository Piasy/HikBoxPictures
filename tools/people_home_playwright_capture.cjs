const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key || !key.startsWith("--") || value === undefined) {
      throw new Error(`无法解析参数: ${argv.join(" ")}`);
    }
    args[key.slice(2)] = value;
  }
  return args;
}

function fail(message, details) {
  const error = new Error(message);
  error.details = details;
  throw error;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const url = String(args.url || "");
  const mode = String(args.mode || "");
  const screenshotPath = String(args.screenshot || "");
  const reportPath = String(args.report || "");
  if (!url || !mode || !screenshotPath || !reportPath) {
    throw new Error("缺少必要参数: --url --mode --screenshot --report");
  }
  const viewportConfig = { width: 1440, height: 1200 };

  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: viewportConfig.width, height: viewportConfig.height },
    deviceScaleFactor: 1,
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(String(error));
  });
  page.on("requestfailed", (request) => {
    failedRequests.push(request.url());
  });

  await page.goto(url, { waitUntil: "networkidle" });
  await page.locator("main.page-content").waitFor();

  let metrics;
  if (mode === "empty") {
    await page.locator(".person-empty-state").waitFor();
    metrics = await page.evaluate(() => ({
      summary: document.querySelector(".summary")?.textContent?.trim() ?? "",
      emptyState: Boolean(document.querySelector(".person-empty-state")),
      personCards: document.querySelectorAll(".person-card").length,
      personGrid: Boolean(document.querySelector(".person-grid")),
      viewerCount: document.querySelectorAll(".media-viewer").length,
    }));
    if (!metrics.emptyState) {
      fail("空库首页没有渲染 person-empty-state", metrics);
    }
    if (metrics.personCards !== 0) {
      fail(`空库首页不应显示人物卡片，实际为 ${metrics.personCards}`, metrics);
    }
    if (metrics.viewerCount !== 0) {
      fail(`空库首页不应显示共享 viewer，实际为 ${metrics.viewerCount}`, metrics);
    }
  } else if (mode === "seeded") {
    await page.locator(".person-card").first().waitFor();
    metrics = await page.evaluate(() => ({
      names: Array.from(document.querySelectorAll(".person-card h3")).map((node) => node.textContent?.trim() ?? ""),
      emptyState: Boolean(document.querySelector(".person-empty-state")),
      personCards: document.querySelectorAll(".person-card").length,
      imageCount: document.querySelectorAll(".person-card-image").length,
      viewerCount: document.querySelectorAll(".media-viewer").length,
    }));
    const coverBox = await page.locator(".person-card-cover").first().boundingBox();
    if (metrics.emptyState) {
      fail("有数据的首页不应显示空状态", metrics);
    }
    if (metrics.personCards < 2) {
      fail(`期望至少 2 张人物卡片，实际为 ${metrics.personCards}`, metrics);
    }
    if (metrics.imageCount < 1) {
      fail("期望至少一张人物封面图", metrics);
    }
    if (metrics.viewerCount !== 0) {
      fail(`首页不应再显示共享 viewer，实际为 ${metrics.viewerCount}`, metrics);
    }
    if (!coverBox) {
      fail("无法读取人物封面卡片尺寸", metrics);
    }
    if (Number(coverBox.height) <= Number(coverBox.width)) {
      fail(
        `人物封面仍然不是竖向比例: width=${coverBox.width}, height=${coverBox.height}`,
        { ...metrics, first_cover_box: coverBox },
      );
    }
    metrics.first_cover_box = coverBox;
  } else {
    fail(`未知 mode: ${mode}`);
  }

  const screenshotDir = path.dirname(screenshotPath);
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: screenshotPath, fullPage: true, animations: "disabled" });
  await browser.close();

  if (consoleErrors.length > 0) {
    fail(`页面出现 console error: ${consoleErrors.join(" | ")}`);
  }
  if (pageErrors.length > 0) {
    fail(`页面出现 pageerror: ${pageErrors.join(" | ")}`);
  }
  if (failedRequests.length > 0) {
    fail(`页面存在请求失败: ${failedRequests.join(" | ")}`);
  }

  const report = {
    ...metrics,
    viewport: "desktop",
    screenshot: screenshotPath,
  };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2), "utf-8");
}

main().catch((error) => {
  console.error(error.stack || String(error));
  if (error.details !== undefined) {
    console.error(JSON.stringify(error.details, null, 2));
  }
  process.exit(1);
});
