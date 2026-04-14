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

  const heading = page.locator("h2");
  await heading.waitFor();
  const headingText = (await heading.textContent()) || "";
  if (!headingText.includes("待审核")) {
    fail(`页面标题异常，期望包含“待审核”，实际为: ${headingText}`);
  }

  const metrics = await page.evaluate(() => {
    const queueBlocks = document.querySelectorAll("[id^='queue-']").length;
    const queueOpenBlocks = document.querySelectorAll("[id^='queue-'][open]").length;
    const queueToggleCount = document.querySelectorAll("[data-queue-toggle]").length;
    const viewerCount = document.querySelectorAll(".media-viewer").length;
    const feedbackNode = Boolean(document.querySelector("[data-review-feedback]"));
    const queueFaces = document.querySelectorAll(".queue-face").length;
    const reviewFocusLinks = document.querySelectorAll("[data-review-focus-index]").length;
    const actionPrev = document.querySelectorAll('[data-action="viewer-prev"]').length;
    const actionNext = document.querySelectorAll('[data-action="viewer-next"]').length;
    const resolveButtons = document.querySelectorAll('[data-action="review-resolve"]').length;
    const dismissButtons = document.querySelectorAll('[data-action="review-dismiss"]').length;
    const ignoreButtons = document.querySelectorAll('[data-action="review-ignore"]').length;
    const pageText = document.body?.innerText || "";
    const firstQueueText = document.querySelector("[id^='queue-']")?.textContent || "";
    const queueSummaries = Array.from(document.querySelectorAll("[id^='queue-']")).map((queue) => ({
      id: queue.id,
      open: Boolean(queue.hasAttribute("open")),
      text: queue.querySelector("[data-queue-toggle]")?.textContent?.trim() || ""
    }));
    return {
      queue_blocks: queueBlocks,
      queue_open_blocks: queueOpenBlocks,
      queue_toggle_count: queueToggleCount,
      viewer_count: viewerCount,
      feedback_node: feedbackNode,
      queue_faces: queueFaces,
      review_focus_links: reviewFocusLinks,
      viewer_actions: {
        prev: actionPrev,
        next: actionNext,
      },
      review_actions: {
        resolve: resolveButtons,
        dismiss: dismissButtons,
        ignore: ignoreButtons,
      },
      text_flags: {
        has_review_keyword: pageText.includes("review #"),
        has_queue_empty: pageText.includes("当前队列为空"),
        has_chinese: /[\u4e00-\u9fff]/.test(pageText),
      },
      first_queue_text: firstQueueText.trim().slice(0, 200),
      queue_summaries: queueSummaries,
      font_probe: {
        noto_cjk_sc: document.fonts.check("16px 'Noto Sans CJK SC'", "待审核"),
        noto_sans_sc: document.fonts.check("16px 'Noto Sans SC'", "待审核"),
        sans_chinese: document.fonts.check("16px sans-serif", "待审核"),
      },
    };
  });

  if (metrics.queue_blocks < 1) {
    fail(`未找到审核队列容器，queue_blocks=${metrics.queue_blocks}`, metrics);
  }
  if (metrics.queue_toggle_count !== metrics.queue_blocks) {
    fail("队列折叠开关数量异常", {
      queue_blocks: metrics.queue_blocks,
      queue_toggle_count: metrics.queue_toggle_count,
    });
  }
  if (metrics.viewer_count < 1) {
    fail(`未找到共享 viewer，viewer_count=${metrics.viewer_count}`, metrics);
  }
  if (!metrics.feedback_node) {
    fail("缺少 data-review-feedback 节点", metrics);
  }
  if (metrics.viewer_actions.prev < 1 || metrics.viewer_actions.next < 1) {
    fail("viewer 交互 hooks 缺失", metrics.viewer_actions);
  }
  if (!metrics.text_flags.has_chinese) {
    fail("页面文本未检测到中文字符", metrics.text_flags);
  }
  if (!metrics.font_probe.noto_cjk_sc && !metrics.font_probe.noto_sans_sc && !metrics.font_probe.sans_chinese) {
    fail("中文字体探测失败，疑似字体不可用", metrics.font_probe);
  }
  if (metrics.queue_open_blocks !== 0) {
    fail("页面初始状态应默认折叠所有审核队列", metrics.queue_summaries);
  }
  if (mode === "seeded") {
    const totalButtons = metrics.review_actions.resolve + metrics.review_actions.dismiss + metrics.review_actions.ignore;
    if (totalButtons < 1 && !metrics.text_flags.has_review_keyword) {
      fail("seeded 场景未检测到 review 条目或操作按钮", metrics);
    }
    if (metrics.queue_faces < 1) {
      fail("seeded 场景未检测到审核卡片预览缩略图", metrics);
    }
    if (metrics.review_focus_links < 1) {
      fail("seeded 场景未检测到 reviewer 与证据区的联动入口", metrics);
    }
  }

  const screenshotDir = path.dirname(screenshotPath);
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: screenshotPath, fullPage: true, animations: "disabled" });

  const firstQueueToggle = page.locator("[data-queue-toggle]").first();
  await firstQueueToggle.click();
  await page.waitForFunction(() => {
    const firstQueue = document.querySelector("[id^='queue-']");
    return Boolean(firstQueue && firstQueue.hasAttribute("open"));
  });
  const expandedMetrics = await page.evaluate(() => {
    const firstQueue = document.querySelector("[id^='queue-']");
    if (!firstQueue) {
      return {
        first_queue_open: false,
        first_queue_item_count: 0,
        first_queue_action_count: 0,
      };
    }
    return {
      first_queue_open: Boolean(firstQueue.hasAttribute("open")),
      first_queue_item_count: firstQueue.querySelectorAll(".queue-item").length,
      first_queue_action_count: firstQueue.querySelectorAll('[data-action^="review-"]').length,
    };
  });
  if (!expandedMetrics.first_queue_open) {
    fail("首个审核队列点击后未展开", expandedMetrics);
  }
  if (mode === "seeded" && expandedMetrics.first_queue_item_count < 1) {
    fail("seeded 场景展开首个审核队列后未检测到任务卡片", expandedMetrics);
  }
  if (mode === "seeded" && expandedMetrics.first_queue_action_count < 1) {
    fail("seeded 场景展开首个审核队列后未检测到审核动作按钮", expandedMetrics);
  }

  let stickyMetrics = {
    item_count: 0,
    item_ids: [],
    item_texts: [],
  };
  const secondQueueId = metrics.queue_summaries[1] ? metrics.queue_summaries[1].id : "";
  if (secondQueueId) {
    await page.evaluate((targetId) => {
      const target = document.getElementById(targetId);
      if (target) {
        target.scrollIntoView({ block: "start" });
      }
    }, secondQueueId);
    await page.waitForTimeout(150);
    stickyMetrics = await page.evaluate(() => {
      const items = Array.from(document.querySelectorAll("[data-review-queue-sticky-item]"));
      return {
        item_count: items.length,
        item_ids: items.map((item) => item.getAttribute("data-review-queue-sticky-item") || ""),
        item_texts: items.map((item) => item.textContent?.trim() || ""),
      };
    });
  }
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
    mode,
    viewport: "desktop",
    url,
    screenshot: screenshotPath,
    metrics,
    expanded_metrics: expandedMetrics,
    sticky_metrics: stickyMetrics,
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
