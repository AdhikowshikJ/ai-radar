#!/usr/bin/env python3
"""AI Radar: watches OpenRouter, Arena leaderboards, sitemaps and GitHub releases,
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
}

RELEASE_REPOS = [
    "openai/codex",
    "openai/openai-python",
    "google-gemini/gemini-cli",
    "googleapis/python-genai",
    "anthropics/claude-code",
    "anthropics/anthropic-sdk-python",
]

# Optional: role to @mention per source (set the role ID, e.g. DISCORD_ROLE_ARENAS=1234567890)
ROLE_ENV = {
    "openrouter": "DISCORD_ROLE_OPENROUTER",
    "arenas": "DISCORD_ROLE_ARENAS",
    "designarena": "DISCORD_ROLE_DESIGNARENA",
    "sitemaps": "DISCORD_ROLE_PAGES",
    "releases": "DISCORD_ROLE_RELEASES",
}
BRAND = os.environ.get("RADAR_BRAND", "AI Radar • Study with US")

# source id -> env var holding that channel's webhook (falls back to DISCORD_WEBHOOK_URL)
WEBHOOK_ENV = {
    "openrouter": "DISCORD_WEBHOOK_OPENROUTER",
    "arenas": "DISCORD_WEBHOOK_ARENAS",
    "designarena": "DISCORD_WEBHOOK_ARENAS",
    "sitemaps": "DISCORD_WEBHOOK_PAGES",
    "releases": "DISCORD_WEBHOOK_RELEASES",
}


def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


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
                "key": f"{slug}::{name}",
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
        if len(urls) < 20:
            print(f"  ! sitemap {name}: only {len(urls)} urls, skipping", file=sys.stderr)
            continue
        good += 1
        for u in urls:
            path = "/" + u.split("//", 1)[-1].split("/", 1)[-1]
            if any(path.startswith(p) or p.rstrip("/") == path for p in cfg["include"]) and path.strip("/"):
                items.append({
                    "key": u, "title": path, "url": u, "desc": "",
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
                "key": e.findtext("a:id", "", ns),
                "title": f"{repo} {e.findtext('a:title', '', ns)}",
                "url": link.get("href") if link is not None else f"https://github.com/{repo}/releases",
                "desc": re.sub(r"\s+", " ", body).strip()[:300],
                "fields": [("Repo", repo)], "color": 0x5865F2,
                "label": "New release",
            })
    return items, good > 0


SOURCES = {
    "openrouter": fetch_openrouter,
    "arenas": fetch_arenas,
    "designarena": fetch_designarena,
    "sitemaps": fetch_sitemaps,
    "releases": fetch_releases,
}

# ---------------------------------------------------------------- discord


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
    return os.environ.get(WEBHOOK_ENV[source]) or os.environ.get("DISCORD_WEBHOOK_URL")


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only; don't post or save")
    ap.add_argument("--only", help="comma-separated: " + ",".join(SOURCES))
    ap.add_argument("--announce-first", action="store_true",
                    help="on a source's first run, post everything instead of just remembering it")
    args = ap.parse_args()

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    wanted = args.only.split(",") if args.only else list(SOURCES)

    for name in wanted:
        print(f"[{name}] fetching...")
        try:
            items, ok = SOURCES[name]()
        except Exception as e:
            print(f"[{name}] failed: {e}", file=sys.stderr)
            continue
        if not ok:
            print(f"[{name}] looks broken, keeping old state")
            continue

        seen = set(state.get(name, []))
        first_run = name not in state
        new = [i for i in items if i["key"] not in seen]
        # Drop duplicate keys inside a single fetch
        uniq = {i["key"]: i for i in new}
        new = list(uniq.values())
        print(f"[{name}] {len(items)} items, {len(new)} new" + (" (first run)" if first_run else ""))

        announce = new if (not first_run or args.announce_first) else []
        hook = webhook_for(name)
        posted = set()
        if announce and args.dry_run:
            for i in announce[:MAX_POSTS_PER_SOURCE]:
                print(f"   would post: {i['label']}: {i['title']} {i['url']}")
            if len(announce) > MAX_POSTS_PER_SOURCE:
                print(f"   ...and {len(announce) - MAX_POSTS_PER_SOURCE} more")
        elif announce and not hook:
            print(f"[{name}] no webhook set ({WEBHOOK_ENV[name]}), not marking as seen")
            continue
        elif announce:
            for i in announce[:MAX_POSTS_PER_SOURCE]:
                if post_embed(hook, i, os.environ.get(ROLE_ENV[name])):
                    posted.add(i["key"])
                time.sleep(1)
            skipped = announce[MAX_POSTS_PER_SOURCE:]
            if skipped:  # too many at once: mention it, then treat them as seen
                post_embed(hook, {
                    "title": f"{len(skipped)} more new items not shown", "url": "https://arena.ai",
                    "desc": "\n".join(f"• {s['title']}" for s in skipped[:15])[:1500],
                    "fields": [], "color": 0x99AAB5, "label": name,
                })
                posted.update(s["key"] for s in skipped)

        if not args.dry_run:
            # First run (without --announce-first): remember everything so we don't spam.
            # Otherwise remember only what was posted; failed posts stay "new" and retry next run.
            keep = {i["key"] for i in items} if (first_run and not args.announce_first) else posted
            state[name] = sorted(seen | keep)

    if not args.dry_run:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=0))


if __name__ == "__main__":
    main()
