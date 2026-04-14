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
      return {
        label: row.getAttribute("data-label") || "",
        cropUrl: row.getAttribute("data-crop-url") || "",
        contextUrl: row.getAttribute("data-context-url") || "",
        originalUrl: row.getAttribute("data-original-url") || ""
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

    if (!items.length) {
      setLayerSrc(root, "crop", "");
      setLayerSrc(root, "context", "");
      setLayerSrc(root, "original", "");
      if (status) {
        status.textContent = "无样例";
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
  }

  function bindViewer(viewer) {
    viewer.__viewerItems = parseViewerItems(viewer);
    viewer.__viewerIndex = 0;
    viewer.__bboxVisible = false;
    render(viewer);

    var actions = viewer.querySelectorAll("[data-action]");
    actions.forEach(function (button) {
      button.addEventListener("click", function () {
        var action = button.getAttribute("data-action");
        if (action === "viewer-prev") {
          window.hikboxViewer.prev(viewer);
        } else if (action === "viewer-next") {
          window.hikboxViewer.next(viewer);
        } else if (action === "viewer-toggle-bbox") {
          window.hikboxViewer.toggleBbox(viewer);
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
    toggleBbox: function (target) {
      var viewer = pickViewer(target);
      if (!viewer) {
        return;
      }
      viewer.__bboxVisible = !viewer.__bboxVisible;
      var bbox = viewer.querySelector("[data-viewer-bbox]");
      if (bbox) {
        bbox.hidden = !viewer.__bboxVisible;
      }
    }
  };

  document.addEventListener("keydown", function (event) {
    if (event.key === "ArrowLeft") {
      window.hikboxViewer.prev();
    } else if (event.key === "ArrowRight") {
      window.hikboxViewer.next();
    } else if (event.key === "b" || event.key === "B") {
      window.hikboxViewer.toggleBbox();
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

  function bindReviewActions() {
    var buttons = document.querySelectorAll("[data-action^='review-']");
    buttons.forEach(function (button) {
      button.addEventListener("click", function () {
        var action = button.getAttribute("data-action");
        var reviewId = button.getAttribute("data-review-id");
        if (!reviewId) {
          setReviewFeedback("失败: review_id 缺失");
          return;
        }
        var path = "";
        if (action === "review-resolve") {
          path = "/api/reviews/" + reviewId + "/actions/resolve";
        } else if (action === "review-dismiss") {
          path = "/api/reviews/" + reviewId + "/actions/dismiss";
        } else if (action === "review-ignore") {
          path = "/api/reviews/" + reviewId + "/actions/ignore";
        } else {
          return;
        }
        setReviewFeedback("执行中...");
        fetch(path, { method: "POST" })
          .then(parseJsonResponse)
          .then(function (result) {
            if (!result.ok) {
              var detail = result.payload && result.payload.detail ? String(result.payload.detail) : "请求失败";
              setReviewFeedback("失败: " + detail);
              return;
            }
            var status = result.payload && result.payload.status ? String(result.payload.status) : "unknown";
            setReviewFeedback("完成: review_id=" + reviewId + " status=" + status);
            window.location.reload();
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
        " exported=" + String(run.exported_count || 0) +
        " failed=" + String(run.failed_count || 0);
      list.appendChild(item);
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
  }

  if (page === "sources") {
    bindScanActions();
  } else if (page === "reviews") {
    bindReviewActions();
  } else if (page === "exports") {
    bindExportActions();
  }
})();
