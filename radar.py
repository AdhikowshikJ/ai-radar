#!/usr/bin/env python3
"""AI Radar: watches OpenRouter, arenas, model APIs, Hugging Face, Google Cloud,
sitemaps, subdomains and GitHub (releases + new repos),
and posts anything new to Discord webhooks. Standard library only.

Usage:
  python radar.py --dry-run        # print what would be posted, change nothing
  python radar.py                  # real run (needs webhook env vars)
  python radar.py --only openrouter,arenas
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path(__file__).parent / "state" / "seen.json"
UA = "Mozilla/5.0 (compatible; ai-radar/1.0)"
MAX_POSTS_PER_SOURCE = 10  # avoid flooding a channel if something big changes

ARENAS = {
    "text": "Text Arena",
    "vision": "Vision Arena",
    "text-to-image": "Text-to-Image Arena",
    "image-edit": "Image Edit Arena",
    "text-to-video": "Text-to-Video Arena",
    "image-to-video": "Image-to-Video Arena",
    "video-edit": "Video Edit Arena",
    "search": "Search Arena",
    "document": "Document Arena",
}

SITEMAPS = {
    "openai": {
        "url": "https://openai.com/sitemap.xml",
        "include": ("/index/", "/news/", "/research/", "/api/", "/business/", "/global-affairs/"),
    },
    "anthropic": {
        "url": "https://www.anthropic.com/sitemap.xml",
        "include": ("/news/", "/research/", "/claude", "/engineering/", "/customers/"),
    },
    # smaller sites: watch every page
    "cursor": {"url": "https://cursor.com/sitemap.xml", "include": ("/",), "min": 3},
    "antigravity": {"url": "https://antigravity.google/sitemap.xml", "include": ("/",), "min": 3},
    "deepseek": {"url": "https://www.deepseek.com/sitemap.xml", "include": ("/",), "min": 5},
    "z.ai": {"url": "https://z.ai/sitemap.xml", "include": ("/",), "min": 5},
    "mistral": {"url": "https://mistral.ai/sitemap.xml", "include": ("/news/", "/models", "/products/"), "min": 5},
    "minimax": {"url": "https://www.minimax.io/sitemap.xml", "include": ("/",), "min": 3},
}

RELEASE_REPOS = [
    "openai/codex",
    "openai/openai-python",
    "google-gemini/gemini-cli",
    "googleapis/python-genai",
    "anthropics/claude-code",
    "anthropics/anthropic-sdk-python",
]

HF_AUTHORS = [
    "openai", "google", "meta-llama", "facebook", "deepseek-ai", "Qwen", "mistralai",
    "moonshotai", "zai-org", "MiniMaxAI", "xai-org", "microsoft", "nvidia", "ibm-granite",
    "allenai", "stepfun-ai", "tencent", "XiaomiMiMo", "ByteDance-Seed", "baidu",
]

GITHUB_ORGS = [
    "openai", "anthropics", "google-gemini", "google-deepmind", "deepseek-ai", "QwenLM",
    "meta-llama", "mistralai", "MoonshotAI", "xai-org", "zai-org", "MiniMax-AI",
]

GCP_FEEDS = {
    "gemini-release-notes": "Gemini (Google Cloud)",
    "gemini-enterprise-release-notes": "Gemini Enterprise",
    "generative-ai-on-vertex-ai-release-notes": "Generative AI on Vertex AI",
}

# OpenAI-compatible "list models" endpoints: id -> (name, url, env var for the key, docs link)
OPENAI_COMPAT_APIS = {
    "openai": ("OpenAI", "https://api.openai.com/v1/models", "OPENAI_API_KEY", "https://platform.openai.com/docs/models"),
    "xai": ("xAI", "https://api.x.ai/v1/models", "XAI_API_KEY", "https://docs.x.ai/docs/models"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/models", "DEEPSEEK_API_KEY", "https://api-docs.deepseek.com/"),
    "mistral": ("Mistral", "https://api.mistral.ai/v1/models", "MISTRAL_API_KEY", "https://docs.mistral.ai/getting-started/models/"),
    "moonshot": ("Moonshot (Kimi)", "https://api.moonshot.ai/v1/models", "MOONSHOT_API_KEY", "https://platform.moonshot.ai/docs"),
    "minimax": ("MiniMax", "https://api.minimax.io/v1/models", "MINIMAX_API_KEY", "https://platform.minimax.io/docs"),
    "qwen": ("Qwen (Alibaba)", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models", "DASHSCOPE_API_KEY",
             "https://www.alibabacloud.com/help/en/model-studio/models"),
    "zai": ("Z.ai (GLM)", "https://api.z.ai/api/paas/v4/models", "ZAI_API_KEY", "https://docs.z.ai/"),
}

# RSS/Atom feeds: source id -> (label, url, regex that title/categories must match or None, color)
FEEDS = {
    "bedrock": ("AWS Bedrock", "https://aws.amazon.com/about-aws/whats-new/recent/feed/", r"bedrock", 0xFF9900),
    "azure": ("Azure AI Foundry", "https://www.microsoft.com/releasecommunications/api/v2/azure/rss",
              r"microsoft foundry|azure openai|foundry models|ai \+ machine learning", 0x0078D4),
    "cursor": ("Cursor changelog", "https://cursor.com/changelog/rss.xml", None, 0x000000),
}

# Changelog pages without feeds: we track their headings. (url, heading must match, heading to skip)
CHANGELOG_PAGES = {
    "Codex": ("https://developers.openai.com/codex/changelog", r"\d", r"^Codex CLI"),  # CLI versions come via releases
    "Antigravity": ("https://antigravity.google/docs/changelog", r"\d", None),
    "Gemini API": ("https://ai.google.dev/gemini-api/docs/changelog", r"\d{4}", None),
}

STATUS_PAGES = {
    "OpenAI": "https://status.openai.com/api/v2/components.json",
    "Anthropic": "https://status.anthropic.com/api/v2/components.json",
}

SUBDOMAIN_ROOTS = ["openai.com", "anthropic.com", "claude.ai", "chatgpt.com"]

# Optional: role to @mention per source (set the role ID, e.g. DISCORD_ROLE_ARENAS=1234567890)
ROLE_ENV = {
    "openrouter": "DISCORD_ROLE_OPENROUTER",
    "arenas": "DISCORD_ROLE_ARENAS",
    "designarena": "DISCORD_ROLE_DESIGNARENA",
    "sitemaps": "DISCORD_ROLE_PAGES",
    "releases": "DISCORD_ROLE_RELEASES",
    "api_anthropic": "DISCORD_ROLE_API_MODELS",
    "api_gemini": "DISCORD_ROLE_API_MODELS",
    **{f"api_{k}": "DISCORD_ROLE_API_MODELS" for k in OPENAI_COMPAT_APIS},
    "bedrock": "DISCORD_ROLE_CLOUD",
    "azure": "DISCORD_ROLE_CLOUD",
    "cursor": "DISCORD_ROLE_TOOLS",
    "changelogs": "DISCORD_ROLE_TOOLS",
    "benchmarks": "DISCORD_ROLE_BENCHMARKS",
    "status": "DISCORD_ROLE_STATUS",
    "huggingface": "DISCORD_ROLE_HUGGINGFACE",
    "subdomains": "DISCORD_ROLE_SUBDOMAINS",
    "newrepos": "DISCORD_ROLE_NEWREPOS",
    "gcp": "DISCORD_ROLE_CLOUD",
}
BRAND = os.environ.get("RADAR_BRAND", "AI Radar • Study with US")

# source id -> env var holding that channel's webhook (falls back to DISCORD_WEBHOOK_URL)
WEBHOOK_ENV = {
    "openrouter": "DISCORD_WEBHOOK_OPENROUTER",
    "arenas": "DISCORD_WEBHOOK_ARENAS",
    "designarena": "DISCORD_WEBHOOK_ARENAS",
    "sitemaps": "DISCORD_WEBHOOK_PAGES",
    "releases": "DISCORD_WEBHOOK_RELEASES",
    "api_anthropic": "DISCORD_WEBHOOK_API_MODELS",
    "api_gemini": "DISCORD_WEBHOOK_API_MODELS",
    **{f"api_{k}": "DISCORD_WEBHOOK_API_MODELS" for k in OPENAI_COMPAT_APIS},
    "bedrock": "DISCORD_WEBHOOK_CLOUD",
    "azure": "DISCORD_WEBHOOK_CLOUD",
    "cursor": "DISCORD_WEBHOOK_TOOLS",
    "changelogs": "DISCORD_WEBHOOK_TOOLS",
    "benchmarks": "DISCORD_WEBHOOK_BENCHMARKS",
    "status": ("DISCORD_WEBHOOK_STATUS", "DISCORD_WEBHOOK_SUBDOMAINS"),
    "huggingface": "DISCORD_WEBHOOK_HUGGINGFACE",
    "subdomains": "DISCORD_WEBHOOK_SUBDOMAINS",
    "newrepos": "DISCORD_WEBHOOK_NEWREPOS",
    "gcp": ("DISCORD_WEBHOOK_CLOUD", "DISCORD_WEBHOOK_GOOGLE_CLOUD"),
}


def http_get(url, timeout=40, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":  # some servers gzip even when we don't ask
        import gzip
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- sources
# Each source returns (items, ok). item = {"key", "title", "url", "desc", "fields", "color"}.
# ok=False means "fetch looked broken", so we skip it and keep the old state
# (otherwise a blip would make everything look new on the next good run).


def fetch_openrouter():
    data = json.loads(http_get("https://openrouter.ai/api/v1/models"))["data"]
    if len(data) < 50:
        return [], False
    items = []
    for m in data:
        p = m.get("pricing") or {}
        try:
            pin = float(p.get("prompt", 0)) * 1_000_000
            pout = float(p.get("completion", 0)) * 1_000_000
            price = "Free" if pin == 0 and pout == 0 else f"${pin:g} in / ${pout:g} out per 1M"
        except (TypeError, ValueError):
            price = "n/a"
        ctx = m.get("context_length")
        items.append({
            # "snap" = values we watch for changes (see track_changes)
            "snap": {"Name": m.get("name") or m["id"], "Context": ctx,
                     "Input price": p.get("prompt"), "Output price": p.get("completion")},
            "key": m["id"],
            "title": m.get("name") or m["id"],
            "url": f"https://openrouter.ai/{m['id']}",
            "desc": (m.get("description") or "")[:300],
            "fields": [("Model ID", f"`{m['id']}`"), ("Context", f"{ctx:,}" if ctx else "n/a"), ("Price", price)],
            "color": 0x6366F1,
            "label": "New model on OpenRouter",
        })
    return items, True


def fetch_arenas():
    items, good = [], 0
    for slug, label in ARENAS.items():
        try:
            page = http_get(f"https://arena.ai/leaderboard/{slug}", timeout=60)
        except Exception as e:  # one arena failing shouldn't kill the rest
            print(f"  ! {slug}: {e}", file=sys.stderr)
            continue
        text = page.replace('\\"', '"')
        entries = re.findall(
            r'"rank":(\d+),.*?"modelDisplayName":"([^"]+)","rating":([\d.]+).*?"votes":(\d+),'
            r'"modelOrganization":"([^"]*)"',
            text,
        )
        if len(entries) < 10:
            print(f"  ! {slug}: only {len(entries)} entries parsed, skipping", file=sys.stderr)
            continue
        good += 1
        for rank, name, rating, votes, org in entries:
            items.append({
                "key": f"{slug}::{name}", "group": slug,
                "title": name,
                "url": f"https://arena.ai/leaderboard/{slug}",
                "desc": "",
                "fields": [("Arena", label), ("Organization", org or "?"), ("Rank", f"#{rank}"),
                           ("Score", f"{float(rating):.0f}"), ("Votes", f"{int(votes):,}")],
                "color": 0x57F287,
                "label": f"New {label} model",
            })
    return items, good > 0


ORG_PREFIXES = {
    "claude": "Anthropic", "gpt": "OpenAI", "o3": "OpenAI", "o4": "OpenAI", "codex": "OpenAI",
    "gemini": "Google", "gemma": "Google", "imagen": "Google", "veo": "Google",
    "grok": "xAI", "deepseek": "DeepSeek", "qwen": "Alibaba", "kimi": "Moonshot",
    "glm": "Zhipu", "minimax": "MiniMax", "mistral": "Mistral", "devstral": "Mistral",
    "llama": "Meta", "muse": "Meta", "nova": "Amazon", "mai": "Microsoft", "phi": "Microsoft",
    "mimo": "Xiaomi", "step": "StepFun", "hy3": "Tencent", "hunyuan": "Tencent", "solar": "Upstage",
    "ernie": "Baidu", "doubao": "ByteDance", "seed": "ByteDance",
}


def guess_org(name):
    n = name.lower()
    for prefix, org in ORG_PREFIXES.items():
        if n.startswith(prefix):
            return org
    return "?"


def fetch_designarena():
    page = http_get("https://www.designarena.ai/leaderboard", timeout=60).replace('\\"', '"')
    items = {}
    # page embeds {"category":"allcategories"...,"modelStats":[{"model":..,"elo":..,"isNew":..}]}
    for m in re.finditer(r'\{"model":"([^"]+)","wins":(\d+),"losses":(\d+).*?"total":(\d+),"winRate":([\d.]+),'
                         r'"elo":([\d.]+).*?"isNew":(true|false)', page):
        name, wins, losses, total, wr, elo, is_new = m.groups()
        if name in items:
            continue
        items[name] = {
            "key": name, "title": name, "url": "https://www.designarena.ai/leaderboard", "desc": "",
            "fields": [("Arena", "Design Arena"), ("Organization", guess_org(name)), ("Elo", f"{float(elo):.0f}"),
                       ("Win rate", f"{wr}%"), ("Battles", f"{int(total):,}")],
            "color": 0x57F287, "label": "New Design Arena model",
        }
    return list(items.values()), len(items) >= 20


def _sitemap_urls(url, depth=0):
    root = ET.fromstring(http_get(url, timeout=60))
    locs = [e.text.strip() for e in root.iter() if e.tag.endswith("}loc") and e.text]
    if root.tag.endswith("sitemapindex") and depth < 1:
        urls = []
        for sub in locs[:60]:  # sanity cap
            try:
                urls += _sitemap_urls(sub, depth + 1)
            except Exception as e:
                print(f"  ! sub-sitemap {sub}: {e}", file=sys.stderr)
        return urls
    return locs


def fetch_sitemaps():
    items, good = [], 0
    for name, cfg in SITEMAPS.items():
        try:
            urls = _sitemap_urls(cfg["url"])
        except Exception as e:
            print(f"  ! sitemap {name}: {e}", file=sys.stderr)
            continue
        if len(urls) < cfg.get("min", 20):
            print(f"  ! sitemap {name}: only {len(urls)} urls, skipping", file=sys.stderr)
            continue
        good += 1
        for u in urls:
            path = "/" + u.split("//", 1)[-1].split("/", 1)[-1]
            if any(path.startswith(p) or p.rstrip("/") == path for p in cfg["include"]) and path.strip("/"):
                items.append({
                    "key": u, "group": name, "title": path, "url": u, "desc": "",
                    "fields": [("Site", name)], "color": 0xFEE75C,
                    "label": f"New {name} page",
                })
    return items, good > 0


def fetch_releases():
    items, good = [], 0
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for repo in RELEASE_REPOS:
        try:
            root = ET.fromstring(http_get(f"https://github.com/{repo}/releases.atom"))
        except Exception as e:
            print(f"  ! {repo}: {e}", file=sys.stderr)
            continue
        good += 1
        for e in root.findall("a:entry", ns):
            link = e.find("a:link", ns)
            if "nightly" in e.findtext("a:title", "", ns).lower():
                continue  # skip noisy nightly builds
            body = re.sub(r"<[^>]+>", " ", html.unescape(e.findtext("a:content", "", ns)))
            items.append({
                "key": e.findtext("a:id", "", ns), "group": repo,
                "title": f"{repo} {e.findtext('a:title', '', ns)}",
                "url": link.get("href") if link is not None else f"https://github.com/{repo}/releases",
                "desc": re.sub(r"\s+", " ", body).strip()[:300],
                "fields": [("Repo", repo)], "color": 0x5865F2,
                "label": "New release",
            })
    return items, good > 0


# --- model APIs: each provider is its own source so adding a key later doesn't flood

def _api_items(provider, ids, url):
    return [{
        "key": mid, "title": mid, "url": url, "desc": "",
        "fields": [("Provider", provider), ("Model ID", f"`{mid}`")],
        "color": 0xEB459E, "label": f"New {provider} API model",
    } for mid in ids]


def make_openai_compat_fetcher(pid):
    name, url, env, docs = OPENAI_COMPAT_APIS[pid]

    def fetch():
        key = os.environ.get(env)
        if not key:
            return [], False
        d = json.loads(http_get(url, headers={"Authorization": f"Bearer {key}"}))
        ids = [m["id"] for m in d.get("data", [])]
        return _api_items(name, ids, docs), len(ids) > 0
    return fetch


def fetch_api_anthropic():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return [], False
    ids, after = [], None
    for _ in range(10):
        url = "https://api.anthropic.com/v1/models?limit=100" + (f"&after_id={after}" if after else "")
        d = json.loads(http_get(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}))
        ids += [m["id"] for m in d["data"]]
        if not d.get("has_more"):
            break
        after = d.get("last_id")
    return _api_items("Anthropic", ids, "https://docs.anthropic.com/en/docs/about-claude/models"), len(ids) > 2


def fetch_api_gemini():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return [], False
    ids, token = [], ""
    for _ in range(10):
        d = json.loads(http_get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
                                + (f"&pageToken={token}" if token else ""), headers={"x-goog-api-key": key}))
        ids += [m["name"].removeprefix("models/") for m in d.get("models", [])]
        token = d.get("nextPageToken")
        if not token:
            break
    return _api_items("Gemini", ids, "https://ai.google.dev/gemini-api/docs/models"), len(ids) > 5


def fetch_huggingface():
    items, good = [], 0
    for author in HF_AUTHORS:
        try:
            models = json.loads(http_get(
                f"https://huggingface.co/api/models?author={author}&sort=createdAt&direction=-1&limit=20"))
        except Exception as e:
            print(f"  ! hf {author}: {e}", file=sys.stderr)
            continue
        good += 1
        for m in models:
            tag = m.get("pipeline_tag") or "n/a"
            items.append({
                "key": m["id"], "group": author, "title": m["id"], "url": f"https://huggingface.co/{m['id']}", "desc": "",
                "fields": [("Organization", author), ("Type", tag), ("Created", (m.get("createdAt") or "")[:10])],
                "color": 0xFFD21E, "label": "New Hugging Face model",
            })
    return items, good >= len(HF_AUTHORS) // 2


def fetch_newrepos():
    items, good = [], 0
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    for org in GITHUB_ORGS:
        try:
            repos = json.loads(http_get(
                f"https://api.github.com/orgs/{org}/repos?sort=created&direction=desc&per_page=30", headers=headers))
        except Exception as e:
            print(f"  ! github {org}: {e}", file=sys.stderr)
            continue
        good += 1
        for r in repos:
            items.append({
                "key": r["full_name"], "group": org, "title": r["full_name"], "url": r["html_url"],
                "desc": (r.get("description") or "")[:300],
                "fields": [("Org", org), ("Language", r.get("language") or "n/a"), ("Created", r["created_at"][:10])],
                "color": 0x24292F, "label": "New GitHub repo",
            })
    return items, good >= len(GITHUB_ORGS) // 2


def fetch_subdomains():
    # public certificate-transparency logs via crt.sh (slow, so a failure just skips this run)
    items, good = [], 0
    for root in SUBDOMAIN_ROOTS:
        try:
            certs = json.loads(http_get(f"https://crt.sh/?q=%25.{root}&output=json&exclude=expired", timeout=90))
        except Exception as e:
            print(f"  ! crt.sh {root}: {e}", file=sys.stderr)
            continue
        good += 1
        names = set()
        for c in certs:
            for n in c.get("name_value", "").split("\n"):
                n = n.strip().lower().removeprefix("*.")
                if n.endswith(root) and n != root and "@" not in n:
                    names.add(n)
        for n in names:
            items.append({
                "key": n, "group": root, "title": n, "url": f"https://crt.sh/?q={n}", "desc": "", "icon": root,
                "fields": [("Domain", root)], "color": 0x1ABC9C, "label": "New subdomain",
            })
    return items, good > 0


def fetch_gcp():
    items, good = [], 0
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for feed, label in GCP_FEEDS.items():
        try:
            root = ET.fromstring(http_get(f"https://cloud.google.com/feeds/{feed}.xml"))
        except Exception as e:
            print(f"  ! gcp {feed}: {e}", file=sys.stderr)
            continue
        good += 1
        for e in root.findall("a:entry", ns):
            link = e.find("a:link", ns)
            body = re.sub(r"<[^>]+>", " ", html.unescape(e.findtext("a:content", "", ns)))
            items.append({
                "key": e.findtext("a:id", "", ns), "group": feed,
                "title": f"{label}: {e.findtext('a:title', '', ns)}",
                "url": link.get("href") if link is not None else "https://cloud.google.com/release-notes",
                "desc": re.sub(r"\s+", " ", body).strip()[:600],
                "fields": [("Product", label)], "color": 0x4285F4, "label": "Google Cloud release notes",
            })
    return items, good > 0


def _parse_feed(text):
    """RSS or Atom -> list of dicts(id, title, link, summary, categories)."""
    root = ET.fromstring(text)
    a = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in root.iter("item"):  # RSS
        out.append({
            "id": it.findtext("guid") or it.findtext("link") or it.findtext("title"),
            "title": it.findtext("title", ""), "link": it.findtext("link", ""),
            "summary": it.findtext("description", ""),
            "categories": [c.text or "" for c in it.findall("category")],
        })
    for it in root.iter(a + "entry"):  # Atom
        link = it.find(a + "link")
        out.append({
            "id": it.findtext(a + "id"), "title": it.findtext(a + "title", ""),
            "link": link.get("href", "") if link is not None else "",
            "summary": it.findtext(a + "content", "") or it.findtext(a + "summary", ""),
            "categories": [c.get("term", "") for c in it.findall(a + "category")],
        })
    return out


def _clean(htmltext, n):
    t = re.sub(r"<[^>]+>", " ", html.unescape(htmltext or ""))
    return re.sub(r"\s+", " ", t).strip()[:n]


def make_feed_fetcher(fid):
    label, url, pattern, color = FEEDS[fid]

    def fetch():
        entries = _parse_feed(http_get(url, timeout=60))
        items = []
        for e in entries:
            hay = (e["title"] + " " + " ".join(e["categories"])).lower()
            if pattern and not re.search(pattern, hay):
                continue
            items.append({
                "key": e["id"], "title": e["title"], "url": e["link"] or url,
                "desc": _clean(e["summary"], 400), "fields": [("Source", label)],
                "color": color, "label": label,
            })
        return items, len(entries) > 0
    return fetch


def fetch_changelogs():
    items, good = [], 0
    for name, (url, must, skip) in CHANGELOG_PAGES.items():
        try:
            page = http_get(url, timeout=60)
        except Exception as e:
            print(f"  ! changelog {name}: {e}", file=sys.stderr)
            continue
        # split the page at each heading; text until the next heading is that entry's body
        parts = re.split(r"<h[1-4][^>]*>(.*?)</h[1-4]>", page, flags=re.S)
        entries = {}
        for i in range(1, len(parts) - 1, 2):
            h = _clean(parts[i], 200)
            if (h and re.search(must, h) and not (skip and re.search(skip, h))
                    and not re.fullmatch(r"[A-Z][a-z]+ \d{4}", h) and h not in entries):  # skip "September 2026"
                entries[h] = _clean(parts[i + 1], 500)
        if len(entries) < 3:
            print(f"  ! changelog {name}: only {len(entries)} entries, skipping", file=sys.stderr)
            continue
        good += 1
        for h, body in entries.items():
            items.append({
                "key": f"{name}::{h}", "group": name, "title": f"{name}: {h}", "url": url, "desc": body,
                "fields": [("Product", name)], "color": 0x2B2D31, "label": f"{name} changelog",
            })
    return items, good > 0


def fetch_benchmarks():
    import csv
    import io
    import zipfile
    req = urllib.request.Request("https://epoch.ai/data/benchmark_data.zip", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    items = []
    for fname in z.namelist():
        if not fname.endswith(".csv"):
            continue
        bench = fname[:-4].replace("_", " ").title().replace("Swe", "SWE").replace("Gpqa", "GPQA")
        rows = csv.DictReader(io.TextIOWrapper(z.open(fname), "utf-8", errors="replace"))
        for row in rows:
            model = row.get("Model version") or row.get("model") or ""
            if not model:
                continue
            score = row.get("Best score (across scorers)") or row.get("mean_score") or ""
            try:
                score = f"{float(score) * 100:.1f}%" if float(score) <= 1 else f"{float(score):g}"
            except ValueError:
                pass
            items.append({
                "key": f"{fname}::{model}", "group": fname, "title": f"{model} on {bench}",
                "url": "https://epoch.ai/benchmarks", "desc": "",
                "fields": [("Benchmark", bench), ("Score", score or "n/a"),
                           ("Organization", row.get("Organization") or "?"),
                           ("Released", row.get("Release date") or "n/a")],
                "color": 0xF1C40F, "label": "New benchmark result (Epoch AI)",
            })
    return items, len(items) > 100


def fetch_status():
    items, good = [], 0
    for name, url in STATUS_PAGES.items():
        try:
            comps = json.loads(http_get(url))["components"]
        except Exception as e:
            print(f"  ! status {name}: {e}", file=sys.stderr)
            continue
        good += 1
        for c in comps:
            items.append({
                "key": c["id"], "group": name, "title": f"{name} status: {c['name']}",
                "url": url.split("/api/")[0], "desc": _clean(c.get("description"), 300),
                "fields": [("Company", name), ("Status", c.get("status", "?"))],
                "color": 0x95A5A6, "label": "New status-page component",
            })
    return items, good > 0


SOURCES = {
    "openrouter": fetch_openrouter,
    "arenas": fetch_arenas,
    "designarena": fetch_designarena,
    "sitemaps": fetch_sitemaps,
    "releases": fetch_releases,
    **{f"api_{k}": make_openai_compat_fetcher(k) for k in OPENAI_COMPAT_APIS},
    "api_anthropic": fetch_api_anthropic,
    "api_gemini": fetch_api_gemini,
    "huggingface": fetch_huggingface,
    "newrepos": fetch_newrepos,
    "subdomains": fetch_subdomains,
    "gcp": fetch_gcp,
    **{k: make_feed_fetcher(k) for k in FEEDS},
    "changelogs": fetch_changelogs,
    "benchmarks": fetch_benchmarks,
    "status": fetch_status,
}

# ---------------------------------------------------------------- discord


def icon_url(item):
    """Site icon for the embed thumbnail: item's "icon" domain, else the domain of its link."""
    domain = item.get("icon") or urllib.parse.urlparse(item.get("url") or "").hostname
    if not domain:
        return None
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


