/*
 * site_nav.js — shared site-navigation bar for every GlowUp dashboard page.
 *
 * Loaded as `<script src="/js/site_nav.js" defer></script>`.  Pages do NOT
 * need to include any `<nav>` markup — this script creates its own element
 * and prepends it to `document.body` on DOMContentLoaded.
 *
 * Why:
 *   The old pattern pasted a 10-line IIFE plus a <nav id="siteNav"> element
 *   into every HTML file.  Adding a new dashboard (vivint.html, 2026-04-12)
 *   revealed the defect: if you forget to paste both pieces, the new page
 *   silently ships with no nav bar.  "Code in one place, test in one place,
 *   fix in one place."
 *
 * Contract:
 *   The link list comes from GET /api/config/nav — a JSON object of the
 *   shape {"links": [{"label": "...", "href": "..."}, ...]}.  The server
 *   is authoritative; pages never hardcode links.  Adding a new link is
 *   a one-line edit in handlers/dashboard.py::_handle_get_nav_config().
 *
 * Active-link highlighting:
 *   A link is marked active when its href either (a) matches
 *   location.pathname exactly, or (b) is a path-prefix of it
 *   (e.g. "/thermal" stays active on "/thermal/host/foo").
 *   As a special case, the site root "/" maps to "/home".
 *
 * Failure mode:
 *   On fetch error the nav element remains empty — the page still
 *   renders, just without a nav bar.  This is intentional: a broken
 *   nav must never block dashboard content from loading.
 */

(function () {
    "use strict";

    // Inline style keeps the bar usable even before any stylesheet
    // parses — matches the visual of the old pasted-in IIFE exactly.
    var NAV_STYLE =
        "display:flex;gap:18px;padding:6px 16px;" +
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
        "font-size:0.8rem;letter-spacing:0.04em;";

    // rgba colours for active / inactive links — dim white on dark bg.
    var COLOR_ACTIVE = "rgba(255,255,255,0.7)";
    var COLOR_INACTIVE = "rgba(255,255,255,0.35)";

    function render(links) {
        // Reuse an existing element if the page provides one — this
        // preserves page-specific CSS (e.g. io.html's .nav class,
        // shopping.html's themed nav).  Otherwise create one with
        // inline styles and prepend to <body>.  New dashboards need
        // only drop the <script> tag and get an auto-styled bar.
        var nav = document.getElementById("siteNav");
        var created = false;
        if (!nav) {
            nav = document.createElement("nav");
            nav.id = "siteNav";
            nav.setAttribute("style", NAV_STYLE);
            created = true;
        } else if (nav.childNodes.length > 0) {
            // Don't double-render if the script somehow runs twice.
            return;
        }

        var path = location.pathname;
        links.forEach(function (link) {
            var a = document.createElement("a");
            a.href = link.href;
            a.textContent = link.label;
            a.style.textDecoration = "none";
            // Exact match, OR proper path-prefix match (so sub-pages
            // like /thermal/host/foo still highlight "Thermal").  The
            // "+ '/'" guard prevents /photos from activating /p.
            var isActive =
                (link.href === path) ||
                (path === "/" && link.href === "/home") ||
                (link.href !== "/" &&
                 path.indexOf(link.href + "/") === 0);
            a.style.color = isActive ? COLOR_ACTIVE : COLOR_INACTIVE;
            if (isActive) {
                a.style.fontWeight = "600";
            }
            nav.appendChild(a);
        });

        // If we created the element, prepend to <body> so the bar
        // sits above any page content.  If the page provided one, it
        // is already in the DOM at its chosen location.
        if (created) {
            if (document.body.firstChild) {
                document.body.insertBefore(nav, document.body.firstChild);
            } else {
                document.body.appendChild(nav);
            }
        }
    }

    function boot() {
        fetch("/api/config/nav")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var links = (data && data.links) || [];
                render(links);
            })
            .catch(function () {
                // Intentional: nav is best-effort, never blocks the page.
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
