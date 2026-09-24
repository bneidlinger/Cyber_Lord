#!/usr/bin/env python3
"""Tell search engines the site changed, via IndexNow (Bing, Yandex, Seznam,
Naver, and others; Google does not participate).

    python tools/indexnow.py             submit every URL in sitemap.xml
    python tools/indexnow.py URL [URL]   submit specific URLs
    python tools/indexnow.py --dry-run   print the request, send nothing

Run it after a push that changes the site, once GitHub Pages has deployed.
No account is needed. The key file (<32 hex digits>.txt at the repository
root) is public by design: it only authorizes URLs under the site's path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from cl_common import ROOT, SITE_URL, use_utf8_output

ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_RE = re.compile(r"[0-9a-f]{32}")
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
RESPONSES = {
    200: "accepted",
    202: "received; the search engines will fetch the key file to verify it",
    400: "bad request",
    403: "key not valid; is the key file live on the site yet?",
    422: "the URLs do not match the host or the key file's location",
    429: "too many requests; try again later",
}


def find_key() -> str:
    keys = [path.stem for path in ROOT.glob("*.txt")
            if KEY_RE.fullmatch(path.stem) and path.read_text(encoding="utf-8").strip() == path.stem]
    if len(keys) != 1:
        raise SystemExit("Expected exactly one IndexNow key file (<32 hex digits>.txt) at the repository root.")
    return keys[0]


def sitemap_urls() -> list[str]:
    tree = ET.parse(ROOT / "sitemap.xml")
    return [loc.text.strip() for loc in tree.getroot().iter(f"{SITEMAP_NS}loc")]


def main() -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="URLs to submit (default: every URL in sitemap.xml)")
    parser.add_argument("--dry-run", action="store_true", help="print the request, send nothing")
    args = parser.parse_args()

    key = find_key()
    urls = args.urls or sitemap_urls()
    outside = [url for url in urls if not url.startswith(SITE_URL)]
    if outside:
        raise SystemExit(f"Not under {SITE_URL}: {', '.join(outside)}")
    payload = {
        "host": urlsplit(SITE_URL).hostname,
        "key": key,
        "keyLocation": f"{SITE_URL}{key}.txt",
        "urlList": urls,
    }
    if args.dry_run:
        print(f"POST {ENDPOINT}")
        print(json.dumps(payload, indent=2))
        return 0

    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "cyber-lord-operator-tools"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except urllib.error.URLError as exc:
        print(f"IndexNow: could not connect: {exc.reason}")
        return 2
    print(f"IndexNow: HTTP {status}, {RESPONSES.get(status, 'unexpected response')}. {len(urls)} URL(s) submitted.")
    return 0 if status in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main())
