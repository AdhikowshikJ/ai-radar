# AI Radar 📡

Free bot that posts to Discord when something new shows up:

| Channel (suggested) | Source | Secret name |
|---|---|---|
| `#openrouter` | New, changed (context / price / name) and removed models on OpenRouter | `DISCORD_WEBHOOK_OPENROUTER` |
| `#arenas` | New models on arena.ai leaderboards (text, vision, image, video, search, document) **and Design Arena** | `DISCORD_WEBHOOK_ARENAS` |
| `#subpages` | New pages on openai.com / anthropic.com (from their sitemaps) | `DISCORD_WEBHOOK_PAGES` |
| `#repo-releases` | Releases of Codex, Gemini CLI, Claude Code, SDKs | `DISCORD_WEBHOOK_RELEASES` |
| `#api-models` | New model IDs live in the OpenAI / Anthropic / Gemini APIs (needs API keys, see below) | `DISCORD_WEBHOOK_API_MODELS` |
| `#huggingface` | New models uploaded by big labs (DeepSeek, Qwen, Meta, Mistral, Google, OpenAI…) | `DISCORD_WEBHOOK_HUGGINGFACE` |
| `#subdomains` | New subdomains of openai.com, anthropic.com, claude.ai, chatgpt.com (certificate logs via crt.sh) | `DISCORD_WEBHOOK_SUBDOMAINS` |
| `#new-repos` | New public GitHub repos from AI labs | `DISCORD_WEBHOOK_NEWREPOS` |
| `#cloud` | AWS Bedrock (new models on AWS), Azure AI Foundry updates, Google Cloud Gemini/Vertex release notes | `DISCORD_WEBHOOK_CLOUD` (older `DISCORD_WEBHOOK_GOOGLE_CLOUD` still works for Google) |
| `#tools` | Cursor changelog, Codex changelog, Antigravity changelog, Gemini API changelog | `DISCORD_WEBHOOK_TOOLS` |
| `#benchmarks` | New model results on 80+ benchmarks from Epoch AI (GPQA, SWE-bench Verified, FrontierMath…) | `DISCORD_WEBHOOK_BENCHMARKS` |
| `#status` | New components on the OpenAI/Anthropic status pages (often = new product) | `DISCORD_WEBHOOK_STATUS` (falls back to `#subdomains`) |

**Leaderboard snapshots** (top 20 with 🥇🥈🥉, scores, 🔼/🔽 moves and 🆕 NEW), posted only when the top-20 order changes:
Artificial Analysis Intelligence Index → `#benchmarks`; arena.ai Text, arena.ai Vision and Design Arena → `#arenas`.

`#subpages` also watches cursor.com, antigravity.google, deepseek.com, z.ai, mistral.ai, minimax.io, blog.google (English), deepmind.google, ai.google.dev, x.ai and microsoft.ai.

Want everything in ONE channel? Just add a single secret `DISCORD_WEBHOOK_URL`. It's used for any source without its own webhook.

## Setup (about 15 minutes)

### 1. Make webhooks in Discord
For each channel: **Channel settings (gear) → Integrations → Webhooks → New Webhook → Copy Webhook URL**.
Treat these URLs like passwords. Anyone with one can post in your channel. Never put them in the code or in chat.

### 2. Put this folder on GitHub
1. Create a new repo on github.com (public = free unlimited Actions minutes; private also works but has a monthly limit).
2. Upload the contents of this `ai-radar` folder as the repo root, including the hidden `.github` folder. Easiest: install GitHub Desktop, or run in this folder:
   ```
   git init && git add . && git commit -m "ai radar"
   git branch -M main
   git remote add origin https://github.com/YOU/ai-radar.git
   git push -u origin main
   ```

### 3. Add the webhook URLs as secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add one secret per channel using the names in the table above (name = secret name, value = webhook URL).

### 4. Turn it on
Repo → **Actions** tab → enable workflows → **AI Radar** → **Run workflow**.

- The **first run is silent**. It only memorises what already exists so your channels don't get flooded with 3,000 old items.
- From the second run on, you only get NEW things.

