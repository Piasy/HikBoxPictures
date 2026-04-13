(function () {
  var page = document.body.getAttribute("data-page");
  if (!page) {
    return;
  }
  var link = document.querySelector('[data-page-link="' + page + '"]');
  if (link) {
    link.classList.add("active");
  }
})();
