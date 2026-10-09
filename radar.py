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

for _k, _v in list(os.environ.items()):  # pasted secrets often carry spaces or newlines
    if (_k.endswith("_API_KEY") or _k.startswith("DISCORD_") or _k == "RADAR_STATE_TOKEN") and _v != _v.strip():
        os.environ[_k] = _v.strip()
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
    # all English pages ("mistral.ai" is a new key, so the wider set is baselined silently)
    "mistral.ai": {"url": "https://mistral.ai/sitemap.xml", "include": ("/",), "exclude": r"mistral\.ai/(fr|it)/", "min": 50},
    "minimax": {"url": "https://www.minimax.io/sitemap.xml", "include": ("/",), "min": 3},
    # Google, xAI, Microsoft
    "google blog": {"url": "https://blog.google/en-us/sitemap.xml", "include": ("/",), "min": 1000},
    "deepmind": {"url": "https://deepmind.google/sitemap.xml", "include": ("/",), "min": 100},
    "google ai dev": {"url": "https://ai.google.dev/sitemap.xml", "include": ("/",), "exclude": r"\?hl=", "min": 100},
    "xai": {"url": "https://x.ai/sitemap.xml", "include": ("/",), "min": 50},
    "microsoft ai": {"url": "https://microsoft.ai/sitemap.xml", "include": ("/",), "min": 20},
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

# Fast RSS checks for the busiest sites (every 5 min). Same group names as SITEMAPS so posts
# look the same; a page is only ever posted once, by whichever check sees it first.
FAST_PAGE_FEEDS = {
    "google blog": "https://blog.google/rss/",
    "openai": "https://openai.com/news/rss.xml",
    "deepmind": "https://deepmind.google/blog/rss.xml",
    "mistral.ai": "https://mistral.ai/rss.xml",
    "google developers blog": "https://developers.googleblog.com/feeds/posts/default",
}

# Trusted outlets for #news. filter=True: general tech feed, keep only AI stories.
NEWS_FEEDS = {
    "Bloomberg": ("https://feeds.bloomberg.com/technology/news.rss", True),
    "The Information": ("https://www.theinformation.com/feed", True),
    "Financial Times": ("https://www.ft.com/artificial-intelligence?format=rss", False),
    "SemiAnalysis": ("https://newsletter.semianalysis.com/feed", False),
    "Axios": ("https://api.axios.com/feed/", True),
    "Reuters": ("https://news.google.com/rss/search?q=site:reuters.com+(%22artificial+intelligence%22+OR+OpenAI+OR+"
                "Anthropic+OR+Nvidia+OR+%22AI%22)+when:2d&hl=en-US&gl=US&ceid=US:en", True),
}
AI_NEWS = re.compile(r"\bA\.?I\b|artificial intelligence|\b(OpenAI|Anthropic|Claude|ChatGPT|Gemini|DeepMind|LLMs?|"
                     r"Nvidia|GPUs?|xAI|Grok|Mistral|DeepSeek|Copilot|Llama|AGI|superintelligence|chatbots?|"
                     r"data cent(er|re)s?|machine learning|Sam Altman|Dario Amodei|Demis Hassabis|Perplexity|Cursor)\b")

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
    "news": "DISCORD_ROLE_NEWS",
    "fastpages": "DISCORD_ROLE_PAGES",
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
    "desktop_apps": "DISCORD_ROLE_DESKTOP_APPS",
    "mobile_ios": "DISCORD_ROLE_MOBILE_APPS",
    "mobile_android": "DISCORD_ROLE_MOBILE_APPS",
    "designarena_registry": "DISCORD_ROLE_DESIGNARENA",
    "sdk_models": "DISCORD_ROLE_API_MODELS",
    "litellm": "DISCORD_ROLE_API_MODELS",
    "arcprize": "DISCORD_ROLE_BENCHMARKS",
    "artificialanalysis": "DISCORD_ROLE_BENCHMARKS",
    "huggingface": "DISCORD_ROLE_HUGGINGFACE",
    "subdomains": "DISCORD_ROLE_SUBDOMAINS",
    "newrepos": "DISCORD_ROLE_NEWREPOS",
    "gcp": "DISCORD_ROLE_CLOUD",
}
BRAND = os.environ.get("RADAR_BRAND", "AI Leaks")

# source id -> env var holding that channel's webhook (falls back to DISCORD_WEBHOOK_URL)
WEBHOOK_ENV = {
    "openrouter": "DISCORD_WEBHOOK_OPENROUTER",
    "arenas": "DISCORD_WEBHOOK_ARENAS",
    "designarena": "DISCORD_WEBHOOK_ARENAS",
    "sitemaps": "DISCORD_WEBHOOK_PAGES",
    "news": "DISCORD_WEBHOOK_NEWS",
    "fastpages": "DISCORD_WEBHOOK_PAGES",
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
    "desktop_apps": "DISCORD_WEBHOOK_DESKTOP_APPS",
    "mobile_ios": "DISCORD_WEBHOOK_MOBILE_APPS",
    "mobile_android": "DISCORD_WEBHOOK_MOBILE_APPS",
    "designarena_registry": "DISCORD_WEBHOOK_ARENAS",
    "sdk_models": "DISCORD_WEBHOOK_API_MODELS",
    "litellm": "DISCORD_WEBHOOK_API_MODELS",
    "arcprize": "DISCORD_WEBHOOK_BENCHMARKS",
    "artificialanalysis": "DISCORD_WEBHOOK_BENCHMARKS",
    "huggingface": "DISCORD_WEBHOOK_HUGGINGFACE",
    "subdomains": "DISCORD_WEBHOOK_SUBDOMAINS",
    "newrepos": "DISCORD_WEBHOOK_NEWREPOS",
    "gcp": ("DISCORD_WEBHOOK_CLOUD", "DISCORD_WEBHOOK_GOOGLE_CLOUD"),
}


import threading

STATE_LOCK = threading.RLock()  # loop mode runs two lanes in parallel; state changes go through this
_TLS = threading.local()        # per-thread cache of plain GETs (arena pages are used by two trackers)


def _get_cache():
    if not hasattr(_TLS, "cache"):
        _TLS.cache = {}
    return _TLS.cache


def http_get(url, timeout=40, headers=None):
    cache = _get_cache()
    if not headers and url in cache:
        return cache[url]
    text = _http_get(url, timeout, headers)
    if not headers:
        cache[url] = text
    return text


def _http_get(url, timeout=40, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip",
                                              **(headers or {})})  # gzip: ~8-10x less to download
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
        if m["id"].startswith("~"):
            continue  # "~vendor/...-latest" are moving aliases, not models: pure noise
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
            "snap": {"Context": ctx},  # only big context changes are announced (see track_changes)
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


def designarena_registry():
    """model id -> (display name, organization). Reveals codenames once Design Arena does;
    organization is "?" for anonymous/stealth providers (triggers the codename alert)."""
    try:
        reg = json.loads(http_get("https://www.designarena.ai/api/registry", timeout=40))
    except Exception as e:
        print(f"  ! designarena registry: {e}", file=sys.stderr)
        return {}
    providers = reg.get("providers", {})
    out = {}
    for k, v in reg.get("models", {}).items():
        p = providers.get(v.get("provider") or "", {})
        org = p.get("displayName") or ""
        if org == "Anonymous" or "mystery" in p.get("logoName", ""):
            org = "?"
        out[k] = (v.get("displayName") or k, org)
    return out


