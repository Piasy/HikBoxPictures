const fs = require("node:fs");
const path = require("node:path");
const { chromium, webkit } = require("playwright");

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
  const screenshotPath = String(args.screenshot || "");
  const reportPath = String(args.report || "");
  const runId = args["run-id"] === undefined ? null : Number(args["run-id"]);

  if (!url || !screenshotPath || !reportPath) {
    throw new Error("缺少必要参数: --url --screenshot --report");
  }
  if (args["run-id"] !== undefined && !Number.isInteger(runId)) {
    throw new Error(`run_id 非法: ${args["run-id"]}`);
  }

  const viewport = { width: 1440, height: 1024 };
  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];

  let browser = null;
  let browserEngine = "webkit";
  let launchFallbackReason = null;
  try {
    browser = await webkit.launch({ headless: true });
  } catch (error) {
    if (String(process.env.PLAYWRIGHT_ALLOW_CHROMIUM_FALLBACK || "1") !== "1") {
      throw error;
    }
    launchFallbackReason = String(error && error.message ? error.message : error);
    browser = await chromium.launch({ headless: true });
    browserEngine = "chromium-fallback";
  }
  const page = await browser.newPage({
    viewport,
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

  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.locator("section.identity-tuning-page").waitFor();
  await page.locator("script#identity-tuning-data").waitFor({ state: "attached" });
  await page.locator(".identity-run-clusters, .summary").first().waitFor();

  const metrics = await page.evaluate(() => {
    const dataNode = document.querySelector("script#identity-tuning-data");
    const rawData = dataNode?.textContent?.trim() || "";

    let payload = null;
    let payloadError = "";
    try {
      payload = rawData ? JSON.parse(rawData) : null;
    } catch (error) {
      payloadError = String(error);
    }

    const clusterNodes = document.querySelectorAll(".identity-run-clusters .identity-list-item").length;
    const headerText = document.querySelector(".identity-tuning-hero h2")?.textContent?.trim() || "";
    const reviewRunIdText = document.querySelector(".identity-tuning-section .identity-kv-grid dd")?.textContent?.trim() || "";

    return {
      has_payload_node: Boolean(dataNode),
      payload_parse_error: payloadError,
      payload_review_run_id: payload && payload.review_run ? Number(payload.review_run.id || 0) : null,
      payload_cluster_count: payload && Array.isArray(payload.clusters) ? payload.clusters.length : 0,
      dom_cluster_count: clusterNodes,
      header_text: headerText,
      review_run_id_text: reviewRunIdText,
    };
  });

  if (!metrics.has_payload_node) {
    fail("页面缺少 identity-tuning-data 节点", metrics);
  }
  if (metrics.payload_parse_error) {
    fail(`identity-tuning-data 解析失败: ${metrics.payload_parse_error}`, metrics);
  }
  if (!metrics.header_text.includes("Identity Run 证据页")) {
    fail(`页面标题异常: ${metrics.header_text}`, metrics);
  }
  if (metrics.payload_cluster_count !== metrics.dom_cluster_count) {
    fail("页面 cluster 数量与 payload 不一致", metrics);
  }

  const screenshotDir = path.dirname(screenshotPath);
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: screenshotPath, fullPage: true, animations: "disabled" });

  const finalUrl = page.url();
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
    browser_engine: browserEngine,
    launch_fallback_reason: launchFallbackReason,
    viewport: "desktop-safari-like",
    requested_url: url,
    final_url: finalUrl,
    run_id: Number.isInteger(runId)
      ? runId
      : (Number.isFinite(metrics.payload_review_run_id) ? Number(metrics.payload_review_run_id) : null),
    screenshot: screenshotPath,
    metrics,
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
