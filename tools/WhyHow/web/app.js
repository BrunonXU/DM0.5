(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { items: [], kind: "markdown", route: null, abort: null, loadId: 0, toc: [], last: {}, toastTimer: null };
  const kindNames = { html: "交互讲解", markdown: "文字阅读" };
  let mermaidCount = 0;
  let scrollScheduled = false;

  function textElement(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    el.textContent = text || "";
    return el;
  }
  function dateText(value) {
    if (!value) return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai" }).format(d);
  }
  function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").classList.add("visible");
    state.toastTimer = setTimeout(() => $("toast").classList.remove("visible"), 2300);
  }
  async function copy(value) {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
      else {
        const field = document.createElement("textarea");
        field.value = value;
        field.style.cssText = "position:fixed;left:-10000px;top:0";
        document.body.append(field);
        field.select();
        const ok = document.execCommand("copy");
        field.remove();
        if (!ok) throw new Error("Clipboard unavailable");
      }
      return true;
    } catch { toast("复制未成功，请从地址栏或正文手动复制"); return false; }
  }
  async function request(url, signal) {
    const response = await fetch(url, { cache: "no-store", signal, headers: { Accept: "application/json" } });
    const type = response.headers.get("content-type") || "";
    const data = type.includes("json") ? await response.json() : null;
    if (!response.ok) {
      const detail = data && (data.message || data.error);
      throw new Error(typeof detail === "string" ? detail : `请求失败（${response.status}）。请确认本地服务可用。`);
    }
    if (!data) throw new Error("服务没有返回可读取的内容，请刷新后重试。");
    return data;
  }
  function currentRoute() {
    const params = new URLSearchParams(location.search);
    const anchor = params.get("anchor") || decodeHash(location.hash);
    if (params.has("html")) return { kind: "html", id: params.get("html"), anchor };
    if (params.has("doc")) return { kind: "markdown", id: params.get("doc"), anchor };
    if (params.has("path")) return { kind: "markdown", path: params.get("path"), anchor };
    return null;
  }
  function decodeHash(hash) {
    try { return decodeURIComponent(hash.replace(/^#/, "")); } catch { return hash.replace(/^#/, ""); }
  }
  function routeUrl(route) {
    const url = new URL("/", location.origin);
    if (route.path) url.searchParams.set("path", route.path);
    else url.searchParams.set(route.kind === "html" ? "html" : "doc", route.id);
    if (route.anchor) url.hash = route.anchor;
    return url.pathname + url.search + url.hash;
  }
  function navigate(route, replace = false) {
    closeLibrary();
    history[replace ? "replaceState" : "pushState"]({}, "", routeUrl(route));
    loadRoute(route);
  }
  function activateKind(kind) {
    state.kind = kind;
    document.body.dataset.kind = kind;
    for (const tab of document.querySelectorAll(".mode-tab")) {
      const selected = tab.dataset.kind === kind;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    $("content-panel").setAttribute("aria-labelledby", `tab-${kind}`);
    $("context-kind").textContent = kindNames[kind];
    for (const button of document.querySelectorAll("[data-font]")) button.disabled = kind === "html";
    renderCatalog();
  }
  function renderCatalog() {
    const search = $("search").value.trim().toLocaleLowerCase();
    const all = state.items.filter((item) => item.kind === state.kind);
    const matches = all.filter((item) => [item.title, item.description, item.category].filter(Boolean).join(" ").toLocaleLowerCase().includes(search));
    $("item-count").textContent = `${matches.length} 篇`;
    $("html-count").textContent = state.items.filter((item) => item.kind === "html").length;
    $("markdown-count").textContent = state.items.filter((item) => item.kind === "markdown").length;
    $("catalog").replaceChildren();
    if (!matches.length) {
      $("catalog").append(textElement("p", "sidebar-message", search ? "没有找到相关内容。试试更短的关键词。" : "这里还没有内容。刷新可重新读取本地书架。"));
      return;
    }
    const groups = new Map();
    for (const item of matches) {
      const category = item.category || "知识笔记";
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(item);
    }
    for (const [category, items] of groups) {
      const group = textElement("section", "catalog-group");
      const heading = textElement("h2", "", category);
      heading.append(textElement("span", "", String(items.length)));
      group.append(heading);
      for (const item of items) {
        const selected = state.route && state.route.id === item.id && state.route.kind === item.kind;
        const button = textElement("button", `catalog-item${selected ? " active" : ""}`);
        button.type = "button";
        if (selected) button.setAttribute("aria-current", "page");
        button.append(textElement("span", "catalog-item-title", item.title));
        if (item.description) button.append(textElement("span", "catalog-item-description", item.description));
        if (item.updated_at) button.append(textElement("span", "catalog-item-meta", dateText(item.updated_at)));
        button.addEventListener("click", () => navigate({ kind: item.kind, id: item.id }));
        group.append(button);
      }
      $("catalog").append(group);
    }
  }
  function showState(type, message = "") {
    for (const id of ["loading", "error", "document", "interactive"]) $(id).hidden = id !== type;
    if (type === "error") $("error-message").textContent = message;
  }
  function updateMetadata(data) {
    $("context-title").textContent = data.title || "未命名文档";
    $("source-label").textContent = data.source_label || "本地 Projects";
    $("article-source").textContent = data.source_label || "";
    $("source-updated").textContent = data.updated_at ? `更新于 ${dateText(data.updated_at)}` : "";
    $("article-date").textContent = data.updated_at ? `${dateText(data.updated_at)} · 笔记` : "";
    document.title = `${data.title || "知识空间"} · Why&How`;
  }
  function renderToc(toc) {
    state.toc = toc;
    $("toc").replaceChildren();
    $("reading-progress").textContent = state.kind === "html" ? "—" : "0%";
    if (!toc.length) {
      $("toc").append(textElement("p", "toc-placeholder", state.kind === "html" ? "在讲解中探索与操作。\n需要更大空间时，可打开独立窗口。" : "这篇内容没有章节标题。\n慢慢读，也很好。"));
      return;
    }
    for (const heading of toc) {
      if (!heading.id || !heading.title) continue;
      const link = textElement("a", `level-${Math.min(Math.max(heading.level || 2, 2), 4)}`, heading.title);
      link.href = `#${encodeURIComponent(heading.id)}`;
      link.dataset.anchor = heading.id;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        goToAnchor(heading.id, true);
      });
      $("toc").append(link);
    }
  }
  function goToAnchor(anchor, record = false) {
    const el = document.getElementById(anchor);
    if (!el || !$("markdown-body").contains(el)) return;
    const area = $("reading-area");
    area.scrollTo({ top: el.getBoundingClientRect().top - area.getBoundingClientRect().top + area.scrollTop - 26, behavior: record ? "smooth" : "auto" });
    if (record) {
      const url = new URL(location.href);
      url.searchParams.delete("anchor");
      url.hash = anchor;
      history.pushState({}, "", url.pathname + url.search + url.hash);
      if (state.route) state.route.anchor = anchor;
    }
  }
  function updateScroll() {
    scrollScheduled = false;
    if (state.kind !== "markdown") return;
    const area = $("reading-area");
    const range = area.scrollHeight - area.clientHeight;
    $("reading-progress").textContent = `${range > 0 ? Math.min(100, Math.round(area.scrollTop / range * 100)) : 100}%`;
    const top = area.getBoundingClientRect().top + 90;
    let active = state.toc.length ? state.toc[0].id : "";
    for (const item of state.toc) {
      const heading = document.getElementById(item.id);
      if (heading && heading.getBoundingClientRect().top <= top) active = item.id;
    }
    for (const a of $("toc").querySelectorAll("a")) {
      a.classList.toggle("active", a.dataset.anchor === active);
      if (a.dataset.anchor === active) a.setAttribute("aria-current", "location");
      else a.removeAttribute("aria-current");
    }
  }
  function enhanceDocument() {
    for (const table of $("markdown-body").querySelectorAll("table")) {
      const wrapper = textElement("div", "table-wrap");
      table.replaceWith(wrapper);
      wrapper.append(table);
    }
    for (const pre of $("markdown-body").querySelectorAll("pre")) {
      if (pre.querySelector("code.language-mermaid")) continue;
      const code = pre.textContent;
      const button = textElement("button", "copy-code", "复制");
      button.type = "button";
      button.setAttribute("aria-label", "复制代码块");
      button.addEventListener("click", async () => { if (await copy(code)) toast("代码已复制"); });
      pre.prepend(button);
    }
    for (const a of $("markdown-body").querySelectorAll("a[href]")) {
      const url = new URL(a.href, location.href);
      if (url.origin !== location.origin || (url.pathname !== "/" && !a.getAttribute("href").startsWith("#"))) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
    }
    for (const img of $("markdown-body").querySelectorAll("img")) {
      img.tabIndex = 0;
      img.setAttribute("role", "button");
      img.setAttribute("aria-label", `查看原图：${img.alt || "文档图片"}`);
      const open = () => {
        $("full-image").src = img.currentSrc || img.src;
        $("full-image").alt = img.alt || "文档原图";
        $("image-caption").textContent = img.alt || "原图";
        $("open-image").href = img.currentSrc || img.src;
        $("image-dialog").showModal();
      };
      img.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); open(); });
      img.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
      });
      img.addEventListener("load", updateScroll);
    }
  }
  async function renderDiagrams(loadId) {
    const sources = [...$("markdown-body").querySelectorAll("pre > code.language-mermaid")];
    if (window.mermaid) window.mermaid.initialize({
      startOnLoad: false, securityLevel: "strict", theme: "base",
      themeVariables: { primaryColor: "#f5f4f1", primaryTextColor: "#202020", primaryBorderColor: "#c8c4bc", lineColor: "#8b8176", secondaryColor: "#f7eee8", tertiaryColor: "#fdfdfc", fontFamily: "sans-serif" }
    });
    for (const code of sources) {
      if (loadId !== state.loadId) return;
      const pre = code.parentElement;
      const raw = code.textContent;
      try {
        if (!window.mermaid) throw new Error("Mermaid unavailable");
        const result = await window.mermaid.render(`wh-diagram-${++mermaidCount}`, raw);
        if (loadId !== state.loadId || !pre.isConnected) return;
        const diagram = textElement("div", "mermaid-diagram");
        diagram.innerHTML = result.svg;
        diagram.setAttribute("role", "img");
        diagram.setAttribute("aria-label", "文档流程图");
        pre.replaceWith(diagram);
        if (result.bindFunctions) result.bindFunctions(diagram);
      } catch {
        if (loadId !== state.loadId || !pre.isConnected) return;
        const details = textElement("details", "diagram-fallback");
        details.open = true;
        details.append(textElement("summary", "", "流程图暂未渲染，保留原始内容"));
        const fallback = document.createElement("pre");
        fallback.append(textElement("code", "", raw));
        details.append(fallback);
        pre.replaceWith(details);
      }
    }
  }
  async function loadRoute(route) {
    const loadId = ++state.loadId;
    if (state.abort) state.abort.abort();
    state.abort = new AbortController();
    state.route = route;
    state.last[route.kind] = route;
    activateKind(route.kind);
    $("copy-link").disabled = false;
    $("open-interactive").hidden = route.kind !== "html";
    $("reading-area").scrollTop = 0;
    // Re-navigating a hidden iframe adds joint history entries and consumes Back.
    // A fresh iframe has no previous document history; Markdown needs no navigation.
    const previousFrame = $("interactive-frame");
    if (previousFrame.hasAttribute("src")) {
      const freshFrame = previousFrame.cloneNode(false);
      freshFrame.removeAttribute("src");
      previousFrame.replaceWith(freshFrame);
    }
    renderToc([]);
    showState("loading");
    const item = state.items.find((it) => it.id === route.id && it.kind === route.kind);
    if (item) updateMetadata(item);
    try {
      if (route.kind === "html") {
        if (!item) throw new Error("目录中没有找到这篇交互讲解。请刷新目录或从左侧重新选择。");
        updateMetadata(item);
        $("interactive-title").textContent = item.title;
        $("interactive-description").textContent = item.description || "在下方操作，观察每一步如何影响结果。";
        const iframeUrl = `/interactive/${encodeURIComponent(route.id)}/`;
        $("open-interactive").href = iframeUrl;
        $("interactive-frame").title = item.title;
        $("iframe-status").hidden = false;
        $("interactive-frame").onload = () => {
          if (loadId === state.loadId) $("iframe-status").hidden = true;
        };
        $("interactive-frame").src = iframeUrl;
        showState("interactive");
        return;
      }
      const endpoint = route.path ? `/api/document?path=${encodeURIComponent(route.path)}` : `/api/document/${encodeURIComponent(route.id)}`;
      const data = await request(endpoint, state.abort.signal);
      if (loadId !== state.loadId) return;
      updateMetadata(data);
      $("article-category").textContent = (item && item.category) || "READ & UNDERSTAND";
      $("markdown-body").innerHTML = data.html || "";
      renderToc(data.toc || []);
      enhanceDocument();
      showState("document");
      await renderDiagrams(loadId);
      if (loadId !== state.loadId) return;
      if (route.anchor) goToAnchor(route.anchor);
      updateScroll();
    } catch (error) {
      if (error.name === "AbortError" || loadId !== state.loadId) return;
      showState("error", error.message || "本地内容暂时无法读取，请稍后重试。");
      $("context-title").textContent = "内容未能载入";
    }
  }
  async function loadCatalog(refresh = false) {
    $("refresh").disabled = true;
    try {
      const data = await request("/api/catalog");
      state.items = Array.isArray(data.items) ? data.items.filter((item) => item.kind === "markdown" || item.kind === "html") : [];
      $("service-label").textContent = `${(data.service && data.service.name) || "Why&How"} · ${(data.service && data.service.port) || "4243"}`;
      let route = currentRoute();
      if (!route) {
        const first = state.items.find((item) => item.kind === state.kind) || state.items[0];
        if (!first) {
          renderCatalog();
          showState("error", "书架还没有收录内容。加入内容后，点击右上角刷新。");
          $("error-title").textContent = "这里即将有新的理解";
          return;
        }
        route = { kind: first.kind, id: first.id };
        history.replaceState({}, "", routeUrl(route));
      }
      $("error-title").textContent = "暂时无法打开";
      await loadRoute(route);
      if (refresh) toast("已重新读取本地内容");
    } catch (error) {
      $("catalog").replaceChildren(textElement("p", "sidebar-message", "目录加载失败，请刷新重试。"));
      showState("error", error.message || "无法连接本地知识服务。");
    } finally { $("refresh").disabled = false; }
  }

  for (const tab of document.querySelectorAll(".mode-tab")) {
    tab.addEventListener("click", () => {
      const kind = tab.dataset.kind;
      const previous = state.last[kind];
      const first = state.items.find((item) => item.kind === kind);
      $("search").value = "";
      if (previous) navigate(previous);
      else if (first) navigate({ kind, id: first.id });
      else {
        activateKind(kind);
        $("copy-link").disabled = true;
        $("open-interactive").hidden = true;
        showState("error", "这个栏目还没有内容。加入内容后，点击刷新即可读取。");
        renderToc([]);
      }
    });
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const next = $(tab.dataset.kind === "html" ? "tab-markdown" : "tab-html");
      next.click();
      next.focus();
    });
  }
  function openLibrary() {
    if (!$('library-dialog').open) $('library-dialog').showModal();
    $('open-library').setAttribute('aria-expanded', 'true');
    $('search').focus();
  }
  function closeLibrary() {
    if ($('library-dialog').open) $('library-dialog').close();
  }
  $('open-library').addEventListener('click', openLibrary);
  $('close-library').addEventListener('click', closeLibrary);
  $('library-dialog').addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); closeLibrary(); }
  });
  $('library-dialog').addEventListener('close', () => {
    $('open-library').setAttribute('aria-expanded', 'false');
    $('open-library').focus();
  });
  $('library-dialog').addEventListener('click', event => {
    const rect = $('library-dialog').getBoundingClientRect();
    if (event.target === $('library-dialog') && (event.clientX > rect.right || event.clientX < rect.left || event.clientY < rect.top || event.clientY > rect.bottom)) closeLibrary();
  });
  $("search").addEventListener("input", renderCatalog);
  $("refresh").addEventListener("click", () => loadCatalog(true));
  $("retry").addEventListener("click", () => loadCatalog(true));
  $("copy-link").addEventListener("click", async () => { if (await copy(location.href)) toast("当前阅读链接已复制"); });
  $("reading-area").addEventListener("scroll", () => {
    if (!scrollScheduled) { scrollScheduled = true; requestAnimationFrame(updateScroll); }
  }, { passive: true });
  $("markdown-body").addEventListener("click", (event) => {
    const a = event.target.closest("a");
    if (!a || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const href = a.getAttribute("href") || "";
    if (href.startsWith("#")) {
      event.preventDefault();
      goToAnchor(decodeHash(href), true);
      return;
    }
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || url.pathname !== "/") return;
    const params = url.searchParams;
    let route = null;
    const anchor = params.get("anchor") || decodeHash(url.hash);
    if (params.has("doc")) route = { kind: "markdown", id: params.get("doc"), anchor };
    else if (params.has("path")) route = { kind: "markdown", path: params.get("path"), anchor };
    else if (params.has("html")) route = { kind: "html", id: params.get("html"), anchor };
    if (route) { event.preventDefault(); navigate(route); }
  });
  window.addEventListener("popstate", () => {
    const route = currentRoute();
    if (!route) { loadCatalog(); return; }
    if (state.route && route.id === state.route.id && route.path === state.route.path && route.kind === state.route.kind) {
      state.route = route;
      if (route.anchor) goToAnchor(route.anchor);
      else $("reading-area").scrollTop = 0;
    } else loadRoute(route);
  });
  window.addEventListener("hashchange", () => {
    const anchor = decodeHash(location.hash);
    if (anchor && state.kind === "markdown") goToAnchor(anchor);
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openLibrary();
      $("search").focus();
      $("search").select();
    }
  });
  function setFont(name) {
    const sizes = { compact: "20px", standard: "22px", large: "24px" };
    if (!sizes[name]) name = "standard";
    document.documentElement.style.setProperty("--body-size", sizes[name]);
    for (const button of document.querySelectorAll("[data-font]")) button.setAttribute("aria-pressed", String(button.dataset.font === name));
    try { localStorage.setItem("whyhow-font", name); } catch {}
    updateScroll();
  }
  for (const button of document.querySelectorAll("[data-font]")) button.addEventListener("click", () => setFont(button.dataset.font));
  $("close-image").addEventListener("click", () => $("image-dialog").close());
  $("image-dialog").addEventListener("click", (event) => {
    if (event.target !== $("image-dialog")) return;
    const r = $("image-dialog").getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) $("image-dialog").close();
  });
  let font = "standard";
  try { font = localStorage.getItem("whyhow-font") || font; } catch {}
  setFont(font);
  loadCatalog();
})();
