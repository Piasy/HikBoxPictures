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
})();
