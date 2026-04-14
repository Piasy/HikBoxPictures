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
  const screenshotPath = String(args.screenshot || "");
  const reportPath = String(args.report || "");
  if (!url || !screenshotPath || !reportPath) {
    throw new Error("缺少必要参数: --url --screenshot --report");
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

  const heading = page.locator("h2");
  await heading.waitFor();
  const headingText = (await heading.textContent()) || "";
  if (!headingText.includes("导出模板")) {
    fail(`页面标题异常，期望包含“导出模板”，实际为: ${headingText}`);
  }

  const initialMetrics = await page.evaluate(() => ({
    create_form_count: document.querySelectorAll('[data-export-form][data-export-mode="create"]').length,
    update_form_count: document.querySelectorAll('[data-export-form][data-export-mode="update"]').length,
    preview_tile_count: document.querySelectorAll(".export-preview-tile").length,
    focus_button_count: document.querySelectorAll("[data-export-focus-index]").length,
    viewer_count: document.querySelectorAll(".media-viewer").length,
    viewer_layer_count: document.querySelectorAll("[data-viewer-layer]").length,
    page_text: (document.body?.innerText || "").slice(0, 1000),
  }));

  if (initialMetrics.create_form_count < 1) {
    fail("未检测到创建模板表单", initialMetrics);
  }
  if (initialMetrics.viewer_count < 1) {
    fail("未检测到导出页共享 viewer", initialMetrics);
  }

  if (initialMetrics.focus_button_count > 0) {
    await page.locator("[data-export-focus-index]").first().click({ force: true });
    await page.waitForTimeout(250);
  }

  const metrics = await page.evaluate(() => {
    const viewer = document.querySelector(".media-viewer");
    const status = document.querySelector("[data-viewer-status]")?.textContent?.trim() || "";
    const label = document.querySelector("[data-viewer-current-label]")?.textContent?.trim() || "";
    const layerMetrics = ["crop", "context", "original"].map((layer) => {
      const node = document.querySelector(`[data-viewer-layer="${layer}"]`);
      if (!node) {
        return {
          layer,
          exists: false,
        };
      }
      const styles = getComputedStyle(node);
      return {
        layer,
        exists: true,
        src: node.getAttribute("src") || "",
        current_src: node.currentSrc || "",
        complete: Boolean(node.complete),
        natural_width: Number(node.naturalWidth || 0),
        natural_height: Number(node.naturalHeight || 0),
        client_width: Number(node.clientWidth || 0),
        client_height: Number(node.clientHeight || 0),
        object_fit: styles.objectFit,
        display: styles.display,
        class_name: node.className,
      };
    });
    const viewerLayerStyles = viewer
      ? {
          grid_template_columns: getComputedStyle(viewer.querySelector("[data-viewer-layers]")).gridTemplateColumns,
          viewer_width: viewer.clientWidth,
        }
      : null;
    return {
      status,
      label,
      viewer_layer_styles: viewerLayerStyles,
      layers: layerMetrics,
      feedback: document.querySelector("[data-export-feedback]")?.textContent?.trim() || "",
    };
  });

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
    viewport: "desktop",
    screenshot: screenshotPath,
    initial: initialMetrics,
    focused: metrics,
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
