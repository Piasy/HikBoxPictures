(function () {
  var page = document.body.getAttribute("data-page");
  if (page) {
    var link = document.querySelector('[data-page-link="' + page + '"]');
    if (link) {
      link.classList.add("active");
    }
  }

  function parseViewerItems(root) {
    var rows = root.querySelectorAll(".viewer-items li");
    return Array.prototype.map.call(rows, function (row) {
      var assignmentId = Number(row.getAttribute("data-assignment-id"));
      var observationId = Number(row.getAttribute("data-observation-id"));
      return {
        label: row.getAttribute("data-label") || "",
        cropUrl: row.getAttribute("data-crop-url") || "",
        contextUrl: row.getAttribute("data-context-url") || "",
        originalUrl: row.getAttribute("data-original-url") || "",
        assignmentId: Number.isFinite(assignmentId) && assignmentId > 0 ? assignmentId : null,
        observationId: Number.isFinite(observationId) && observationId > 0 ? observationId : null
      };
    });
  }

  function setLayerSrc(root, layer, url) {
    var target = root.querySelector('[data-viewer-layer="' + layer + '"]');
    if (!target) {
      return;
    }
    target.setAttribute("src", url || "");
    if (url) {
      target.classList.remove("is-empty");
    } else {
      target.classList.add("is-empty");
    }
  }

  function render(root) {
    var items = root.__viewerItems || [];
    var index = root.__viewerIndex || 0;
    var status = root.querySelector("[data-viewer-status]");
    var label = root.querySelector("[data-viewer-current-label]");

    if (!items.length) {
      setLayerSrc(root, "crop", "");
      setLayerSrc(root, "context", "");
      setLayerSrc(root, "original", "");
      if (status) {
        status.textContent = "无样例";
      }
      if (label) {
        label.textContent = "";
      }
      return;
    }

    var item = items[index];
    setLayerSrc(root, "crop", item.cropUrl);
    setLayerSrc(root, "context", item.contextUrl);
    setLayerSrc(root, "original", item.originalUrl);
    if (status) {
      status.textContent = (index + 1) + " / " + items.length;
    }
    if (label) {
      label.textContent = item.label || "";
    }
    root.dispatchEvent(
      new CustomEvent("hikbox:viewer-change", {
        bubbles: true,
        detail: {
          index: index,
          total: items.length,
          label: item.label || "",
          assignmentId: item.assignmentId,
          observationId: item.observationId
        }
      })
    );
  }

  function bindViewer(viewer) {
    viewer.__viewerItems = parseViewerItems(viewer);
    viewer.__viewerIndex = 0;
    render(viewer);

    var actions = viewer.querySelectorAll("[data-action]");
    actions.forEach(function (button) {
      button.addEventListener("click", function () {
        var action = button.getAttribute("data-action");
        if (action === "viewer-prev") {
          window.hikboxViewer.prev(viewer);
        } else if (action === "viewer-next") {
          window.hikboxViewer.next(viewer);
        }
      });
    });
  }

  function pickViewer(target) {
    if (target && target.classList && target.classList.contains("media-viewer")) {
      return target;
    }
    return document.querySelector(".media-viewer");
  }

  function currentViewerItem(target) {
    var viewer = pickViewer(target);
    if (!viewer || !viewer.__viewerItems || !viewer.__viewerItems.length) {
      return null;
    }
    return viewer.__viewerItems[viewer.__viewerIndex || 0] || null;
  }

  window.hikboxViewer = {
    prev: function (target) {
      var viewer = pickViewer(target);
      if (!viewer || !viewer.__viewerItems || !viewer.__viewerItems.length) {
        return;
      }
      var total = viewer.__viewerItems.length;
      viewer.__viewerIndex = (viewer.__viewerIndex - 1 + total) % total;
      render(viewer);
    },
    next: function (target) {
      var viewer = pickViewer(target);
      if (!viewer || !viewer.__viewerItems || !viewer.__viewerItems.length) {
        return;
      }
      var total = viewer.__viewerItems.length;
      viewer.__viewerIndex = (viewer.__viewerIndex + 1) % total;
      render(viewer);
    },
    select: function (target, index) {
      var viewer = pickViewer(target);
      var nextIndex = Number(index);
      if (!viewer || !viewer.__viewerItems || !viewer.__viewerItems.length || !Number.isFinite(nextIndex)) {
        return;
      }
      nextIndex = Math.max(0, Math.min(Math.floor(nextIndex), viewer.__viewerItems.length - 1));
      viewer.__viewerIndex = nextIndex;
      render(viewer);
    },
    currentItem: function (target) {
      return currentViewerItem(target);
    }
  };

  document.addEventListener("keydown", function (event) {
    if (event.key === "ArrowLeft") {
      window.hikboxViewer.prev();
    } else if (event.key === "ArrowRight") {
      window.hikboxViewer.next();
    }
  });

  document.querySelectorAll(".media-viewer").forEach(bindViewer);

  function setScanFeedback(message) {
    var node = document.querySelector("[data-scan-feedback]");
    if (!node) {
      return;
    }
    node.textContent = message || "";
  }

  function postScanAction(path) {
    setScanFeedback("执行中...");
    return fetch(path, { method: "POST" })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (payload) {
            return { ok: response.ok, status: response.status, payload: payload };
          });
      })
      .then(function (result) {
        if (!result.ok) {
          var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
          setScanFeedback("失败: " + detail);
          return;
        }
        var sessionId =
          result.payload && Object.prototype.hasOwnProperty.call(result.payload, "session_id")
            ? String(result.payload.session_id)
            : "-";
        var status = result.payload && result.payload.status ? String(result.payload.status) : "unknown";
        setScanFeedback("完成: session_id=" + sessionId + " status=" + status);
        window.location.reload();
      })
      .catch(function (error) {
        setScanFeedback("失败: " + (error && error.message ? error.message : "网络错误"));
      });
  }

  function bindScanActions() {
    var resume = document.querySelector('[data-action="scan-resume"]');
    if (resume) {
      resume.addEventListener("click", function () {
        postScanAction("/api/scan/start_or_resume");
      });
    }

    var abort = document.querySelector('[data-action="scan-abort"]');
    if (abort) {
      abort.addEventListener("click", function () {
        postScanAction("/api/scan/abort");
      });
    }

    var startNew = document.querySelector('[data-action="scan-start-new"]');
    if (startNew) {
      startNew.addEventListener("click", function () {
        postScanAction("/api/scan/start_new?abandon_resumable=true");
      });
    }
  }

  function parseJsonResponse(response) {
    return response
      .json()
      .catch(function () {
        return {};
      })
      .then(function (payload) {
        return { ok: response.ok, status: response.status, payload: payload };
      });
  }

  function setReviewFeedback(message) {
    var node = document.querySelector("[data-review-feedback]");
    if (!node) {
      return;
    }
    node.textContent = message || "";
  }

  function setPersonDetailFeedback(message) {
    var node = document.querySelector("[data-person-feedback]");
    if (!node) {
      return;
    }
    node.textContent = message || "";
  }

  function bindPersonDetailPreviewFocus() {
    var buttons = document.querySelectorAll("[data-person-focus-index]");
    if (!buttons.length) {
      return;
    }

    function syncActiveButton(index, shouldScroll) {
      var activeButton = null;
      buttons.forEach(function (button) {
        var isActive = button.getAttribute("data-person-focus-index") === String(index);
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
        if (isActive) {
          activeButton = button;
        }
      });
      if (shouldScroll && activeButton && typeof activeButton.scrollIntoView === "function") {
        activeButton.scrollIntoView({
          block: "nearest",
          inline: "nearest"
        });
      }
    }

    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var index = button.getAttribute("data-person-focus-index");
        if (!index) {
          return;
        }
        window.hikboxViewer.select(undefined, index);
        syncActiveButton(index, true);
        var label = button.getAttribute("data-person-focus-label");
        if (label) {
          setPersonDetailFeedback("已定位样本: " + label);
        }
      });
    });

    var detailPage = document.querySelector(".person-detail-page");
    if (detailPage) {
      detailPage.addEventListener("hikbox:viewer-change", function (event) {
        if (!event.detail || !Number.isFinite(event.detail.index)) {
          return;
        }
        syncActiveButton(event.detail.index, true);
        var excludeButton = detailPage.querySelector('[data-action="person-exclude-assignment"]');
        if (excludeButton) {
          excludeButton.disabled = !Number.isFinite(event.detail.assignmentId) || Number(event.detail.assignmentId) <= 0;
        }
      });
    }

    var viewer = document.querySelector(".person-detail-page .media-viewer");
    if (viewer && Number.isFinite(viewer.__viewerIndex)) {
      syncActiveButton(viewer.__viewerIndex, false);
    }
  }

  function bindPersonDetailExcludeAction() {
    var button = document.querySelector('[data-action="person-exclude-assignment"]');
    if (!button) {
      return;
    }
    button.addEventListener("click", function () {
      var personId = button.getAttribute("data-person-id");
      var currentItem = window.hikboxViewer.currentItem();
      var assignmentId = currentItem && Number(currentItem.assignmentId);
      if (!personId) {
        setPersonDetailFeedback("失败: person_id 缺失");
        return;
      }
      if (!Number.isFinite(assignmentId) || assignmentId <= 0) {
        setPersonDetailFeedback("失败: 当前样本没有可排除的归属记录");
        return;
      }
      button.disabled = true;
      setPersonDetailFeedback("排除中...");
      fetch("/api/people/" + personId + "/actions/exclude-assignment", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ assignment_id: assignmentId })
      })
        .then(parseJsonResponse)
        .then(function (result) {
          if (!result.ok) {
            button.disabled = false;
            var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
            setPersonDetailFeedback("失败: " + detail);
            return;
          }
          var remaining =
            result.payload && Object.prototype.hasOwnProperty.call(result.payload, "remaining_sample_count")
              ? String(result.payload.remaining_sample_count)
              : "-";
          var reviewId =
            result.payload && Object.prototype.hasOwnProperty.call(result.payload, "review_id")
              ? String(result.payload.review_id)
              : "-";
          setPersonDetailFeedback("完成: 已排除并重建，remaining=" + remaining + " review_id=" + reviewId);
          window.location.reload();
        })
        .catch(function (error) {
          button.disabled = false;
          setPersonDetailFeedback("失败: " + (error && error.message ? error.message : "网络错误"));
        });
    });
  }

  function bindReviewEvidenceFocus(root) {
    var scope = root || document;
    var buttons = scope.querySelectorAll("[data-review-focus-index]");
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var index = button.getAttribute("data-review-focus-index");
        if (!index) {
          return;
        }
        window.hikboxViewer.select(undefined, index);
        var label = button.getAttribute("data-review-focus-label");
        if (label) {
          setReviewFeedback("已定位证据: " + label);
        }
      });
    });
  }

  function bindReviewPreviewScrollers(root) {
    var scope = root || document;
    var buttons = scope.querySelectorAll("[data-preview-shift]");
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var shell = button.closest("[data-preview-shell]");
        if (!shell) {
          return;
        }
        var scroller = shell.querySelector("[data-preview-scroller]");
        if (!scroller) {
          return;
        }
        var shift = Number(button.getAttribute("data-preview-shift"));
        if (!Number.isFinite(shift) || shift === 0) {
          return;
        }
        var step = Math.max(scroller.clientWidth - 24, 96);
        scroller.scrollBy({
          left: shift * step,
          behavior: "smooth"
        });
      });
    });
  }

  function bindReviewStickyQueueStack(root) {
    if (window.__hikboxReviewStickyCleanup) {
      window.__hikboxReviewStickyCleanup();
      window.__hikboxReviewStickyCleanup = null;
    }

    var scope = root || document;
    var stack = scope.querySelector("[data-review-queue-sticky-stack]");
    if (!stack) {
      return;
    }
    var queueRoot = stack.closest(".review-queues");
    if (!queueRoot) {
      return;
    }
    var blocks = Array.prototype.slice.call(queueRoot.querySelectorAll(".queue-block"));
    if (!blocks.length) {
      return;
    }

    var stickyTop = 20;
    var stickyGap = 8;
    var frameId = 0;
    var destroyed = false;

    function toggleBlock(block) {
      if (!block) {
        return;
      }
      block.open = !block.open;
    }

    function buildStickyItem(block) {
      var summary = block.querySelector("[data-queue-toggle]");
      if (!summary) {
        return null;
      }
      var titleText = "";
      var countText = "";
      var titleNode = summary.querySelector(".queue-summary-title");
      var countNode = summary.querySelector(".queue-count");
      if (titleNode) {
        titleText = titleNode.textContent || "";
      }
      if (countNode) {
        countText = countNode.textContent || "";
      }

      var button = document.createElement("button");
      button.type = "button";
      button.className = "queue-summary queue-summary-clone";
      button.setAttribute("data-review-queue-sticky-item", block.id || "");
      button.setAttribute("data-queue-type", block.getAttribute("data-queue-type") || "");
      button.setAttribute("aria-expanded", block.open ? "true" : "false");
      if (block.id) {
        button.setAttribute("aria-controls", block.id);
      }

      var title = document.createElement("span");
      title.className = "queue-summary-title";
      title.textContent = titleText;

      var meta = document.createElement("span");
      meta.className = "queue-summary-meta";

      var count = document.createElement("span");
      count.className = "queue-count";
      count.textContent = countText;

      var icon = document.createElement("span");
      icon.className = "queue-toggle-icon";
      icon.setAttribute("aria-hidden", "true");

      meta.appendChild(count);
      meta.appendChild(icon);
      button.appendChild(title);
      button.appendChild(meta);

      button.addEventListener("click", function () {
        toggleBlock(block);
        scheduleSync();
      });
      return button;
    }

    function collectStickyBlocks() {
      var activeBlocks = [];
      var anchorTop = stickyTop;
      for (var index = 0; index < blocks.length; index += 1) {
        var block = blocks[index];
        var summary = block.querySelector("[data-queue-toggle]");
        if (!summary) {
          continue;
        }
        var rect = summary.getBoundingClientRect();
        var summaryHeight = Math.ceil(rect.height);
        if (rect.top <= anchorTop) {
          activeBlocks.push(block);
          anchorTop += summaryHeight + stickyGap;
          continue;
        }
        break;
      }
      return activeBlocks;
    }

    function syncStack() {
      if (destroyed) {
        return;
      }
      frameId = 0;
      stack.textContent = "";

      var activeBlocks = collectStickyBlocks();
      if (!activeBlocks.length) {
        return;
      }

      var fragment = document.createDocumentFragment();
      activeBlocks.forEach(function (block) {
        var item = buildStickyItem(block);
        if (item) {
          fragment.appendChild(item);
        }
      });
      stack.appendChild(fragment);
    }

    function scheduleSync() {
      if (destroyed || frameId) {
        return;
      }
      frameId = window.requestAnimationFrame(syncStack);
    }

    function cleanup() {
      if (destroyed) {
        return;
      }
      destroyed = true;
      if (frameId) {
        window.cancelAnimationFrame(frameId);
        frameId = 0;
      }
      blocks.forEach(function (block) {
        block.removeEventListener("toggle", scheduleSync);
      });
      window.removeEventListener("scroll", scheduleSync);
      window.removeEventListener("resize", scheduleSync);
    }

    blocks.forEach(function (block) {
      block.addEventListener("toggle", scheduleSync);
    });
    window.addEventListener("scroll", scheduleSync, { passive: true });
    window.addEventListener("resize", scheduleSync);
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(scheduleSync).catch(function () {});
    }
    window.__hikboxReviewStickyCleanup = cleanup;
    scheduleSync();
  }

  function collectOpenReviewQueueIds(root) {
    var scope = root || document;
    return Array.prototype.map.call(scope.querySelectorAll(".queue-block[open]"), function (block) {
      return block.id || "";
    }).filter(function (value) {
      return Boolean(value);
    });
  }

  function restoreOpenReviewQueueIds(root, queueIds) {
    var scope = root || document;
    (queueIds || []).forEach(function (queueId) {
      var block = scope.querySelector("#" + queueId);
      if (!block) {
        return;
      }
      if (block.querySelector(".queue-item")) {
        block.open = true;
      }
    });
  }

  function refreshReviewPagePartial() {
    var currentHero = document.querySelector(".review-hero");
    var currentQueues = document.querySelector(".review-queues");
    if (!currentHero || !currentQueues) {
      return Promise.resolve(false);
    }

    var openQueueIds = collectOpenReviewQueueIds(currentQueues);
    return fetch("/reviews", {
      headers: {
        "X-Requested-With": "fetch"
      }
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("待审核页面刷新失败");
        }
        return response.text();
      })
      .then(function (html) {
        var parser = new DOMParser();
        var nextDocument = parser.parseFromString(html, "text/html");
        var nextHero = nextDocument.querySelector(".review-hero");
        var nextQueues = nextDocument.querySelector(".review-queues");
        if (!nextHero || !nextQueues) {
          throw new Error("待审核页面缺少可替换节点");
        }

        currentHero.replaceWith(nextHero);
        currentQueues.replaceWith(nextQueues);
        restoreOpenReviewQueueIds(nextQueues, openQueueIds);
        bindReviewEvidenceFocus(nextQueues);
        bindReviewPreviewScrollers(nextQueues);
        bindReviewStickyQueueStack(nextQueues);
        bindReviewActions(nextQueues);
        return true;
      });
  }

  function bindReviewActions(root) {
    var scope = root || document;
    var buttons = scope.querySelectorAll("[data-action^='review-']");
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var action = button.getAttribute("data-action");
        var reviewId = button.getAttribute("data-review-id");
        var reviewIdsAttr = button.getAttribute("data-review-ids") || "";
        var actionRoot = button.closest(".review-action-group") || button.closest(".queue-item-actions");
        if (!reviewId) {
          setReviewFeedback("失败: review_id 缺失");
          return;
        }
        var reviewIds = reviewIdsAttr
          .split(",")
          .map(function (value) {
            return Number(value);
          })
          .filter(function (value) {
            return Number.isFinite(value) && value > 0;
          });
        if (!reviewIds.length) {
          reviewIds = [Number(reviewId)];
        }
        var path = "";
        var requestPayload = null;
        if (action === "review-resolve") {
          path = "/api/reviews/" + reviewId + "/actions/resolve";
        } else if (action === "review-dismiss") {
          path = "/api/reviews/" + reviewId + "/actions/dismiss";
        } else if (action === "review-ignore") {
          path = "/api/reviews/" + reviewId + "/actions/ignore";
        } else if (action === "review-create-person") {
          var createNameInput = actionRoot ? actionRoot.querySelector("[data-review-create-name]") : null;
          var displayName = createNameInput ? String(createNameInput.value || "").trim() : "";
          if (!displayName) {
            setReviewFeedback("失败: 新人物名称不能为空");
            return;
          }
          path = "/api/reviews/" + reviewId + "/actions/create-person";
          requestPayload = {
            review_ids: reviewIds,
            display_name: displayName
          };
        } else if (action === "review-assign-person") {
          var assignSelect = actionRoot ? actionRoot.querySelector("[data-review-assign-person-id]") : null;
          var personId = assignSelect ? Number(assignSelect.value) : NaN;
          if (!Number.isFinite(personId) || personId <= 0) {
            setReviewFeedback("失败: 请选择目标人物");
            return;
          }
          path = "/api/reviews/" + reviewId + "/actions/assign-person";
          requestPayload = {
            review_ids: reviewIds,
            person_id: personId
          };
        } else {
          return;
        }
        setReviewFeedback("执行中..." + (reviewIds.length > 1 ? "（批量 " + reviewIds.length + " 条）" : ""));
        var requestInit = { method: "POST" };
        if (requestPayload) {
          requestInit.headers = { "Content-Type": "application/json" };
          requestInit.body = JSON.stringify(requestPayload);
        } else if (reviewIds.length) {
          requestInit.headers = { "Content-Type": "application/json" };
          requestInit.body = JSON.stringify({ review_ids: reviewIds });
        }
        fetch(path, requestInit)
          .then(parseJsonResponse)
          .then(function (result) {
            if (!result.ok) {
              var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
              setReviewFeedback("失败: " + detail);
              return;
            }
            var status = result.payload && result.payload.status ? String(result.payload.status) : "unknown";
            var updatedCount =
              result.payload && Object.prototype.hasOwnProperty.call(result.payload, "updated_count")
                ? String(result.payload.updated_count)
                : String(reviewIds.length || 1);
            var personLabel = "";
            if (result.payload && result.payload.display_name) {
              personLabel = " person=" + String(result.payload.display_name);
            } else if (result.payload && Object.prototype.hasOwnProperty.call(result.payload, "person_id")) {
              personLabel = " person_id=" + String(result.payload.person_id);
            }
            setReviewFeedback(
              "完成: review_id=" + reviewId + " status=" + status + " updated=" + updatedCount + personLabel
            );
            refreshReviewPagePartial()
              .then(function (refreshed) {
                if (!refreshed) {
                  window.location.reload();
                  return;
                }
              })
              .catch(function () {
                window.location.reload();
              });
          })
          .catch(function (error) {
            setReviewFeedback("失败: " + (error && error.message ? error.message : "网络错误"));
          });
      });
    });
  }

  function setExportFeedback(message) {
    var node = document.querySelector("[data-export-feedback]");
    if (!node) {
      return;
    }
    node.textContent = message || "";
  }

  function collectExportPayload(form) {
    return {
      name: String((form.querySelector('[name="name"]') || {}).value || "").trim(),
      output_root: String((form.querySelector('[name="output_root"]') || {}).value || "").trim(),
      start_datetime: String((form.querySelector('[name="start_datetime"]') || {}).value || "").trim() || null,
      end_datetime: String((form.querySelector('[name="end_datetime"]') || {}).value || "").trim() || null,
      include_group: Boolean(form.querySelector('[name="include_group"]') && form.querySelector('[name="include_group"]').checked),
      export_live_mov: Boolean(
        form.querySelector('[name="export_live_mov"]') && form.querySelector('[name="export_live_mov"]').checked
      ),
      enabled: Boolean(form.querySelector('[name="enabled"]') && form.querySelector('[name="enabled"]').checked),
      person_ids: Array.prototype.map
        .call(form.querySelectorAll('input[name="person_ids"]:checked'), function (input) {
          return Number(input.value);
        })
        .filter(function (value) {
          return Number.isFinite(value) && value > 0;
        })
    };
  }

  function bindExportPreviewFocus() {
    var buttons = document.querySelectorAll("[data-export-focus-index]");
    if (!buttons.length) {
      return;
    }

    function syncActiveButton(index, shouldScroll) {
      var activeButton = null;
      buttons.forEach(function (button) {
        var isActive = button.getAttribute("data-export-focus-index") === String(index);
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
        if (isActive) {
          activeButton = button;
        }
      });
      if (shouldScroll && activeButton && typeof activeButton.scrollIntoView === "function") {
        activeButton.scrollIntoView({
          block: "nearest",
          inline: "nearest"
        });
      }
    }

    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var index = button.getAttribute("data-export-focus-index");
        if (!index) {
          return;
        }
        window.hikboxViewer.select(undefined, index);
        syncActiveButton(index, true);
        var label = button.getAttribute("data-export-focus-label");
        if (label) {
          setExportFeedback("已定位样例: " + label);
        }
      });
    });

    var exportPage = document.querySelector(".export-page");
    if (exportPage) {
      exportPage.addEventListener("hikbox:viewer-change", function (event) {
        if (!event.detail || !Number.isFinite(event.detail.index)) {
          return;
        }
        syncActiveButton(event.detail.index, true);
      });
    }

    var viewer = document.querySelector(".export-page .media-viewer");
    if (viewer && Number.isFinite(viewer.__viewerIndex)) {
      syncActiveButton(viewer.__viewerIndex, false);
    }
  }

  function renderExportRuns(runs) {
    var list = document.querySelector("[data-export-runs-list]");
    if (!list) {
      return;
    }
    list.innerHTML = "";
    if (!runs || !runs.length) {
      var empty = document.createElement("li");
      empty.textContent = "无运行历史";
      list.appendChild(empty);
      return;
    }
    runs.forEach(function (run) {
      var item = document.createElement("li");
      item.textContent =
        "#" + String(run.id) +
        " status=" + String(run.status || "unknown") +
        " started_at=" + String(run.started_at || "-") +
        " exported=" + String(run.exported_count || 0) +
        " failed=" + String(run.failed_count || 0);
      list.appendChild(item);
    });
  }

  function bindExportForms() {
    var forms = document.querySelectorAll("[data-export-form]");
    forms.forEach(function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var mode = form.getAttribute("data-export-mode");
        var templateId = form.getAttribute("data-template-id");
        var payload = collectExportPayload(form);
        var path = "/api/export/templates";
        var method = "POST";
        if (mode === "update") {
          if (!templateId) {
            setExportFeedback("失败: template_id 缺失");
            return;
          }
          path = "/api/export/templates/" + templateId;
          method = "PUT";
        }
        setExportFeedback(mode === "update" ? "保存中..." : "创建中...");
        fetch(path, {
          method: method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        })
          .then(parseJsonResponse)
          .then(function (result) {
            if (!result.ok) {
              var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
              setExportFeedback("失败: " + detail);
              return;
            }
            var savedTemplateId =
              result.payload && Object.prototype.hasOwnProperty.call(result.payload, "id")
                ? String(result.payload.id)
                : templateId || "-";
            setExportFeedback(
              (mode === "update" ? "已保存模板" : "已创建模板") + ": template_id=" + savedTemplateId
            );
            window.location.reload();
          })
          .catch(function (error) {
            setExportFeedback("失败: " + (error && error.message ? error.message : "网络错误"));
          });
      });
    });
  }

  function bindExportActions() {
    var runButtons = document.querySelectorAll('[data-action="export-run"]');
    runButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        var templateId = button.getAttribute("data-template-id");
        if (!templateId) {
          setExportFeedback("失败: template_id 缺失");
          return;
        }
        setExportFeedback("执行中...");
        fetch("/api/export/templates/" + templateId + "/actions/run", { method: "POST" })
          .then(parseJsonResponse)
          .then(function (result) {
            if (!result.ok) {
              var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
              setExportFeedback("失败: " + detail);
              return;
            }
            var runId =
              result.payload && Object.prototype.hasOwnProperty.call(result.payload, "run_id")
                ? String(result.payload.run_id)
                : "-";
            setExportFeedback("完成: template_id=" + templateId + " run_id=" + runId);
            window.location.reload();
          })
          .catch(function (error) {
            setExportFeedback("失败: " + (error && error.message ? error.message : "网络错误"));
          });
      });
    });

    var historyButtons = document.querySelectorAll('[data-action="export-show-runs"]');
    historyButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        var templateId = button.getAttribute("data-template-id");
        if (!templateId) {
          setExportFeedback("失败: template_id 缺失");
          return;
        }
        setExportFeedback("加载中...");
        fetch("/api/export/templates/" + templateId + "/runs")
          .then(parseJsonResponse)
          .then(function (result) {
            if (!result.ok) {
              var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
              setExportFeedback("失败: " + detail);
              return;
            }
            renderExportRuns(result.payload);
            setExportFeedback("完成: template_id=" + templateId + " runs=" + String(result.payload.length || 0));
          })
          .catch(function (error) {
            setExportFeedback("失败: " + (error && error.message ? error.message : "网络错误"));
          });
      });
    });

    var deleteButtons = document.querySelectorAll('[data-action="export-delete-template"]');
    deleteButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        var templateId = button.getAttribute("data-template-id");
        if (!templateId) {
          setExportFeedback("失败: template_id 缺失");
          return;
        }
        if (!window.confirm("确认删除模板 #" + templateId + " 吗？")) {
          return;
        }
        setExportFeedback("删除中...");
        fetch("/api/export/templates/" + templateId, { method: "DELETE" })
          .then(parseJsonResponse)
          .then(function (result) {
            if (!result.ok) {
              var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
              setExportFeedback("失败: " + detail);
              return;
            }
            setExportFeedback("已删除模板: template_id=" + templateId);
            window.location.reload();
          })
          .catch(function (error) {
            setExportFeedback("失败: " + (error && error.message ? error.message : "网络错误"));
          });
      });
    });
  }

  if (page === "sources") {
    bindScanActions();
  } else if (page === "people") {
    bindPersonDetailPreviewFocus();
    bindPersonDetailExcludeAction();
  } else if (page === "reviews") {
    var reviewQueues = document.querySelector(".review-queues");
    bindReviewEvidenceFocus(reviewQueues || document);
    bindReviewPreviewScrollers(reviewQueues || document);
    bindReviewStickyQueueStack(reviewQueues || document);
    bindReviewActions(reviewQueues || document);
  } else if (page === "exports") {
    bindExportPreviewFocus();
    bindExportForms();
    bindExportActions();
  }
})();