def fetch_designarena():
    page = http_get("https://www.designarena.ai/leaderboard", timeout=60).replace('\\"', '"')
    reg = designarena_registry()
    items = {}
    # page embeds {"category":"allcategories"...,"modelStats":[{"model":..,"elo":..,"isNew":..}]}
    for m in re.finditer(r'\{"model":"([^"]+)","wins":(\d+),"losses":(\d+).*?"total":(\d+),"winRate":([\d.]+),'
                         r'"elo":([\d.]+).*?"isNew":(true|false)', page):
        name, wins, losses, total, wr, elo, is_new = m.groups()
        if name in items:
            continue
        display, org = reg.get(name, (name, ""))
        same = re.sub(r"[^a-z0-9]", "", display.lower()) == re.sub(r"[^a-z0-9]", "", name.lower())
        title = display if same else f"{display} (id: {name})"
        items[name] = {
            "key": name, "title": title, "url": "https://www.designarena.ai/leaderboard", "desc": "",
            "fields": [("Arena", "Design Arena"), ("Organization", org or guess_org(name)),
                       ("Elo", f"{float(elo):.0f}"),
                       ("Win rate", f"{wr}%"), ("Battles", f"{int(total):,}")],
            "color": 0x57F287, "label": "New Design Arena model",
        }
    return list(items.values()), len(items) >= 20


def _sitemap_urls(url, depth=0):
    root = ET.fromstring(http_get(url, timeout=60))
    locs = [e.text.strip() for e in root.iter() if e.tag.endswith("}loc") and e.text]
    if root.tag.endswith("sitemapindex") and depth < 1:
        urls = []
        # fetch the parts in parallel (OpenAI has ~60); if any part fails, the whole site fails
        # this run, otherwise that part's old pages would look "new" the next time it loads
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            for part in pool.map(lambda sub: _sitemap_urls(sub, depth + 1), locs[:60]):  # sanity cap
                urls += part
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
            if cfg.get("exclude") and re.search(cfg["exclude"], u):
                continue
            if any(path.startswith(p) or p.rstrip("/") == path for p in cfg["include"]) and path.strip("/"):
                items.append({
                    "key": u, "group": name, "title": path, "url": u, "desc": "",
                    "fields": [("Site", name)], "color": 0xFEE75C,
                    "label": f"New {name} page",
                })
    return items, good > 0


def norm_url(u):
    """Compare URLs loosely: no scheme/www/query/fragment/trailing slash, lowercase host."""
    p = urllib.parse.urlparse(u.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}"


# Small sitemaps checked every minute (same group names as SITEMAPS; shared memory, so no double posts)
FAST_SITEMAPS = {
    "anthropic": ["https://www.anthropic.com/sitemap.xml"],
    "openai": [f"https://openai.com/sitemap.xml/{part}/" for part in
               ("page", "release", "product", "research", "publication", "milestone", "company", "safety",
                "global-affairs")],
    "openai deployment safety": ["https://deploymentsafety.openai.com/sitemap.xml"],
    "claude.com": ["https://claude.com/sitemap.xml"],
}
# skip translated copies (claude.com/ja/..., /de/..., /pt-br/...)
FAST_SITEMAP_EXCLUDE = {"claude.com": r"claude\.com/[a-z]{2}(-[a-z]{2,4})?/"}


def _fast_sitemap_items():
    items, good = [], 0
    for site, urls in FAST_SITEMAPS.items():
        try:
            locs = []  # (url, section): each sitemap section is its own group, so a newly added
            for u in urls:  # section is remembered quietly instead of posting all its old pages
                root = ET.fromstring(http_get(u, timeout=40))
                section = u.rstrip("/").rsplit("/", 1)[-1]
                locs += [(e.text.strip(), section) for e in root.iter() if e.tag.endswith("}loc") and e.text]
        except Exception as e:
            print(f"  ! fast sitemap {site}: {e}", file=sys.stderr)
            continue
        good += 1
        for loc, section in locs:
            # deploymentsafety's sitemap lists localhost URLs; point them at the real site
            loc = re.sub(r"^https?://localhost:\d+", "https://deploymentsafety.openai.com", loc)
            if site in FAST_SITEMAP_EXCLUDE and re.search(FAST_SITEMAP_EXCLUDE[site], loc):
                continue
            path = urllib.parse.urlparse(loc).path
            # same path filter as the full sitemap check (OpenAI: /index/, /research/...; not /about/, /form/)
            if site in SITEMAPS and not any(path.startswith(p) for p in SITEMAPS[site]["include"]):
                continue
            if not path.strip("/"):
                continue
            items.append({"key": norm_url(loc), "group": f"{site} [{section}]", "site": site, "title": path,
                          "url": loc, "desc": "", "fields": [("Site", site)], "color": 0xFEE75C,
                          "label": f"New {site} page"})
    return items, good


def fetch_fastpages():
    items, good = _fast_sitemap_items()
    for site, url in FAST_PAGE_FEEDS.items():
        try:
            entries = _parse_feed(http_get(url, timeout=40))
        except Exception as e:
            print(f"  ! feed {site}: {e}", file=sys.stderr)
            continue
        if not entries:
            continue
        good += 1
        for e in entries:
            link = e["link"]
            if not link:
                continue
            items.append({
                "key": norm_url(link), "group": site, "title": e["title"] or link, "url": link,
                "desc": "", "fields": [("Site", site)], "color": 0xFEE75C, "label": f"New {site} page",
            })
    return items, good > 0


