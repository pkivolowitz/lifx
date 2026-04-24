#!/usr/bin/env python3
"""Render PARAM_AS_SIGNAL.md → self-contained HTML with embedded Mermaid.js.

Output is a single HTML file with the Mermaid runtime inlined, so the file
works offline and can be emailed, AirDropped, or printed to PDF.
"""
from __future__ import annotations

import html
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "PARAM_AS_SIGNAL.md"
BUILD = REPO / "build"
CACHE = BUILD / ".cache"
OUT = BUILD / "param_as_signal.html"

MERMAID_VERSION = "11"
MERMAID_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"
MERMAID_CACHED = CACHE / f"mermaid-{MERMAID_VERSION}.min.js"


def ensure_mermaid() -> str:
    if not MERMAID_CACHED.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"fetching {MERMAID_URL}", file=sys.stderr)
        with urllib.request.urlopen(MERMAID_URL, timeout=30) as resp:
            MERMAID_CACHED.write_bytes(resp.read())
    return MERMAID_CACHED.read_text()


def render_body(src: Path) -> str:
    body = subprocess.run(
        ["pandoc", "-f", "gfm", "-t", "html", str(src)],
        check=True, capture_output=True, text=True,
    ).stdout

    def to_mermaid_div(match: re.Match) -> str:
        inner = match.group(1)
        raw = html.unescape(re.sub(r"</?code[^>]*>", "", inner))
        return f'<div class="mermaid">{raw}</div>'

    return re.sub(
        r'<pre class="mermaid">(.*?)</pre>',
        to_mermaid_div,
        body,
        flags=re.DOTALL,
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    --fg: #1a1a1a; --muted: #555; --bg: #fafafa; --paper: #ffffff;
    --accent: #2b6cb0; --rule: #e2e2e2; --code-bg: #f4f4f6;
  }}
  html, body {{ background: var(--bg); color: var(--fg); }}
  body {{
    font: 17px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    max-width: 860px; margin: 0 auto; padding: 56px 32px 96px;
    background: var(--paper);
    box-shadow: 0 0 0 1px var(--rule), 0 4px 24px rgba(0,0,0,0.04);
  }}
  h1 {{ font-size: 2.0em; line-height: 1.2; margin: 0 0 0.2em; }}
  h2 {{ font-size: 1.4em; margin: 2em 0 0.6em; padding-bottom: 6px; border-bottom: 1px solid var(--rule); }}
  h3 {{ font-size: 1.15em; margin: 1.6em 0 0.4em; }}
  p, li {{ margin: 0.6em 0; }}
  hr {{ border: 0; border-top: 1px solid var(--rule); margin: 2em 0; }}
  blockquote {{ margin: 1em 0; padding: 0.5em 1em; border-left: 3px solid var(--accent); background: #f6f9fc; color: var(--muted); }}
  a {{ color: var(--accent); }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.92em; background: var(--code-bg); padding: 1px 5px; border-radius: 3px; }}
  pre {{ background: var(--code-bg); padding: 14px 16px; border-radius: 6px; overflow-x: auto; font-size: 0.88em; line-height: 1.5; }}
  pre code {{ background: none; padding: 0; }}
  .mermaid {{
    background: #ffffff; border: 1px solid var(--rule); border-radius: 6px;
    padding: 18px; margin: 1.4em 0; text-align: center; overflow-x: auto;
  }}
  .mermaid svg {{ max-width: 100%; height: auto; }}
  em {{ color: var(--muted); }}
  @media print {{
    body {{ box-shadow: none; max-width: 100%; padding: 0; }}
    .mermaid {{ break-inside: avoid; }}
    h2, h3 {{ break-after: avoid; }}
  }}
</style>
</head>
<body>
{body}
<script>
{mermaid_js}
</script>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: 'default',
    flowchart: {{ htmlLabels: true, curve: 'basis' }},
    securityLevel: 'loose'
  }});
</script>
</body>
</html>
"""


def main() -> int:
    if not SRC.exists():
        print(f"missing source: {SRC}", file=sys.stderr)
        return 1
    BUILD.mkdir(parents=True, exist_ok=True)
    body = render_body(SRC)
    mermaid_js = ensure_mermaid()
    doc = TEMPLATE.format(
        title="Param-as-Signal — A Continuous-Binding Paradigm for Home Automation",
        body=body,
        mermaid_js=mermaid_js,
    )
    OUT.write_text(doc)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
