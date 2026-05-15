(function () {
  var article = document.querySelector(".article");
  var content = document.querySelector(".article-content");

  if (!article || !content) {
    return;
  }

  var storageKey = "articleToolbarPrefs";
  var prefs = {
    fontStep: 0,
    wide: false
  };

  try {
    prefs = Object.assign(prefs, JSON.parse(localStorage.getItem(storageKey) || "{}"));
  } catch (error) {
    prefs = {
      fontStep: 0,
      wide: false
    };
  }

  var style = document.createElement("style");
  style.textContent = [
    ".article-toolbar{position:fixed;right:clamp(14px,3vw,42px);bottom:clamp(76px,12vh,132px);z-index:850;display:grid;gap:10px;opacity:0;pointer-events:none;transform:translateY(14px);transition:opacity .18s ease,transform .18s ease}",
    ".article-toolbar.is-visible{opacity:1;pointer-events:auto;transform:translateY(0)}",
    ".article-toolbar__panel{display:grid;gap:10px;max-height:0;overflow:hidden;opacity:0;transform:translateY(10px);transition:max-height .2s ease,opacity .18s ease,transform .18s ease}",
    ".article-toolbar.is-open .article-toolbar__panel{max-height:360px;opacity:1;transform:translateY(0)}",
    ".article-toolbar__button{display:grid;place-items:center;width:52px;height:52px;border:1px solid rgba(17,24,39,.08);border-radius:10px;background:rgba(31,31,35,.86);color:rgba(255,255,255,.74);box-shadow:0 14px 34px rgba(15,23,42,.18);backdrop-filter:blur(12px);cursor:pointer;transition:background .16s ease,color .16s ease,transform .16s ease}",
    ".article-toolbar__button:hover,.article-toolbar__button:focus-visible,.article-toolbar__button.is-active{background:rgba(104,104,110,.94);color:#fff;transform:translateY(-1px);outline:0}",
    ".article-toolbar__button svg{width:27px;height:27px;stroke:currentColor;stroke-width:2.45;stroke-linecap:round;stroke-linejoin:round;fill:none}",
    ".article-toolbar__button--small svg{width:24px;height:24px}",
    ".article-toolbar__main{display:grid;gap:10px}",
    ".article-toolbar__progress{position:absolute;right:64px;top:10px;width:3px;height:calc(100% - 20px);border-radius:99px;background:rgba(17,24,39,.1);overflow:hidden}",
    ".article-toolbar__progress span{position:absolute;left:0;bottom:0;width:100%;height:0;background:var(--accent,#d43f2f);transition:height .12s linear}",
    "body.article-toolbar-wide .article-layout{grid-template-columns:minmax(0,1120px);justify-content:center}",
    "body.article-toolbar-wide .article-sidebar{display:none}",
    "@media(max-width:900px){.article-toolbar{right:12px;bottom:72px}.article-toolbar__button{width:48px;height:48px}.article-toolbar__progress{display:none}}",
    "@media(max-width:640px){.article-toolbar{bottom:64px}.article-toolbar__button{width:46px;height:46px;border-radius:9px}.article-toolbar__button svg{width:25px;height:25px}}",
    "@media(prefers-reduced-motion:reduce){.article-toolbar,.article-toolbar__panel,.article-toolbar__button,.article-toolbar__progress span{transition:none}}"
  ].join("");
  document.head.appendChild(style);

  var icon = {
    up: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 15 6-6 6 6"/><path d="M12 9v12"/></svg>',
    settings: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 8.92 4a1.65 1.65 0 0 0 1-1.51V2a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 15 3.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.14.48.5.86 1 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg>',
    text: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7V4h16v3"/><path d="M9 20h6"/><path d="M12 4v16"/></svg>',
    wide: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 7 3 12l5 5"/><path d="M16 7l5 5-5 5"/><path d="M3 12h18"/></svg>'
  };

  function button(name, label, className) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "article-toolbar__button" + (className ? " " + className : "");
    el.setAttribute("aria-label", label);
    el.title = label;
    el.innerHTML = icon[name];
    return el;
  }

  var toolbar = document.createElement("div");
  toolbar.className = "article-toolbar";
  toolbar.setAttribute("aria-label", "文章工具");

  var panel = document.createElement("div");
  panel.className = "article-toolbar__panel";

  var progress = document.createElement("div");
  progress.className = "article-toolbar__progress";
  progress.innerHTML = "<span></span>";

  var textButton = button("text", "字号", "article-toolbar__button--small");
  var wideButton = button("wide", "宽屏阅读", "article-toolbar__button--small");
  var settingsButton = button("settings", "阅读设置");
  var upButton = button("up", "返回顶部");

  panel.appendChild(textButton);
  panel.appendChild(wideButton);

  var main = document.createElement("div");
  main.className = "article-toolbar__main";
  main.appendChild(settingsButton);
  main.appendChild(upButton);

  toolbar.appendChild(progress);
  toolbar.appendChild(panel);
  toolbar.appendChild(main);
  document.body.appendChild(toolbar);

  function savePrefs() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(prefs));
    } catch (error) {}
  }

  function applyPrefs() {
    document.body.classList.toggle("article-toolbar-wide", !!prefs.wide);
    content.style.fontSize = 18 + prefs.fontStep + "px";
    textButton.classList.toggle("is-active", prefs.fontStep !== 0);
    wideButton.classList.toggle("is-active", !!prefs.wide);
  }

  function updateVisibility() {
    var rect = article.getBoundingClientRect();
    var articleTop = window.scrollY + rect.top;
    var articleBottom = articleTop + article.offsetHeight;
    var scrollY = window.scrollY || document.documentElement.scrollTop;
    var viewportBottom = scrollY + window.innerHeight;
    var articleProgress = (scrollY + window.innerHeight * 0.35 - articleTop) / Math.max(1, article.offsetHeight - window.innerHeight * 0.35);
    var visible = scrollY > articleTop + 180 && viewportBottom < articleBottom + window.innerHeight * 0.42;

    toolbar.classList.toggle("is-visible", visible);
    progress.querySelector("span").style.height = Math.max(0, Math.min(100, articleProgress * 100)) + "%";

    if (!visible) {
      toolbar.classList.remove("is-open");
      settingsButton.setAttribute("aria-expanded", "false");
    }
  }

  settingsButton.setAttribute("aria-expanded", "false");
  settingsButton.addEventListener("click", function () {
    var isOpen = toolbar.classList.toggle("is-open");
    settingsButton.setAttribute("aria-expanded", String(isOpen));
  });

  upButton.addEventListener("click", function () {
    var target = Math.max(0, article.getBoundingClientRect().top + window.scrollY - 24);
    window.scrollTo({
      top: target,
      behavior: "smooth"
    });
  });

  textButton.addEventListener("click", function () {
    prefs.fontStep = prefs.fontStep >= 4 ? -1 : prefs.fontStep + 1;
    applyPrefs();
    savePrefs();
  });

  wideButton.addEventListener("click", function () {
    prefs.wide = !prefs.wide;
    applyPrefs();
    savePrefs();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      toolbar.classList.remove("is-open");
      settingsButton.setAttribute("aria-expanded", "false");
    }
  });

  applyPrefs();
  updateVisibility();
  window.addEventListener("scroll", updateVisibility, { passive: true });
  window.addEventListener("resize", updateVisibility);
})();