def post_embed(webhook, item, role_id=None):
    now = int(time.time())
    fields = list(item["fields"])
    unknown_org = any(n == "Organization" and v in ("?", "", "Unknown") for n, v in fields)
    fields = [(n, "❓ unknown" if n == "Organization" and unknown_org else v) for n, v in fields]
    desc = f"**Discovered:** <t:{now}:F> (<t:{now}:R>)"
    if unknown_org:
        desc += "\n🕵️ **Unknown org, possibly a codename / stealth model**"
    if item["desc"]:
        desc += "\n\n" + item["desc"]
    embed = {
        "author": {"name": item["label"]},
        "title": item["title"][:256],
        "url": item["url"],
        "description": desc[:4000],
        "color": 0xED4245 if unknown_org else item["color"],
        "fields": [{"name": n, "value": str(v)[:1000], "inline": True} for n, v in fields],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": BRAND},
    }
    if icon_url(item):
        embed["thumbnail"] = {"url": icon_url(item)}
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if role_id:
        payload["content"] = f"<@&{role_id}> {item['label']}"
        payload["allowed_mentions"] = {"roles": [role_id]}
    body = json.dumps(payload).encode()
    for attempt in range(3):
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                time.sleep(wait + 0.5)
                continue
            print(f"  ! discord HTTP {e.code}: {e.read()[:200]}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"  ! discord error: {e}", file=sys.stderr)
            return False
    return False


def webhook_for(source):
    names = WEBHOOK_ENV[source]
    for n in ((names,) if isinstance(names, str) else names) + ("DISCORD_WEBHOOK_URL",):
        if os.environ.get(n):
            return os.environ[n]
    return None


# ---------------------------------------------------------------- change tracking


def _fmt_snap(field, v):
    if v is None or v == "":
        return "n/a"
    if "price" in field:
        try:
            f = float(v) * 1_000_000
            return "Free" if f == 0 else f"${f:g} / 1M"
        except (TypeError, ValueError):
            return str(v)
    return f"{v:,}" if isinstance(v, int) else str(v)


def track_changes(name, items, old_snap):
    """Compare items' "snap" values with last run. Returns (change_items, current_snap)."""
    cur = {i["key"]: i["snap"] for i in items if "snap" in i}
    by_key = {i["key"]: i for i in items}
    changes = []
    for k, now in cur.items():
        before = old_snap.get(k)
        if before is None or before == now:
            continue
        diffs = [(f, before.get(f), now.get(f)) for f in now if before.get(f) != now.get(f)]
        base = by_key[k]
        changes.append({
            "key": f"chg::{k}", "model": k, "title": base["title"], "url": base["url"],
            "desc": "\n".join(f"• **Old** {f}: **{_fmt_snap(f, a)}**\n• **New** {f}: **{_fmt_snap(f, b)}**"
                              for f, a, b in diffs),
            "fields": [("Model ID", f"`{k}`")], "color": 0xF39C12, "label": "Changed OpenRouter model",
        })
    # removals: skip if the list suddenly shrank a lot (probably an API hiccup, not real removals)
    gone = [k for k in old_snap if k not in cur]
    if gone and len(cur) >= 0.9 * len(old_snap):
        for k in gone:
            changes.append({
                "key": f"rm::{k}", "model": k, "title": old_snap[k].get("Name") or k,
                "url": f"https://openrouter.ai/{k}", "desc": "No longer listed on OpenRouter.",
                "fields": [("Model ID", f"`{k}`")], "color": 0x747F8D, "label": "Removed from OpenRouter",
            })
    return changes, cur


# ---------------------------------------------------------------- schedule
# The workflow fires every 5 min. Each source runs on its own interval, decided from the
# clock (no extra state, so no commit every 5 min). If GitHub skips a slot, slower sources
# just catch up on their next slot; they change slowly anyway.
FAST_MIN, MEDIUM_MIN, SLOW_MIN = 5, 15, 30
SCHEDULE_MIN = {
    # every 5 min: light and where being first matters
    "openrouter": FAST_MIN, "releases": FAST_MIN, "cursor": FAST_MIN,
    "bedrock": FAST_MIN, "azure": FAST_MIN, "gcp": FAST_MIN,
    **{k: FAST_MIN for k in SOURCES if k.startswith("api_")},
    # every 15 min: many requests per run
    "sitemaps": MEDIUM_MIN, "huggingface": MEDIUM_MIN, "newrepos": MEDIUM_MIN,
    "changelogs": MEDIUM_MIN, "designarena": MEDIUM_MIN,
    # every 30 min: heavy downloads or slow/fragile services
    "arenas": SLOW_MIN, "benchmarks": SLOW_MIN, "subdomains": SLOW_MIN, "status": SLOW_MIN,
}


def due_sources(now=None):
    slot = int((now or time.time()) // (FAST_MIN * 60))  # which 5-min slot we're in
    return [k for k in SOURCES if slot % (SCHEDULE_MIN.get(k, SLOW_MIN) // FAST_MIN) == 0]


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only; don't post or save")
    ap.add_argument("--only", help="comma-separated: " + ",".join(SOURCES))
    ap.add_argument("--all", action="store_true", help="ignore the 5/15/30 min schedule, run every source")
    ap.add_argument("--announce-first", action="store_true",
                    help="on a source's first run, post everything instead of just remembering it")
    args = ap.parse_args()

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if args.only:
        wanted = args.only.split(",")
    elif args.all or os.environ.get("RADAR_MODE") == "all":
        wanted = list(SOURCES)  # manual "Run workflow" checks everything
    else:
        wanted = due_sources()
    print(f"sources this run: {', '.join(wanted)}")

    for name in wanted:
        print(f"[{name}] fetching...")
        try:
            items, ok = SOURCES[name]()
        except Exception as e:
            print(f"[{name}] failed: {e}", file=sys.stderr)
            continue
        if not ok:
            print(f"[{name}] skipped (no data or no API key), keeping old state")
            continue

        seen = set(state.get(name, []))
        first_run = name not in state
        new = [i for i in items if i["key"] not in seen]
        # Drop duplicate keys inside a single fetch
        uniq = {i["key"]: i for i in new}
        new = list(uniq.values())

        # A group (arena, org, domain, feed...) seen for the first time is baselined silently,
        # so a flaky group that finally loads, or an org you add later, doesn't flood the channel.
        gkey = f"{name}#groups"
        present = {i["group"] for i in items if i.get("group")}
        if gkey not in state and not first_run:
            # upgrade from older state: a group is known only if we've already seen some of its items
            state[gkey] = sorted({i["group"] for i in items if i.get("group") and i["key"] in seen})
        known_groups = set(state.get(gkey, []))
        fresh = {i["key"] for i in new if i.get("group") and i["group"] not in known_groups}
        new = [i for i in new if i["key"] not in fresh]
        print(f"[{name}] {len(items)} items, {len(new)} new" + (" (first run)" if first_run else ""))

        announce = new if (not first_run or args.announce_first) else []
        skey = f"{name}#snap"
        changes, cur_snap = [], None
        if any("snap" in i for i in items):
            if skey in state:
                changes, cur_snap = track_changes(name, items, state[skey])
                announce = announce + changes
            else:
                cur_snap = {i["key"]: i["snap"] for i in items if "snap" in i}  # first time: baseline
            if changes:
                print(f"[{name}] {len(changes)} changed/removed")
        hook = webhook_for(name)
        posted = set()
        if announce and args.dry_run:
            for i in announce[:MAX_POSTS_PER_SOURCE]:
                print(f"   would post: {i['label']}: {i['title']} {i['url']}")
            if len(announce) > MAX_POSTS_PER_SOURCE:
                print(f"   ...and {len(announce) - MAX_POSTS_PER_SOURCE} more")
        elif announce and not hook:
            print(f"[{name}] no webhook set ({WEBHOOK_ENV[name]}), will post once it's added")
            continue
        elif announce:
            for i in announce[:MAX_POSTS_PER_SOURCE]:
                if post_embed(hook, i, os.environ.get(ROLE_ENV[name])):
                    posted.add(i["key"])
                time.sleep(1)
            skipped = announce[MAX_POSTS_PER_SOURCE:]
            if skipped:  # too many at once: mention it, then treat them as seen
                post_embed(hook, {
                    "title": f"{len(skipped)} more new items not shown", "url": skipped[0]["url"],
                    "desc": "\n".join(f"• {s['title']}" for s in skipped[:15])[:1500],
                    "fields": [], "color": 0x99AAB5, "label": name,
                })
                posted.update(s["key"] for s in skipped)

        if not args.dry_run:
            # First run (without --announce-first): remember everything so we don't spam.
            # Otherwise remember only what was posted; failed posts stay "new" and retry next run.
            keep = {i["key"] for i in items} if (first_run and not args.announce_first) else posted
            state[name] = sorted(seen | keep | fresh)
            if present:
                state[gkey] = sorted(known_groups | present)
            if cur_snap is not None:
                # keep the old values for changes that failed to post, so they retry next run
                snap = dict(cur_snap)
                for c in changes:
                    if c["key"] not in posted:
                        snap[c["model"]] = state[skey][c["model"]]
                state[skey] = snap

    if not args.dry_run:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=0))


if __name__ == "__main__":
    main()