def fetch_news():
    items, good = [], 0
    for outlet, (url, filt) in NEWS_FEEDS.items():
        try:
            entries = _parse_feed(http_get(url, timeout=40))
        except Exception as e:
            print(f"  ! news {outlet}: {e}", file=sys.stderr)
            continue
        good += 1
        for e in entries:
            title = html.unescape(e["title"] or "").strip()
            if outlet == "Reuters":
                title = re.sub(r"\s+-\s+Reuters$", "", title)
            if filt and not AI_NEWS.search(title + " " + _clean(e["summary"], 300)):
                continue
            items.append({
                "key": norm_url(e["link"]) if e["link"] else e["id"], "group": outlet, "title": title[:250],
                "url": e["link"], "desc": _clean(e["summary"], 300), "fields": [("Source", outlet)],
                "color": 0x2F3136, "label": f"{outlet}",
                "icon": {"Reuters": "reuters.com", "Financial Times": "ft.com"}.get(outlet),
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
    # unauthenticated GitHub API = 60 requests/hour per IP (shared on hosts like Render);
    # any token = 5,000/hour. Public org repo lists need no special permission.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("RADAR_STATE_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
        certs = None
        for attempt in range(3):  # crt.sh often answers 502 / cuts the reply short; retry gently
            try:
                certs = json.loads(_http_get(f"https://crt.sh/?q=%25.{root}&output=json&exclude=expired", 120))
                break
            except Exception as e:
                err = e
                time.sleep(10 * (attempt + 1))
        if certs is None:
            print(f"  ! crt.sh {root}: {err} (crt.sh is busy; will retry next run)", file=sys.stderr)
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


def fetch_arcprize():
    base = "https://arcprize.org/media/data/"
    models = {m["id"]: m for m in json.loads(http_get(base + "models.json"))}
    datasets = {d["id"]: d.get("displayName") or d["id"] for d in json.loads(http_get(base + "datasets.json"))}
    items = []
    for e in json.loads(http_get(base + "evaluations.json")):
        if e.get("display") is False:
            continue
        m = models.get(e["modelId"], {})
        name = m.get("displayName") or e["modelId"]
        ds = datasets.get(e["datasetId"], e["datasetId"])
        score, cost = e.get("score"), e.get("costPerTask")
        items.append({
            "key": f"{e['datasetId']}::{e['modelId']}", "group": e["datasetId"],
            "title": f"{name} on {ds}", "url": "https://arcprize.org/leaderboard", "desc": "",
            "fields": [("Benchmark", ds), ("Score", f"{score * 100:.1f}%" if isinstance(score, (int, float)) else "n/a"),
                       ("Cost / task", f"${cost:g}" if isinstance(cost, (int, float)) else "n/a"),
                       ("Organization", m.get("providerId") or "?"), ("Released", (m.get("modelReleaseDate") or "n/a")[:10])],
            "color": 0x9B59B6, "label": "New ARC Prize result",
        })
    return items, len(items) > 100


def fetch_artificialanalysis():
    page = http_get("https://artificialanalysis.ai/leaderboards/models", timeout=90).replace('\\"', '"')
    items = {}
    for slug, name, _dep, released, _cslug, creator in re.findall(
            r'\{"slug":"([^"]+)","name":"([^"]+)","deprecated":(true|false),"releaseDate":"?([^",]*)"?,'
            r'"creator":\{"slug":"([^"]+)","name":"([^"]+)"', page):
        items.setdefault(slug, {
            "key": slug, "title": name, "url": f"https://artificialanalysis.ai/models/{slug}", "desc": "",
            "fields": [("Organization", creator), ("Released", released if released not in ("", "null") else "n/a")],
            "color": 0xE67E22, "label": "New model on Artificial Analysis",
        })
    return list(items.values()), len(items) > 100


def fetch_designarena_registry():
    """Every model Design Arena has added to battles (appears here before the leaderboard)."""
    reg = json.loads(http_get("https://www.designarena.ai/api/registry", timeout=40))
    providers = reg.get("providers", {})
    items = []
    for k, v in reg.get("models", {}).items():
        if v.get("active") is False:
            continue  # inactive = taken out of battles; reported as removed
        p = providers.get(v.get("provider") or "", {})
        org = p.get("displayName") or v.get("provider") or "?"
        if org == "Anonymous" or "mystery" in p.get("logoName", ""):
            org = "?"
        name = v.get("displayName") or k
        same = re.sub(r"[^a-z0-9]", "", name.lower()) == re.sub(r"[^a-z0-9]", "", k.lower())
        arenas = sorted({a for lst in (v.get("arenas") or {}).values() for a in (lst or [])})
        fields = [("Organization", org)]
        if v.get("inputModalities"):
            fields.append(("Input", ", ".join(v["inputModalities"])))
        if arenas:
            fields.append(("Arenas", ", ".join(arenas[:8]) + (" …" if len(arenas) > 8 else "")))
        items.append({
            "key": k, "title": name if same else f"{name} (id: {k})",
            "url": "https://www.designarena.ai/leaderboard", "desc": "", "fields": fields,
            "color": 0x57F287, "label": "New Design Arena model", "icon": "designarena.ai",
        })
    return items, len(items) > 100


# SDK files that list model IDs (new IDs often land here before launch)
SDK_MODEL_FILES = {
    "OpenAI SDK": "https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/shared/chat_model.py",
    "Anthropic SDK": "https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/src/anthropic/types/model_param.py",
}


def fetch_sdk_models():
    items, good = [], 0
    for group, url in SDK_MODEL_FILES.items():
        try:
            text = http_get(url)
        except Exception as e:
            print(f"  ! {group}: {e}", file=sys.stderr)
            continue
        ids = [x for x in re.findall(r'"([a-z0-9][a-z0-9.\-:/]{2,})"', text) if not x.startswith("__")]
        if len(ids) < 5:
            continue
        good += 1
        for mid in dict.fromkeys(ids):
            items.append({
                "key": f"{group}::{mid}", "group": group, "title": f"`{mid}`",
                "url": url.replace("raw.githubusercontent.com", "github.com").replace("/main/", "/blob/main/"),
                "desc": "", "fields": [], "color": 0xEB459E, "label": f"New {group} model ID",
                "icon": "openai.com" if "OpenAI" in group else "anthropic.com",
            })
    return items, good > 0


def fetch_litellm():
    d = json.loads(http_get(
        "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json", timeout=60))
    items = []
    for mid, v in d.items():
        if mid == "sample_spec" or not isinstance(v, dict):
            continue
        prov = v.get("litellm_provider") or "?"
        items.append({
            "key": mid, "group": "LiteLLM", "title": f"`{mid}` ({prov})",
            "url": "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json",
            "desc": "", "fields": [], "color": 0xEB459E, "label": "New LiteLLM model ID", "icon": "litellm.ai",
        })
    return items, len(items) > 500


# ---- desktop / CLI tools on npm: every release channel (latest, alpha, preview, nightly...)
NPM_APPS = {
    "@openai/codex": ("Codex CLI", "openai.com"),
    "@google/gemini-cli": ("Gemini CLI", "gemini.google.com"),
    "@anthropic-ai/claude-code": ("Claude Code", "claude.ai"),
    "@qwen-code/qwen-code": ("Qwen Code", "qwen.ai"),
    "@github/copilot": ("GitHub Copilot CLI", "github.com"),
    "opencode-ai": ("OpenCode", "opencode.ai"),
    "@sourcegraph/amp": ("Amp", "ampcode.com"),
    "@kilocode/cli": ("Kilo Code CLI", "kilocode.ai"),
    "@augmentcode/auggie": ("Auggie (Augment)", "augmentcode.com"),
    "@charmland/crush": ("Crush", "charm.sh"),
    "@factory/cli": ("Factory Droid", "factory.ai"),
    "@zed-industries/claude-code-acp": ("Zed Claude Code adapter", "zed.dev"),
}
NPM_CHANNELS = ("latest", "next", "preview", "alpha", "beta", "nightly", "rc", "canary", "stable", "insiders")


def fetch_desktop_apps():
    items, good = [], 0
    for pkg, (name, icon) in NPM_APPS.items():
        tags = None
        for reg in ("https://registry.npmjs.org", "https://registry.npmmirror.com"):  # mirror if npm is blocked
            try:
                tags = json.loads(http_get(f"{reg}/-/package/{pkg}/dist-tags", timeout=30))
                break
            except Exception as e:
                last_err = e
        if tags is None:
            print(f"  ! npm {pkg}: {last_err}", file=sys.stderr)
            continue
        good += 1
        for tag, version in tags.items():
            if tag not in NPM_CHANNELS:
                continue  # skip per-platform / one-off tags
            items.append({
                "key": f"{pkg}::{tag}", "snap": {"Version": version},
                "title": f"{name}" + ("" if tag == "latest" else f" ({tag})"),
                "url": f"https://www.npmjs.com/package/{pkg}?activeTab=versions", "desc": "",
                "fields": [("Package", f"`{pkg}`"), ("Tag", tag), ("Version", version)],
                "change_fields": [("NPM tag", tag), ("Package", f"`{pkg}`")],
                "change_label": f"New {name} update", "color": 0x5865F2, "label": f"New {name} channel",
                "icon": icon,
            })
    return items, good >= len(NPM_APPS) // 2


# ---- mobile apps: iOS (Apple's official lookup API, with release notes) and Android (Play page)
MOBILE_APPS = {  # name: (iOS App Store id, Android package, icon domain)
    "ChatGPT": (6448311069, "com.openai.chatgpt", "openai.com"),
    "Claude": (6473753684, "com.anthropic.claude", "claude.ai"),
    "Gemini": (6477489729, "com.google.android.apps.bard", "gemini.google.com"),
    "Grok": (6670324846, "ai.x.grok", "x.ai"),
    "Vibe by Mistral": (6740410176, "ai.mistral.chat", "mistral.ai"),
    "Perplexity": (1668000334, "ai.perplexity.app.android", "perplexity.ai"),
    "Microsoft Copilot": (541164041, None, "copilot.microsoft.com"),
    "DeepSeek": (6737597349, "com.deepseek.chat", "deepseek.com"),
    "Meta AI": (1558240027, "com.facebook.stella", "meta.ai"),
    "Kimi": (6474233312, "com.moonshot.kimichat", "kimi.com"),
}


def _app_item(name, platform, version, url, icon, notes=""):
    return {
        "key": f"{name}::{platform}", "snap": {"Version": version},
        "title": f"{name} ({platform})", "url": url, "desc": notes,
        "fields": [("Platform", platform), ("Version", version)],
        "change_fields": [("Platform", platform)],
        "change_label": f"New {name} update ({platform})", "color": 0x3BA55C,
        "label": f"{name} ({platform})", "icon": icon,
    }


def fetch_mobile_ios():
    ids = ",".join(str(v[0]) for v in MOBILE_APPS.values())
    res = json.loads(http_get(f"https://itunes.apple.com/lookup?id={ids}&country=us", timeout=40))["results"]
    by_id = {r["trackId"]: r for r in res}
    items = []
    for name, (ios_id, _a, icon) in MOBILE_APPS.items():
        r = by_id.get(ios_id)
        if r:
            notes = (r.get("releaseNotes") or "").strip()[:500]
            items.append(_app_item(name, "iOS", r["version"], r.get("trackViewUrl", ""), icon,
                                   f"**Release notes:** {notes}" if notes else ""))
    return items, len(items) >= len(MOBILE_APPS) // 2


def fetch_mobile_android():
    items = []
    for name, (_i, pkg, icon) in MOBILE_APPS.items():
        if not pkg:
            continue
        url = f"https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=US"
        try:
            v = re.findall(r'\[\[\["(\d+[\w.\-]*)"\]\]', http_get(url, timeout=40))
        except Exception as e:
            print(f"  ! play {pkg}: {e}", file=sys.stderr)
            continue
        if v:
            items.append(_app_item(name, "Android", v[0], url, icon))
    return items, len(items) >= 4


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
    "sitemaps": fetch_sitemaps,
    "releases": fetch_releases,
    "news": fetch_news,
    "fastpages": fetch_fastpages,
    **{f"api_{k}": make_openai_compat_fetcher(k) for k in OPENAI_COMPAT_APIS},
    "api_anthropic": fetch_api_anthropic,
    "api_gemini": fetch_api_gemini,
    "huggingface": fetch_huggingface,
    "newrepos": fetch_newrepos,
    "subdomains": fetch_subdomains,
    "gcp": fetch_gcp,
    **{k: make_feed_fetcher(k) for k in FEEDS},
    "changelogs": fetch_changelogs,
    "status": fetch_status,
    "desktop_apps": fetch_desktop_apps,
    "mobile_ios": fetch_mobile_ios,
    "mobile_android": fetch_mobile_android,
    "designarena_registry": fetch_designarena_registry,
    "sdk_models": fetch_sdk_models,
    "arcprize": fetch_arcprize,
    "artificialanalysis": fetch_artificialanalysis,
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
    return post_payload(webhook, payload)


def post_payload(webhook, payload):
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


# ---------------------------------------------------------------- leaderboard snapshots
# Each returns rows sorted best-first: [{"id", "name", "score"}]


def lb_artificialanalysis():
    page = http_get("https://artificialanalysis.ai/leaderboards/models", timeout=90).replace('\\"', '"')
    chunks = page.split('{"slug":"')[1:]
    # variant slug (e.g. claude-opus-5-5-xhigh) -> model slug; listed in a separate part of the page
    release = dict(re.findall(r'^([^"]+)","name":"[^"]*","releaseSlug":"([^"]+)"', "\n".join(chunks), re.M))
    rows = {}  # one row per model, keeping its best-scoring effort variant
    for chunk in chunks:
        m = re.match(r'([^"]+)","name":"([^"]+)"', chunk)
        score = re.search(r'"intelligenceIndex":([\d.]+),"intelligenceIndexIsEstimated":false', chunk[:3000])
        if not (m and score):
            continue
        model = release.get(m.group(1), m.group(1))
        row = {"id": model, "name": m.group(2), "score": float(score.group(1))}
        if model not in rows or row["score"] > rows[model]["score"]:
            rows[model] = row
    return sorted(rows.values(), key=lambda r: -r["score"])


def lb_arena(slug):
    def fetch():
        page = http_get(f"https://arena.ai/leaderboard/{slug}", timeout=60).replace('\\"', '"')
        first = page.split('"entries":[', 1)[1].split('"entries":[', 1)[0]  # overall board only
        rows = {}
        for rank, name, rating in re.findall(r'"rank":(\d+),.*?"modelDisplayName":"([^"]+)","rating":([\d.]+)', first):
            rows.setdefault(name, {"id": name, "name": name, "score": float(rating), "rank": int(rank)})
        return sorted(rows.values(), key=lambda r: (r["rank"], -r["score"]))
    return fetch


def lb_designarena():
    page = http_get("https://www.designarena.ai/leaderboard", timeout=60).replace('\\"', '"')
    main_board = page.split('"modelStats":[', 2)[1]  # 1st board = main "all categories" (2nd is fullstack)
    reg = designarena_registry()
    rows = {}
    for name, elo in re.findall(r'\{"model":"([^"]+)","wins".*?"elo":([\d.]+)', main_board):
        rows.setdefault(name, {"id": name, "name": reg.get(name, (name, ""))[0], "score": float(elo)})
    return sorted(rows.values(), key=lambda r: -r["score"])


def _board(bid, title, subtitle, url, rows, channel, color, fmt, icon=None):
    return {"id": bid, "title": title, "subtitle": subtitle, "url": url, "rows": rows,
            "channel": channel, "color": color, "fmt": fmt, "icon": icon}


def boards_epoch():
    """One top-20 board per Epoch AI benchmark (FrontierMath, GPQA, SWE-bench Verified, ...)."""
    import csv
    import io
    import zipfile
    req = urllib.request.Request("https://epoch.ai/data/benchmark_data.zip", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    nice = {"Frontiermath": "FrontierMath", "Gpqa": "GPQA", "Swe": "SWE", "Hle": "HLE", "Aime": "AIME",
            "Arc Agi": "ARC-AGI", "Otis": "OTIS", "Critpt": "CritPt", "Ale": "ALE", "Apex": "APEX",
            "Webdev": "WebDev", "Simpleqa": "SimpleQA", "Scicode": "SciCode", "Gdp": "GDP", "Pdf": "PDF"}
    skip_cols = {"Model version", "Release date", "Organization", "Country", "Provider", "Company", "id",
                 "Training compute (FLOP)", "Training compute notes", "Log viewer", "Logs", "Started at"}
    boards = []
    for fname in z.namelist():
        if not fname.endswith(".csv"):
            continue
        rows_raw = list(csv.DictReader(io.TextIOWrapper(z.open(fname), "utf-8", errors="replace")))
        if not rows_raw:
            continue
        cols = [c for c in rows_raw[0] if c not in skip_cols]
        # score = first column that is numeric for most rows (Epoch's columns differ per benchmark)
        def numeric(c):
            ok = 0
            for r in rows_raw:
                try:
                    float(r.get(c) or "x"); ok += 1
                except ValueError:
                    pass
            return ok >= 0.6 * len(rows_raw)
        prefer = ["Best score (across scorers)", "mean_score"]
        score_col = next((c for c in prefer if c in cols and numeric(c)), None) or \
            next((c for c in cols if numeric(c) and not re.search(r"err|ci|cost|token|std", c, re.I)), None)
        if not score_col:
            continue
        err_col = next((c for c in cols if re.search(r"stderr|standard error|95% ci \(±\)", c, re.I)), None)
        pct = all(0 <= float(r[score_col]) <= 1 for r in rows_raw if (r.get(score_col) or "").replace(".", "", 1).isdigit())
        best = {}
        for r in rows_raw:
            try:
                sc = float(r.get(score_col) or "x")
            except ValueError:
                continue
            model = r.get("Model version") or ""
            if not model:
                continue
            name = re.sub(r"_([a-z0-9\-]+)$", r" (\1)", model)
            err = r.get(err_col) if err_col else None
            if model not in best or sc > best[model]["score"]:
                best[model] = {"id": model, "name": name, "score": sc * (100 if pct else 1),
                               "err": float(err) * (100 if pct else 1) if err and err.replace(".", "", 1).isdigit() else None}
        rows = sorted(best.values(), key=lambda x: -x["score"])
        title = fname[:-4].replace("_external", "").replace("_", " ").title()
        for k, v in nice.items():
            title = title.replace(k, v)
        title = re.sub(r" V(\d)$", r" (v\1)", title)
        boards.append(_board(f"lb_epoch::{fname}", title, f"Epoch AI benchmark data ({score_col}).",
                             "https://epoch.ai/benchmarks", rows, "benchmarks", 0xF1C40F,
                             "{:.1f}%" if pct else "{:.1f}", icon="epoch.ai"))
    return boards


VALS_BENCHMARKS = ["vals_index", "terminal-bench-4", "vibe-code", "programbench", "srebench", "legal_bench",
                   "tax_agent_bench", "proof_bench", "ioi", "web_search", "cua_bench", "skillsbench",
                   "time_horizon_index", "rsi_index", "cyber", "code-migration", "medcode", "sage"]


def _astro(v):
    """Decode Astro's serialized props ([type, value] pairs)."""
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        t, x = v
        if t == 0:
            return {k: _astro(y) for k, y in x.items()} if isinstance(x, dict) else x
        if t == 1:
            return [_astro(y) for y in x]
        return x
    if isinstance(v, dict):
        return {k: _astro(y) for k, y in v.items()}
    return v


def _pretty_model(mid):
    name = mid.split("/", 1)[-1].replace("-", " ").replace("_", " ")
    name = re.sub(r"(\d) (\d)", r"\1.\2", name).title()
    for a, b in {"Gpt ": "GPT-", "Glm ": "GLM-", "Mimo": "MiMo", "Deepseek": "DeepSeek"}.items():
        name = name.replace(a, b)
    return name


def boards_vals():
    boards = []
    for slug in VALS_BENCHMARKS:
        url = f"https://www.vals.ai/benchmarks/{slug}"
        try:
            raw = http_get(url, timeout=60)
        except Exception as e:
            print(f"  ! vals {slug}: {e}", file=sys.stderr)
            continue
        page = html.unescape(raw)
        title = re.search(r"<title>(.*?) Leaderboard", page)
        title = title.group(1).strip() if title else slug
        rows = []
        table = re.search(r'<table id="[^"]*accessibility-table".*?</table>', page, re.S)
        if table:  # the Vals Index page has a plain table
            for _r, mslug, name, acc in re.findall(
                    r'<tr>\s*<td>(\d+)</td>\s*<th scope="row">\s*<a href="/models/([^"]+)">([^<]+)</a>\s*</th>\s*'
                    r'<td>([\d.]+)%</td>', table.group(0)):
                rows.append({"id": mslug, "name": name.strip(), "score": float(acc)})
            desc = "Vals AI composite index."
        else:     # other pages: overall task inside the benchmark component's props
            desc = "Vals AI benchmark."
            for m in re.finditer(r"<astro-island ([^>]*)>", raw):
                attrs = html.unescape(m.group(1))
                if "BenchmarkViewComponent" not in attrs:
                    continue
                props = {k: _astro(v) for k, v in json.loads(
                    html.unescape(re.search(r'props="([^"]*)"', m.group(1)).group(1))).items()}
                view = props.get("benchmarkView") or {}
                overall = ((view.get("default") or view).get("tasks") or {}).get("overall") or {}
                for mid, r in overall.items():
                    acc = (r or {}).get("accuracy")
                    if isinstance(acc, (int, float)) and acc > 0:
                        err = r.get("stderr")
                        rows.append({"id": mid, "name": _pretty_model(mid), "score": float(acc),
                                     "err": float(err) if isinstance(err, (int, float)) and err > 0 else None})
                desc = props.get("shortDescription") or desc
                break
        rows.sort(key=lambda r: -r["score"])
        boards.append(_board(f"lb_vals::{slug}", title, desc, url, rows, "benchmarks", 0x1F7A4D,
                             "{:.2f}%", icon="vals.ai"))
    return boards


def lb_cursorbench():
    page = http_get("https://cursor.com/cursorbench", timeout=60)
    rows = {}
    for name, score in re.findall(r'aria-label="([^":]+): ([\d.]+)%', page):
        name = html.unescape(name).strip()
        if name not in rows or float(score) > rows[name]["score"]:
            rows[name] = {"id": name, "name": name, "score": float(score)}
    return list(rows.values())


def _single(bid, title, subtitle, url, fetch, channel, color, fmt):
    return lambda: [_board(bid, title, subtitle, url, fetch(), channel, color, fmt)]


# board provider id -> function returning a list of boards
LEADERBOARDS = {
    "lb_aa_index": _single("lb_aa_index", "Artificial Analysis Intelligence",
                           "A comprehensive suite of evaluations across reasoning, knowledge, maths and programming.",
                           "https://artificialanalysis.ai/leaderboards/models", lb_artificialanalysis,
                           "benchmarks", 0xE67E22, "{:.1f}"),
    "lb_arena_text": _single("lb_arena_text", "Text Arena leaderboard", "Human-preference Elo from arena.ai (overall, text).",
                             "https://arena.ai/leaderboard/text", lb_arena("text"), "arenas", 0x57F287, "{:.0f}"),
    "lb_arena_vision": _single("lb_arena_vision", "Vision Arena leaderboard", "Human-preference Elo from arena.ai (overall, vision).",
                               "https://arena.ai/leaderboard/vision", lb_arena("vision"), "arenas", 0x57F287, "{:.0f}"),
    "lb_designarena_main": _single("lb_designarena_main", "Design Arena leaderboard", "Elo from designarena.ai (all categories).",
                                   "https://www.designarena.ai/leaderboard", lb_designarena, "arenas", 0x57F287, "{:.0f}"),
    "lb_cursorbench": _single("lb_cursorbench", "CursorBench", "Coding-agent performance on real Cursor sessions.",
                              "https://cursor.com/cursorbench", lb_cursorbench, "benchmarks", 0x111111, "{:.1f}%"),
    "lb_epoch": boards_epoch,
    "lb_vals": boards_vals,
}
LB_TOP, LB_MEMORY, LB_MIN_ROWS = 20, 50, 5  # show top 20; remember top 50 for arrows; skip tiny boards
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def render_board(b, prev):
    lines = []
    for i, r in enumerate(b["rows"][:LB_TOP], 1):
        if r["id"] not in prev:
            move = " 🆕 **NEW**"
        else:
            p = prev.index(r["id"]) + 1
            move = f" 🔼{p - i}" if p > i else (f" 🔽{i - p}" if p < i else "")
        score = b["fmt"].format(r["score"])
        if r.get("err") is not None:
            score += f" ±{b['fmt'].format(r['err'])}"
        lines.append(f"{MEDALS.get(i, f'{i}.')} {r['name']} [`{score}`]{move}")
    now = int(time.time())
    return {
        "embeds": [{
            "title": b["title"], "url": b["url"], "color": b["color"],
            "description": (f"{b['subtitle']}\n**Updated:** <t:{now}:R>\n\n" + "\n".join(lines))[:4000],
            "thumbnail": {"url": icon_url({"url": b["url"], "icon": b.get("icon")})},
            "footer": {"text": BRAND}, "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "allowed_mentions": {"parse": []},
    }


def run_board(b, state, dry_run):
    # equal scores used to swap places between fetches and re-post the board; sort ties by name
    b["rows"] = sorted(b["rows"], key=lambda r: (-r["score"], str(r["name"]).lower()))
    bid, rows = b["id"], b["rows"]
    if len(rows) < LB_MIN_ROWS:
        return
    key = f"{bid}#ranking2"  # new key: re-learn rankings quietly with the stable tie order
    top = min(LB_TOP, len(rows))
    ids = [r["id"] for r in rows[:LB_MEMORY]]
    prev = state.get(key)
    if prev is None:
        print(f"[{bid}] first run, remembering ranking")
        if not dry_run:
            state[key] = ids
        return
    if ids[:top] == prev[:top]:
        return
    print(f"[{bid}] top {top} changed")
    payload = render_board(b, prev)
    if dry_run:
        print(payload["embeds"][0]["description"][:600])
        return
    hook = webhook_for(b["channel"])
    if not hook:
        print(f"[{bid}] no webhook set, will post once it's added")
        return
    if post_payload(hook, payload):
        state[key] = ids
    time.sleep(1)


def run_leaderboard(lb_id, state, dry_run):
    try:
        boards = LEADERBOARDS[lb_id]()
    except Exception as e:
        print(f"[{lb_id}] failed: {e}", file=sys.stderr)
        return
    print(f"[{lb_id}] {len(boards)} board(s)")
    quiet = lb_id in CATCHUP
    CATCHUP.discard(lb_id)
    for b in boards:
        with STATE_LOCK:
            if quiet:  # catch-up: just remember the current ranking
                b["rows"] = sorted(b["rows"], key=lambda r: (-r["score"], str(r["name"]).lower()))
                if not dry_run and len(b["rows"]) >= LB_MIN_ROWS:
                    state[f"{b['id']}#ranking2"] = [r["id"] for r in b["rows"][:LB_MEMORY]]
                continue
            run_board(b, state, dry_run)


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


MIN_PRICE_CHANGE = 0.05  # ignore price moves smaller than 5%


def _small_price_move(field, a, b):
    if "price" not in field:
        return False
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return a > 0 and b > 0 and abs(b - a) / a < MIN_PRICE_CHANGE


MIN_CONTEXT_RATIO = 1.5  # announce context changes only when they grow/shrink 1.5x or more


def _small_context_move(field, a, b):
    if field != "Context" or not isinstance(a, int) or not isinstance(b, int) or a <= 0 or b <= 0:
        return False
    return max(a, b) / min(a, b) < MIN_CONTEXT_RATIO


def track_changes(name, items, old_snap):
    """Compare items' "snap" values with last run. Returns (change_items, current_snap)."""
    cur = {i["key"]: i["snap"] for i in items if "snap" in i}
    by_key = {i["key"]: i for i in items}
    changes = []
    for k, now in cur.items():
        before = {f: v for f, v in (old_snap.get(k) or {}).items() if f in now} or None  # ignore fields we stopped watching
        if before is None or before == now:
            continue
        diffs = [(f, before.get(f), now.get(f)) for f in now if before.get(f) != now.get(f)]
        small = [d for d in diffs if _small_price_move(*d) or _small_context_move(*d)]
        if small:
            # keep the last announced price, so many tiny moves still add up to an alert
            cur[k] = {**now, **{f: a for f, a, _ in small}}
            diffs = [d for d in diffs if d not in small]
        if not diffs:
            continue
        base = by_key[k]
        changes.append({
            "key": f"chg::{k}", "model": k, "title": base["title"], "url": base["url"],
            "desc": "\n".join(f"• **Old** {f}: **{_fmt_snap(f, a)}**\n• **New** {f}: **{_fmt_snap(f, b)}**"
                              for f, a, b in diffs),
            "fields": base.get("change_fields", [("Model ID", f"`{k}`")]), "color": 0xF39C12,
            "label": base.get("change_label", "Changed OpenRouter model"), "icon": base.get("icon"),
        })
    # removals: skip if the list suddenly shrank a lot (probably an API hiccup, not real removals)
    gone = [k for k in old_snap if k not in cur] if name == "openrouter" else []
    if gone and len(cur) >= 0.9 * len(old_snap):
        for k in gone:
            changes.append({
                "key": f"rm::{k}", "model": k, "title": old_snap[k].get("Name") or k,
                "url": f"https://openrouter.ai/{k}", "desc": "No longer listed on OpenRouter.",
                "fields": [("Model ID", f"`{k}`")], "color": 0x747F8D, "label": "Removed from OpenRouter",
            })
    return changes, cur


# ---------------------------------------------------------------- removals
# For these sources, items that disappear are announced (one message per group), like
# "Removed Arena.ai models". A removed item that comes back is announced as new again.
REMOVAL_SOURCES = {"arenas": "Arena.ai", "designarena_registry": "Design Arena"}
MAX_REMOVED_SHARE = 0.2  # more than 20% of a group vanishing at once = probably a broken fetch


def find_removals(name, items, seen):
    current = {i["key"] for i in items}
    loaded = {i.get("group") for i in items}
    by_group = {}
    for k in seen:
        g = k.split("::", 1)[0] if "::" in k else None
        if g not in loaded or k in current:
            continue
        by_group.setdefault(g, []).append(k)
    out = []
    for g, keys in by_group.items():
        in_group = sum(1 for k in seen if (k.split("::", 1)[0] if "::" in k else None) == g)
        if len(keys) > max(5, MAX_REMOVED_SHARE * in_group):
            print(f"[{name}] {len(keys)} removals in {g} looks like a broken fetch, ignoring")
            continue
        where = REMOVAL_SOURCES[name] + (f" {ARENAS.get(g, g)}" if g else "")
        out.append({
            "key": f"removed::{g}", "removes": keys, "title": f"Removed {where} models",
            "url": f"https://arena.ai/leaderboard/{g}" if name == "arenas" else "https://www.designarena.ai/leaderboard",
            "desc": "\n".join(f"🗑️ {k.split('::', 1)[-1]}" for k in keys[:40]), "fields": [],
            "color": 0xED4245, "label": f"Removed {REMOVAL_SOURCES[name]} models",
            "icon": "arena.ai" if name == "arenas" else "designarena.ai",
        })
    return out


# ---------------------------------------------------------------- batching
# For these sources, all new items from the same site/domain in one run go into ONE message.
BATCH_SOURCES = {"sitemaps": "pages", "fastpages": "pages", "subdomains": "subdomains",
                 "sdk_models": "model IDs", "litellm": "model IDs"}


def batch_items(name, items):
    groups = {}
    for i in items:
        groups.setdefault(i.get("group") or name, []).append(i)
    batches = []
    for group, members in groups.items():
        first = members[0]
        noun = BATCH_SOURCES[name]
        label_noun = noun[:-1] if len(members) == 1 and noun.endswith("s") else noun
        lines, used = [], 0
        for m in members:
            line = f"• {m['url'] if noun == 'pages' else m['title']}"
            if used + len(line) > 3300:
                lines.append(f"…and {len(members) - len(lines)} more")
                break
            lines.append(line)
            used += len(line) + 1
        first = members[0]
        batches.append({
            "key": f"batch::{group}", "members": [m["key"] for m in members],
            "title": f"New {first.get('site', group)} {label_noun}",
            "url": first["url"], "icon": first.get("icon"), "desc": "\n".join(lines),
            "fields": [("Count", str(len(members)))] if len(members) > 1 else [],
            "color": first["color"], "label": first["label"],
        })
    return batches


# ---------------------------------------------------------------- schedule
# The workflow fires every 5 min. Each source runs on its own interval, decided from the
# clock (no extra state, so no commit every 5 min). If GitHub skips a slot, slower sources
# just catch up on their next slot; they change slowly anyway.
FAST_MIN, MEDIUM_MIN, SLOW_MIN = 5, 15, 30
SCHEDULE_MIN = {
    # every 5 min: light and where being first matters
    "openrouter": FAST_MIN, "releases": FAST_MIN, "cursor": FAST_MIN, "fastpages": FAST_MIN,
    "sdk_models": FAST_MIN, "designarena_registry": FAST_MIN, "news": FAST_MIN,
    "desktop_apps": FAST_MIN, "mobile_ios": FAST_MIN,
    "bedrock": FAST_MIN, "azure": FAST_MIN, "gcp": FAST_MIN,
    **{k: FAST_MIN for k in SOURCES if k.startswith("api_")},
    # every 15 min: many requests per run
    "sitemaps": MEDIUM_MIN, "huggingface": MEDIUM_MIN, "newrepos": MEDIUM_MIN,
    "changelogs": MEDIUM_MIN, "mobile_android": MEDIUM_MIN, "designarena": MEDIUM_MIN, "litellm": MEDIUM_MIN,
    # every 30 min: heavy downloads or slow/fragile services
    "arenas": SLOW_MIN, "benchmarks": SLOW_MIN, "subdomains": SLOW_MIN, "status": SLOW_MIN,
    "arcprize": SLOW_MIN, "artificialanalysis": SLOW_MIN,
}


def due_sources(now=None):
    slot = int((now or time.time()) // (FAST_MIN * 60))  # which 5-min slot we're in
    return [k for k in list(SOURCES) + list(LEADERBOARDS)
            if slot % (SCHEDULE_MIN.get(k, SLOW_MIN) // FAST_MIN) == 0]


# ---------------------------------------------------------------- main


def run_source(name, state, args):
    if name in LEADERBOARDS:
        run_leaderboard(name, state, args.dry_run)
        return
    print(f"[{name}] fetching...")
    try:
        items, ok = SOURCES[name]()
    except Exception as e:
        print(f"[{name}] failed: {e}", file=sys.stderr)
        return
    if not ok:
        print(f"[{name}] skipped (no data or no API key), keeping old state")
        return

    with STATE_LOCK:
        _process(name, items, state, args)


def _process(name, items, state, args):
    seen = set(state.get(name, []))
    first_run = name not in state
    new = [i for i in items if i["key"] not in seen]
    # the sitemap check and the fast RSS check share pages: never post one twice
    if name in ("sitemaps", "fastpages"):
        other = state.get("fastpages" if name == "sitemaps" else "sitemaps", [])
        already = {norm_url(k) if name == "fastpages" else k for k in other}
        new = [i for i in new if (i["key"] if name == "fastpages" else norm_url(i["key"])) not in already]
    # Drop duplicate keys inside a single fetch
    new = list({i["key"]: i for i in new}.values())

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

    quiet = name in CATCHUP
    CATCHUP.discard(name)
    if quiet:
        print(f"[{name}] catching up quietly")
        first_run = True  # remember everything current, post nothing
    announce = new if (not first_run or args.announce_first) else []
    removals = find_removals(name, items, seen) if (name in REMOVAL_SOURCES and not first_run) else []
    if removals:
        print(f"[{name}] {sum(len(r['removes']) for r in removals)} removed")
    skey = f"{name}#snap"
    changes, cur_snap = [], None
    if any("snap" in i for i in items):
        if skey in state and not quiet:
            changes, cur_snap = track_changes(name, items, state[skey])
            announce = announce + changes
        else:
            cur_snap = {i["key"]: i["snap"] for i in items if "snap" in i}  # first time: baseline
        if changes:
            print(f"[{name}] {len(changes)} changed/removed")
    hook = webhook_for(name)
    posted, gone = set(), set()
    if (announce or removals) and args.dry_run:
        for i in (announce + removals)[:MAX_POSTS_PER_SOURCE]:
            print(f"   would post: {i['label']}: {i['title']} {i['url']}")
        if len(announce) > MAX_POSTS_PER_SOURCE:
            print(f"   ...and {len(announce) - MAX_POSTS_PER_SOURCE} more")
    elif (announce or removals) and not hook:
        print(f"[{name}] no webhook set ({WEBHOOK_ENV[name]}), will post once it's added")
        return
    elif announce or removals:
        if name in BATCH_SOURCES:
            announce = batch_items(name, announce)
        for i in announce[:MAX_POSTS_PER_SOURCE]:
            if post_embed(hook, i, os.environ.get(ROLE_ENV[name])):
                posted.update(i.get("members", [i["key"]]))
            time.sleep(1)
        skipped = announce[MAX_POSTS_PER_SOURCE:]
        if skipped:  # too many at once: mention it, then treat them as seen
            post_embed(hook, {
                "title": f"{len(skipped)} more new items not shown", "url": skipped[0]["url"],
                "desc": "\n".join(f"• {s['title']}" for s in skipped[:15])[:1500],
                "fields": [], "color": 0x99AAB5, "label": name,
            })
            for sk in skipped:
                posted.update(sk.get("members", [sk["key"]]))
        for r in removals:
            if post_embed(hook, r, os.environ.get(ROLE_ENV[name])):
                gone.update(r["removes"])
            time.sleep(1)

    if not args.dry_run:
        # First run (without --announce-first): remember everything so we don't spam.
        # Otherwise remember only what was posted; failed posts stay "new" and retry next run.
        keep = {i["key"] for i in items} if (first_run and not args.announce_first) else posted
        state[name] = sorted((seen | keep | fresh) - gone)
        if present:
            state[gkey] = sorted(known_groups | present)
        if cur_snap is not None:
            # keep the old values for changes that failed to post, so they retry next run
            snap = dict(cur_snap)
            for c in changes:
                if c["key"] not in posted:
                    snap[c["model"]] = state[skey][c["model"]]
            state[skey] = snap


# ---------------------------------------------------------------- remote state (Render etc.)
# Hosts like Render's free tier wipe the disk on every restart. If RADAR_STATE_TOKEN is set, the
# bot keeps its memory in the GitHub repo instead (gzipped, so it stays small) and saves it
# every few minutes and on shutdown.
STATE_REPO = os.environ.get("RADAR_STATE_REPO", "AdhikowshikJ/ai-radar")
# Memory lives on its own branch: hosts like Render redeploy on every commit to main, so saving
# there would restart the bot after every save.
STATE_BRANCH = os.environ.get("RADAR_STATE_BRANCH", "radar-state")
STATE_REMOTE_PATH = "state/remote-seen.json.gz"
STATE_TOKEN = os.environ.get("RADAR_STATE_TOKEN")
REMOTE_SAVE_SECONDS = int(os.environ.get("RADAR_SAVE_SECONDS", "300"))
_remote = {"sha": None, "hash": None, "saved_at": 0}
# If the loaded memory is older than this, each source's first check after startup only
# updates the memory (no posts), so a long gap never turns into a flood of old news.
STALE_AFTER_SECONDS = int(os.environ.get("RADAR_STALE_HOURS", "6")) * 3600
CATCHUP = set()


def _gh(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{STATE_REPO}/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {STATE_TOKEN}", "Accept": "application/vnd.github+json",
                 "User-Agent": UA, "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def _state_digest(state):
    import hashlib
    data = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    return data, hashlib.sha256(data).hexdigest()


def _ensure_state_branch():
    try:
        _gh("GET", f"git/ref/heads/{STATE_BRANCH}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        main_sha = _gh("GET", "git/ref/heads/main")["object"]["sha"]
        _gh("POST", "git/refs", {"ref": f"refs/heads/{STATE_BRANCH}", "sha": main_sha})
        print(f"created branch {STATE_BRANCH} for the bot's memory")


def load_remote_state():
    import base64
    import gzip
    _ensure_state_branch()
    for ref in (STATE_BRANCH, "main"):  # main = where an older version saved it
        try:
            meta = _gh("GET", f"contents/{STATE_REMOTE_PATH}?ref={ref}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            continue
        state = json.loads(gzip.decompress(base64.b64decode(meta["content"])))
        if ref == STATE_BRANCH:
            _remote["sha"] = meta["sha"]
            _remote["hash"] = _state_digest({k: v for k, v in state.items() if k != "_saved_at"})[1]
        age = time.time() - float(state.get("_saved_at") or 0)
        if age > STALE_AFTER_SECONDS:
            CATCHUP.update(list(SOURCES) + list(LEADERBOARDS))
            print(f"memory is {age / 3600:.0f}h old: catching up quietly (first check of each source won't post)")
        print(f"loaded remote state from {ref} ({len(state)} keys)")
        return state
    raw = http_get(f"https://raw.githubusercontent.com/{STATE_REPO}/main/state/seen.json", timeout=90)
    print("no remote state yet, starting from state/seen.json")
    return json.loads(raw)


def save_remote_state(state, force=False):
    import base64
    import gzip
    with STATE_LOCK:
        data, digest = _state_digest({k: v for k, v in state.items() if k != "_saved_at"})
        if digest != _remote["hash"]:
            state["_saved_at"] = time.time()
            data = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    if digest == _remote["hash"] or (not force and time.time() - _remote["saved_at"] < REMOTE_SAVE_SECONDS):
        return
    body = {"message": "radar: save state", "branch": STATE_BRANCH,
            "content": base64.b64encode(gzip.compress(data, 9, mtime=0)).decode()}
    if _remote["sha"]:
        body["sha"] = _remote["sha"]
    try:
        try:
            _remote["sha"] = _gh("PUT", f"contents/{STATE_REMOTE_PATH}", body)["content"]["sha"]
        except urllib.error.HTTPError as e:
            if e.code not in (409, 422):
                raise
            # file changed since we read it (e.g. the old instance saved during a redeploy):
            # fetch its current version id and overwrite with our newer memory
            body["sha"] = _gh("GET", f"contents/{STATE_REMOTE_PATH}?ref={STATE_BRANCH}")["sha"]
            _remote["sha"] = _gh("PUT", f"contents/{STATE_REMOTE_PATH}", body)["content"]["sha"]
        _remote["hash"], _remote["saved_at"] = digest, time.time()
        print("saved remote state")
    except Exception as e:
        _remote["saved_at"] = time.time()  # back off instead of retrying after every source
        hint = " (token needs Contents: Read and write on the repo)" if "403" in str(e) or "401" in str(e) else ""
        print(f"  ! remote state save failed: {e}{hint}", file=sys.stderr)


def save_state(state):
    with STATE_LOCK:
        _save_state(state)


def _save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0))
    tmp.replace(STATE_FILE)  # atomic, so a crash never leaves half a file


# Loop mode (for an always-on server): seconds between checks per speed tier
LOOP_SECONDS = {
    FAST_MIN: int(os.environ.get("RADAR_LOOP_FAST", "60")),
    MEDIUM_MIN: int(os.environ.get("RADAR_LOOP_MEDIUM", "300")),
    SLOW_MIN: int(os.environ.get("RADAR_LOOP_SLOW", "300")),
}


# Per-source loop intervals (seconds) that differ from their tier, and which lane runs them.
# Lanes run in parallel, so arena/AA checks every minute never delay the API checks.
ARENA_LANE = ["arenas", "artificialanalysis", "lb_arena_text", "lb_arena_vision", "lb_aa_index"]
LOOP_OVERRIDE_SECONDS = {
    **{k: int(os.environ.get("RADAR_LOOP_ARENA", "60")) for k in ARENA_LANE},
    "sitemaps": int(os.environ.get("RADAR_LOOP_SITEMAPS", "180")),
    "subdomains": int(os.environ.get("RADAR_LOOP_SUBDOMAINS", "1800")),  # crt.sh is fragile: be gentle
}


def loop_interval(name):
    return LOOP_OVERRIDE_SECONDS.get(name, LOOP_SECONDS[SCHEDULE_MIN.get(name, SLOW_MIN)])


def _lane(lane, names, state, args):
    last = {}
    while True:
        now = time.time()
        due = [k for k in names if now - last.get(k, 0) >= loop_interval(k)]
        _get_cache().clear()
        for name in due:
            last[name] = time.time()
            try:
                run_source(name, state, args)
            except Exception as e:  # never let one source kill the loop
                print(f"[{name}] crashed: {e}", file=sys.stderr)
            if not args.dry_run:
                save_state(state)
                if STATE_TOKEN:
                    save_remote_state(state)  # throttled to once per RADAR_SAVE_SECONDS
        time.sleep(5)


def run_loop(state, args):
    """Three lanes in parallel: fast (APIs, SDKs, ...), arena (arena.ai + Artificial Analysis),
    heavy (sitemaps every 3 min; Epoch, Vals, ARC, changelogs ... every 5 min)."""
    everything = list(SOURCES) + list(LEADERBOARDS)
    arena = [k for k in ARENA_LANE if k in everything]
    fast = [k for k in everything if SCHEDULE_MIN.get(k, SLOW_MIN) == FAST_MIN and k not in arena]
    heavy = [k for k in everything if k not in fast and k not in arena]
    for lane, names in (("fast", fast), ("arena", arena), ("heavy", heavy)):
        print(f"lane {lane}: " + ", ".join(f"{k}@{loop_interval(k)}s" for k in names))
    for lane, names in (("arena", arena), ("heavy", heavy)):
        threading.Thread(target=_lane, args=(lane, names, state, args), daemon=True).start()
    _lane("fast", fast, state, args)


def serve(state, args):
    """Web-service mode (Render free tier): answer pings on $PORT and run the loop."""
    import http.server
    import signal
    import threading

    class Ping(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"AI Leaks radar is running\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=http.server.ThreadingHTTPServer(("0.0.0.0", port), Ping).serve_forever,
                     daemon=True).start()
    print(f"listening on port {port}")

    def shutdown(*_):  # Render sends SIGTERM before restarting: save memory first
        print("shutting down, saving state")
        if STATE_TOKEN and not args.dry_run:
            save_remote_state(state, force=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    run_loop(state, args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only; don't post or save")
    ap.add_argument("--only", help="comma-separated: " + ",".join(list(SOURCES) + list(LEADERBOARDS)))
    ap.add_argument("--all", action="store_true", help="ignore the 5/15/30 min schedule, run every source")
    ap.add_argument("--loop", action="store_true", help="run forever (always-on server); fast sources every 60s")
    ap.add_argument("--serve", action="store_true", help="web-service mode for Render: --loop plus a ping page")
    ap.add_argument("--announce-first", action="store_true",
                    help="on a source's first run, post everything instead of just remembering it")
    args = ap.parse_args()

    if STATE_TOKEN:
        state = load_remote_state()
    else:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if args.serve:
        serve(state, args)
        return
    if args.loop:
        run_loop(state, args)
        return
    if args.only:
        wanted = args.only.split(",")
    elif args.all or os.environ.get("RADAR_MODE") == "all":
        wanted = list(SOURCES) + list(LEADERBOARDS)  # manual "Run workflow" checks everything
    else:
        wanted = due_sources()
    print(f"sources this run: {', '.join(wanted)}")
    for name in wanted:
        run_source(name, state, args)
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