## How often each tracker checks
cron-job.org triggers the workflow every 5 minutes (GitHub's own schedule is only a 30-min backup, because it often runs late or skips):
- **Every 5 min:** OpenRouter, all model APIs, GitHub releases, Cursor, Bedrock, Azure, Google Cloud
- **Every 15 min:** sitemaps, Hugging Face, new GitHub repos, changelogs, Design Arena
- **Every 30 min:** arena.ai, Epoch benchmarks, subdomains, status pages

Pressing **Run workflow** manually checks everything at once. Change the speeds in `SCHEDULE_MIN` in `radar.py`.

## API keys for `#api-models` (optional, free to create)
Listing models doesn't generate tokens, but check each provider's current terms. Add any you have as secrets; missing ones are simply skipped:
- `OPENAI_API_KEY` from platform.openai.com → API keys
- `ANTHROPIC_API_KEY` from console.anthropic.com → API keys
- `GEMINI_API_KEY` from aistudio.google.com → Get API key
- `XAI_API_KEY` (console.x.ai), `DEEPSEEK_API_KEY` (platform.deepseek.com), `MISTRAL_API_KEY` (console.mistral.ai)
- `MOONSHOT_API_KEY` (platform.moonshot.ai, Kimi), `MINIMAX_API_KEY` (platform.minimax.io)
- `DASHSCOPE_API_KEY` (Alibaba Model Studio international, Qwen), `ZAI_API_KEY` (z.ai, GLM)

Tip: use a separate key for the radar so you can revoke it anytime.

## Optional: role pings (like zAI's "@Design Arena")
1. Discord → Server Settings → Roles → create roles like `Arena Alerts`, `Design Arena`, `OpenRouter`.
2. Turn on Developer Mode (User Settings → Advanced), right-click the role → **Copy Role ID**.
3. Add a GitHub secret with that ID: `DISCORD_ROLE_ARENAS`, `DISCORD_ROLE_DESIGNARENA`, `DISCORD_ROLE_OPENROUTER`, `DISCORD_ROLE_PAGES` or `DISCORD_ROLE_RELEASES`.
4. Give yourselves the roles you want pings for (or let people self-assign with Carl-bot reaction roles).

Posts show a live "Discovered: … (x minutes ago)" timestamp. Models with an **unknown organization turn red** and are flagged as a possible codename/stealth model. Those are the ones worth tweeting early.

## Test on your own laptop first (optional)
```
python3 radar.py --dry-run                 # shows what it would post; posts nothing
export DISCORD_WEBHOOK_URL="paste-url"     # one test channel
python3 radar.py --announce-first --only releases   # posts the current releases so you can see how they look
```
(Don't commit `state/seen.json` from a test run, or delete it before pushing.)

## Customising
Everything is at the top of `radar.py`:
- `ARENAS`: which arena.ai leaderboards to watch
- `SITEMAPS`: sites and which URL paths count (`include`)
- `RELEASE_REPOS`: GitHub repos to follow
- `MAX_POSTS_PER_SOURCE`: flood protection (extra items get summarised in one message)

## Known limits
- arena.ai has no official API, so the Arena part reads the page's embedded data. If they redesign the site it can break. The bot notices when parsing fails and skips (with a warning in the Actions log) rather than spamming.
- Benchmark data © Epoch AI, CC BY 4.0 (credited in posts via the link to epoch.ai/benchmarks).
- The Chinese and xAI API endpoints were checked to exist (they answer "unauthorized" without a key) but haven't been tested with real keys.
- crt.sh (subdomains) is often slow or down; the bot just skips it and tries again next run.
- New orgs/domains/feeds you add are remembered silently the first time, so they never flood a channel.
- The `agent` and `webdev` arenas aren't tracked yet because their pages load data differently.
- OpenAI/Anthropic pages appear in sitemaps when published, sometimes just before an announcement.
- GitHub pauses scheduled workflows after 60 days of no repo activity. The state commits count as activity, so this normally won't happen.

## Reliable 5-minute trigger with cron-job.org (free)
1. GitHub → profile picture → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
   - Repository access: **Only select repositories → ai-radar**
   - Permissions → Repository permissions → **Actions: Read and write** (nothing else)
2. cron-job.org → **Create cronjob**
   - URL: `https://api.github.com/repos/AdhikowshikJ/ai-radar/actions/workflows/radar.yml/dispatches`
   - Schedule: every 5 minutes
   - Advanced → Request method **POST**
   - Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <your token>`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
   - Request body: `{"ref":"main","inputs":{"mode":"scheduled"}}`
3. **Test run** in cron-job.org should return **204**. A new "AI Radar" run appears in the Actions tab.

`"mode":"scheduled"` matters: without it every trigger checks all trackers (that's what the manual button does).
