(function () {
  function stripGeneratedWhitespace(text) {
    return (text || "")
      .replace(/[\u00A0\u1680\u180E\u2000-\u200A\u202F\u205F\u3000]/g, "")
      .replace(/\r\n?/g, "\n");
  }

  function copyText(text, button) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () {
        return true;
      });
    }

    var textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.top = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    var ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return Promise.resolve(ok);
  }

  function getOriginalCode(block) {
    var script = block.querySelector(".code-source");

    if (!script) {
      return null;
    }

    try {
      return JSON.parse(script.textContent);
    } catch (error) {
      return null;
    }
  }

  function setupCodeBlock(block) {
    var button = block.querySelector(".copy-code");
    var pre = block.querySelector("pre");

    if (!button || !pre || button.dataset.copyReady === "true") {
      return;
    }

    button.dataset.copyReady = "true";
    button.addEventListener("click", function () {
      var code = pre.querySelector("code");
      var originalText = getOriginalCode(block);
      var text =
        originalText !== null
          ? originalText
          : stripGeneratedWhitespace(code ? code.textContent : pre.textContent);

      copyText(text, button).then(function (ok) {
        button.textContent = ok ? "已复制" : "复制失败";
        setTimeout(function () {
          button.textContent = "复制";
        }, 1500);
      });
    });
  }

  function wrapPlainCodeBlock(pre) {
    if (pre.closest(".code-block")) {
      return;
    }

    var wrapper = document.createElement("div");
    wrapper.className = "code-block";
    var button = document.createElement("button");
    button.className = "copy-code";
    button.type = "button";
    button.textContent = "复制";

    pre.parentNode.insertBefore(wrapper, pre);
    wrapper.appendChild(button);
    wrapper.appendChild(pre);
    setupCodeBlock(wrapper);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document
      .querySelectorAll(".article-content .code-block")
      .forEach(setupCodeBlock);

    document.querySelectorAll(".article-content pre").forEach(function (pre) {
      if (pre.closest(".code-block")) {
        return;
      }

      wrapPlainCodeBlock(pre);
    });

    var lightboxImages = Array.prototype.slice.call(
      document.querySelectorAll(".article-content img"),
    );
    var currentImageIndex = 0;
    var currentScale = 1;
    var lightbox = document.createElement("div");
    lightbox.className = "image-lightbox";
    lightbox.innerHTML =
      '<div class="image-lightbox-stage">' +
      '<img alt="">' +
      "</div>" +
      '<div class="image-lightbox-toolbar" aria-label="图片预览工具栏">' +
      '<button class="lightbox-prev" type="button" title="上一张" aria-label="上一张">‹</button>' +
      '<button class="lightbox-next" type="button" title="下一张" aria-label="下一张">›</button>' +
      '<span class="lightbox-counter" aria-live="polite"></span>' +
      '<button class="lightbox-zoom-out" type="button" title="缩小" aria-label="缩小">−</button>' +
      '<button class="lightbox-zoom-in" type="button" title="放大" aria-label="放大">＋</button>' +
      '<button class="lightbox-reset" type="button" title="适应窗口" aria-label="适应窗口">□</button>' +
      '<button class="lightbox-fullscreen" type="button" title="切换全屏" aria-label="切换全屏">⛶</button>' +
      '<button class="image-lightbox-close" type="button" title="关闭" aria-label="关闭图片预览">×</button>' +
      "</div>";
    document.body.appendChild(lightbox);

    var lightboxImage = lightbox.querySelector("img");
    var stage = lightbox.querySelector(".image-lightbox-stage");
    var closeButton = lightbox.querySelector(".image-lightbox-close");
    var prevButton = lightbox.querySelector(".lightbox-prev");
    var nextButton = lightbox.querySelector(".lightbox-next");
    var zoomOutButton = lightbox.querySelector(".lightbox-zoom-out");
    var zoomInButton = lightbox.querySelector(".lightbox-zoom-in");
    var resetButton = lightbox.querySelector(".lightbox-reset");
    var fullscreenButton = lightbox.querySelector(".lightbox-fullscreen");
    var counter = lightbox.querySelector(".lightbox-counter");

    function updateLightboxImage() {
      var sourceImage = lightboxImages[currentImageIndex];
      if (!sourceImage) {
        return;
      }

      lightboxImage.src = sourceImage.currentSrc || sourceImage.src;
      lightboxImage.alt = sourceImage.alt || "";
      currentScale = 1;
      lightboxImage.style.transform = "scale(1)";
      counter.textContent = lightboxImages.length
        ? currentImageIndex + 1 + " / " + lightboxImages.length
        : "";
      prevButton.disabled = lightboxImages.length <= 1;
      nextButton.disabled = lightboxImages.length <= 1;
    }

    function closeLightbox() {
      lightbox.classList.remove("is-open");
      lightboxImage.removeAttribute("src");
      lightboxImage.style.transform = "scale(1)";
      document.body.classList.remove("lightbox-open");
      if (document.fullscreenElement === lightbox && document.exitFullscreen) {
        document.exitFullscreen();
      }
    }

    function openImage(index) {
      currentImageIndex = index;
      updateLightboxImage();
      lightbox.classList.add("is-open");
      document.body.classList.add("lightbox-open");
    }

    function showRelativeImage(offset) {
      if (!lightboxImages.length) {
        return;
      }
      currentImageIndex =
        (currentImageIndex + offset + lightboxImages.length) %
        lightboxImages.length;
      updateLightboxImage();
    }

    function setScale(scale) {
      currentScale = Math.min(3, Math.max(0.5, scale));
      lightboxImage.style.transform = "scale(" + currentScale + ")";
    }

    function toggleFullscreen() {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else if (lightbox.requestFullscreen) {
        lightbox.requestFullscreen();
      }
    }

    lightboxImages.forEach(function (image, index) {
      image.classList.add("zoomable-image");
      image.setAttribute("tabindex", "0");
      image.setAttribute("role", "button");
      image.setAttribute("aria-label", "放大查看图片");
      image.addEventListener("click", function () {
        openImage(index);
      });
      image.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openImage(index);
        }
      });
    });

    lightbox
      .querySelector(".image-lightbox-toolbar")
      .addEventListener("click", function (event) {
        event.stopPropagation();
      });
    stage.addEventListener("click", function (event) {
      if (event.target === stage) {
        closeLightbox();
      }
    });
    closeButton.addEventListener("click", closeLightbox);
    lightboxImage.addEventListener("click", closeLightbox);
    prevButton.addEventListener("click", function () {
      showRelativeImage(-1);
    });
    nextButton.addEventListener("click", function () {
      showRelativeImage(1);
    });
    zoomOutButton.addEventListener("click", function () {
      setScale(currentScale - 0.25);
    });
    zoomInButton.addEventListener("click", function () {
      setScale(currentScale + 0.25);
    });
    resetButton.addEventListener("click", function () {
      setScale(1);
    });
    fullscreenButton.addEventListener("click", toggleFullscreen);
    lightbox.addEventListener("click", function (event) {
      if (event.target === lightbox) {
        closeLightbox();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (!lightbox.classList.contains("is-open")) {
        return;
      }
      if (event.key === "Escape") {
        closeLightbox();
      }
      if (event.key === "ArrowLeft") {
        showRelativeImage(-1);
      }
      if (event.key === "ArrowRight") {
        showRelativeImage(1);
      }
    });
  });
})();
