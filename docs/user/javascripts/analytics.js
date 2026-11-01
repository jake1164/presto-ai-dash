/* Analytics for the public website: the product page, the documentation and
 * the interactive demo.
 *
 * This is website code. The installed product — the daemon, its admin page and
 * the Presto app — sends nothing anywhere, and none of them load this file.
 *
 * One consent choice covers all three surfaces. Material keeps it under
 * "<scope pathname>.__consent", and its scope is always the documentation
 * root, so the product page resolves that same path from its own URL and both
 * read and write the identical key: accepting on either surface covers the
 * other, and nobody is asked twice.
 *
 * Nothing is requested from Google, and no cookie is set, until the stored
 * choice says "analytics": this file reads that first and only then starts
 * GA4. Rejecting writes Material's own "{}" so its banner agrees, and the
 * "cookie settings" link in either footer is how the choice is changed later.
 *
 * The event names and parameters are documented in docs/analytics.md.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  // Material defines its GA4 loader on the documentation pages. There, the
  // theme's own consent gate has already started analytics by the time this
  // file runs; on the product page nothing has.
  var material = typeof window.__md_analytics === "function";

  // -- the shared consent choice ---------------------------------------------

  // The path Material scopes its stored choices to: the documentation root.
  // On a documentation page the theme has computed it already and is the
  // authority; on the product page, which sits one level above the docs, the
  // same path is whatever "docs/" resolves to.
  function docsPath() {
    if (window.__md_scope && window.__md_scope.pathname) return window.__md_scope.pathname;
    return new URL("docs/", location.href).pathname;
  }

  function consentKey() { return docsPath() + ".__consent"; }

  // The stored shape is Material's: {"analytics": true} when accepted, {} when
  // rejected, and no entry at all when the visitor has not been asked yet.
  function readConsent() {
    try {
      var raw = localStorage.getItem(consentKey());
      return raw === null ? null : JSON.parse(raw);
    } catch (error) {
      return null;  // storage unavailable: treat as undecided, so nothing runs
    }
  }

  function allowed() {
    var consent = readConsent();
    return !!(consent && consent.analytics === true);
  }

  function setConsent(accept) {
    try {
      localStorage.setItem(consentKey(), JSON.stringify(accept ? { analytics: true } : {}));
    } catch (error) { /* the reload simply asks again */ }
  }

  function clearAnalyticsCookies() {
    // GA's default cookies, including the property-specific GA4 session cookie.
    // Try host-only and parent-domain cookies at the site and docs paths.
    var names = ["_ga", "_ga_82FD55P3RH", "_gid", "_gat"];
    document.cookie.split(";").forEach(function (cookie) {
      var name = cookie.split("=")[0].trim();
      if (/^(_ga(?:_|$)|_gid$|_gat(?:_|$))/.test(name)) names.push(name);
    });
    var paths = ["/"];
    [docsPath(), location.pathname].forEach(function (path) {
      var parts = path.split("/");
      for (var i = 1; i <= parts.length; i++) {
        var parent = parts.slice(0, i).join("/") || "/";
        paths.push(parent, parent.replace(/\/$/, "") + "/");
      }
    });
    var domains = [""];
    var labels = location.hostname.split(".");
    for (var i = 0; i < labels.length; i++) {
      var domain = labels.slice(i).join(".");
      domains.push("; Domain=" + domain, "; Domain=." + domain);
    }
    names.forEach(function (name) {
      paths.forEach(function (path) {
        domains.forEach(function (domain) {
          document.cookie = name + "=; Max-Age=0; Path=" + path + domain;
        });
      });
    });
  }

  // -- GA4 -------------------------------------------------------------------

  // The product page declares the property on its own script tag, so the
  // measurement ID lives in the page that uses it. mkdocs.yml stays the only
  // place the documentation's copy is configured.
  var property = (script && script.dataset && script.dataset.ga4) || "";

  function startGa4() {
    if (material) {
      // Material's loader uses a private gtag function. Expose the documented
      // command wrapper for our events without starting a second loader.
      window.gtag = window.gtag || function () { window.dataLayer.push(arguments); };
      return;
    }
    if (!property || window.gtag) return;
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    window.gtag("js", new Date());
    window.gtag("config", property);  // this is the one page view for this page
    var tag = document.createElement("script");
    tag.async = true;
    tag.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(property);
    document.head.appendChild(tag);
  }

  function track(name, params) {
    // No consent, or GA4 has not started: send nothing and queue nothing.
    if (!allowed() || typeof window.gtag !== "function") return;
    window.gtag("event", name, params || {});
  }

  // -- what a click means ----------------------------------------------------

  // External destinations worth counting, and only these: the documentation
  // footer links Material's own site on every page, and a click there says
  // nothing about whether anyone adopts this project.
  var OUTBOUND = [
    [/^github\.com$/i, /^\/jake1164\/presto-ai-dash\/issues(\/|$)/i, "github_issues"],
    [/^github\.com$/i, /^\/sponsors(\/|$)/i, "github_sponsors"],
    [/^github\.com$/i, /^\/jake1164\/presto-ai-dash\/?$/i, "github_repo"],
    [/^shop\.pimoroni\.com$/i, /^\/products\/presto\/?$/i, "pimoroni_store"],
    [/^buymeacoffee\.com$/i, /^\/jake1164s\/?$/i, "support"]
  ];

  // The getting-started path, by its last segment. Reading one of these is what
  // "proceeding to the installation instructions" means, from any surface.
  var GUIDES = [
    ["add-the-first-source", "source"],
    ["install-the-daemon", "daemon"],
    ["install-with-docker", "docker"],
    ["set-up-the-presto", "presto"]
  ];

  function guideFor(path) {
    for (var i = 0; i < GUIDES.length; i++) {
      if (new RegExp("/getting-started/" + GUIDES[i][0] + "/?$").test(path)) return GUIDES[i][1];
    }
    return "";
  }

  function destinationFor(url) {
    for (var i = 0; i < OUTBOUND.length; i++) {
      if (OUTBOUND[i][0].test(url.hostname) && OUTBOUND[i][1].test(url.pathname)) return OUTBOUND[i][2];
    }
    return "";
  }

  // Where the link sits, so a click can be attributed to a part of the page.
  function where(anchor) {
    if (material) {
      if (anchor.closest(".md-footer")) return "footer";
      if (anchor.closest(".md-header, .md-tabs, .md-nav, .md-sidebar")) return "navigation";
      return "content";
    }
    if (anchor.closest("footer")) return "footer";
    if (anchor.closest("header")) return "header";
    var section = anchor.closest("section[id]");
    return section ? section.id : "page";
  }

  function classify(anchor) {
    var url;
    try { url = new URL(anchor.getAttribute("href"), location.href); }
    catch (error) { return null; }

    var destination = destinationFor(url);
    if (destination) {
      return ["outbound_click", {
        destination: destination,
        link_domain: url.hostname,
        link_path: url.pathname,
        link_location: where(anchor)
      }];
    }
    if (url.origin !== location.origin) return null;  // another site we do not count

    var method = guideFor(url.pathname);
    if (method) {
      return ["install_click", {
        install_method: method,
        link_path: url.pathname,
        link_location: where(anchor)
      }];
    }
    // Only the product page counts links into the documentation: there it is
    // the step from the product page to the help. Within the docs, the page
    // view already says which page was read.
    if (!material && url.pathname.indexOf("/docs/") !== -1) {
      return ["docs_link_click", { link_path: url.pathname, link_location: where(anchor) }];
    }
    return null;
  }

  // One listener on the document, so it survives Material's instant navigation
  // (which replaces the page's content but not the document) and costs nothing
  // per page. A click produces at most one event.
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var anchor = event.target.closest && event.target.closest("a[href]");
    if (!anchor || anchor.hasAttribute("download")) return;
    var href = anchor.getAttribute("href");
    if (!href || href.charAt(0) === "#") return;
    var found = classify(anchor);
    if (found) track(found[0], found[1]);
  }, true);

  // -- the product page's own banner -----------------------------------------

  // Material renders the documentation's banner. The product page has its own,
  // in website/index.html, reading and writing the same stored choice.
  function banner() { return document.querySelector("[data-landing-consent]"); }

  function showBanner() {
    var box = banner();
    if (box) box.hidden = false;
  }

  function choose(accept) {
    setConsent(accept);
    if (!accept) clearAnalyticsCookies();
    location.reload();  // start, or stop, GA4 exactly as Material does
  }

  function wireBanner() {
    var box = banner();
    if (!box) return;
    box.addEventListener("click", function (event) {
      var button = event.target.closest && event.target.closest("button");
      if (!button) return;
      if (button.hasAttribute("data-consent-accept")) choose(true);
      else if (button.hasAttribute("data-consent-reject")) choose(false);
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-consent-open]"),
      function (link) {
        link.addEventListener("click", function (event) {
          event.preventDefault();
          showBanner();
          var first = box.querySelector("button");
          if (first) first.focus();
        });
      });
    if (readConsent() === null) showBanner();
  }

  // -- start -----------------------------------------------------------------

  function boot() {
    if (allowed()) startGa4();
    else if (readConsent() !== null) clearAnalyticsCookies();
    wireBanner();
  }

  // Capture before Material persists and reloads its consent form. A reset
  // means Reject; submitting with analytics unchecked also withdraws consent.
  ["reset", "submit"].forEach(function (action) {
    document.addEventListener(action, function (event) {
      if (!event.target.matches("form[name=consent]")) return;
      var analytics = event.target.querySelector("input[name=analytics]");
      if (action === "reset" || !analytics || !analytics.checked) clearAnalyticsCookies();
    }, true);
  });

  // The demo page drives its own events through this.
  window.prestoAnalytics = { track: track, allowed: allowed };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}());
