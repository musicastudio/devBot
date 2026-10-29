"""devBot mirrors GitHub issues into Discord forum threads and files reports from @devBot mentions."""
import asyncio
import base64
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp
import discord

HERE = Path(__file__).parent
if (HERE / ".env").exists():
    for line in (HERE / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip())

REPOS = os.environ.get("GITHUB_REPOS", "musicastudio/FSVR,musicastudio/FM8.plus").split(",")
PRODUCTS = {r.split("/")[1]: r for r in REPOS}  # FSVR -> musicastudio/FSVR
MODEL = os.environ.get("OLLAMA_MODEL", "glm-5.3-flash")
STRONG = os.environ.get("OLLAMA_STRONG_MODEL", "glm-5.3")  # for the calls that close issues or merge reports
GH_TOKEN = os.environ.get("GITHUB_TOKEN")  # James's token: deleting an issue, which GitHub keeps for repo admins
# devBot's own GitHub identity, installed on every musicastudio repo, so what it posts shows as devbot-musicastudio[bot].
# claudeOpus posts as the app too, so the comment mirror tells devBot's own comments apart by MARK, not by author.
APP_ID, INSTALLATION = 5485208, 175580962
APP_KEY = HERE / "github-app.pem"

GH_EVENTS = 1562173189386412142
FORUMS = {"bug": 1564151878831575070, "feature": 1564151914566778931}
PULLS = 1565088081315635250  # pull-requests: a thread for each open pull request
APPROVALS = 2  # approvals from the Team and Contributors roles that merge a pull request
# GitHub logins of people in the Team and Contributors roles -> their Discord ids, so a pull request they open counts
# as their approval, and devBot names them by their GitHub login: James, rgwan, Wouter and EvilDragon
GITHUB_TEAM = {"jameshansen": 779115813805490189, "rgwan": 1173170271599988778,
               "WouterVanNifterick": 1375865258169733201, "mkruselj": 228175224263147523}
GITHUB_NAME = {v: k for k, v in GITHUB_TEAM.items()}


def gh_name(user):
    """How devBot names a Discord user: by GitHub login when it knows it, else by Discord name."""
    return GITHUB_NAME.get(user.id, user.display_name)
LOG = 1564169641285263400  # devBot says here what it does
CONTACT = 1564374132949520467  # #contact-inbox, private: the website's contact form posts each message here
FILES = "devbot-files"  # the branch each repo keeps Discord attachments on, since GitHub's API can't attach files to issues
CLAUDE_OPUS = 1550529204066451506  # the coding agent issues are handed to
SCRATCHPAD = 1557447421926187118  # where claudeOpus takes work asked for outside its channels
# claudeOpus's DISCORD_ALLOWED_CHANNELS: #fsvr-development, #fsvr-gui, #fsvr-questions and #scratchpad. The forums stay out,
# since claudeOpus reading an issue thread would answer a direct tag there as well as devBot's handover of it.
OPUS_CHANNELS = {1549099992101290114, 1552054486720315513, 1552054514809315439, SCRATCHPAD}
AGENT_USERS = {779115813805490189, 1173170271599988778}  # James and rgwan, who claudeOpus takes work from
AGENT_ROLES = {1551612933157556264, 1555639415890514010}  # and the Team and Contributors roles, which it takes work from too
CATEGORIES = ["Engine", "Effects", "Presets", "UI", "MIDI", "Host/DAW", "Platform", "Docs", "Other"]
IMPORTANCE = ["Critical", "High", "Medium", "Low"]
POLL = 300  # seconds between GitHub checks when gh-events is quiet

REF = re.compile(r"(?<![\w#&])#(\d+)\b")  # #54, but not C#3 or &#39;
MENTION = re.compile(r"<@[!&]?\d+>")
CLOSE = re.compile(r"^close\b", re.I)  # `@devBot close`
ADD = re.compile(r"^add\s+(images?|pictures?|screenshots?|files?)\b", re.I)  # `@devBot add image`
DELETE = re.compile(r"^delete\b", re.I)  # `@devBot delete`
APPROVE = re.compile(r"^approved?\b", re.I)  # `@devBot approve`, in a pull request's thread
IMG = re.compile(r'<img[^>]*\bsrc="([^"]+)"|!\[[^\]]*\]\((\S+?)\)')  # an image in an issue body, as HTML or markdown
# devBot comments on GitHub with James's token, so its own comments are told apart by this marker, and by how the
# ones it wrote before the marker open
MARK = "<!-- devBot -->"
OWN = ("Resolved by ", "Closed from Discord by ", "Added from Discord by ", "Files from the Discord report", "Also reported on Discord by ")
KEY = re.compile(r"(?:(?<![\w#])([A-Za-z][\w.]*?))?#(\d+)\b")  # FSVR#49, FM8.plus#3 or #49
LINK = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)")  # a Discord message link

STATE = HERE / "state.json"
ARCHIVE = HERE / "archive"  # backups of closed issues' threads
state = json.loads(STATE.read_text()) if STATE.exists() else {"issues": {}, "since": {}, "commits": {}, "seen": []}
state.setdefault("comments", {})  # per repo, when its GitHub comments were last read
state.setdefault("pending", {})  # issues a commit resolved, closed once their grace period is over (schedule_close)
state.setdefault("pulls", {})  # open pull requests: their thread, head commit and who approved that commit
state.setdefault("inbox", {})  # commands being answered, message id -> channel id, so a restart doesn't lose them
seen = set(state["seen"])  # ponytail: one entry per commit and PR event, prune it if it ever gets large


def save():
    state["seen"] = sorted(seen)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def parse_json(text):
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


def pick(value, options, default):
    return next((o for o in options if o.lower() == str(value).strip().lower()), default)


def norm(product, ref):
    ref = str(ref or "").strip().lstrip("#")
    return f"{product}#{ref}" if ref.isdigit() else ref


def grounded(quote, text):
    """True when the model's quote really is in the text, so a reason it made up can't close an issue."""
    q = " ".join(str(quote or "").split()).lower()
    return len(q) >= 20 and q in " ".join(text.split()).lower()


def now_iso(minus=timedelta()):
    return (datetime.now(timezone.utc) - minus).strftime("%Y-%m-%dT%H:%M:%SZ")


# GitHub and Ollama

http: aiohttp.ClientSession = None
about = {}


app_token = {"value": None, "expires": 0.0}


def app_jwt():
    """The app proves itself with a JWT signed by its key, which GitHub trades for an hour's installation token."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(APP_KEY.read_bytes(), None)
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=")
    now = int(time.time())
    signed = part({"alg": "RS256", "typ": "JWT"}) + b"." + part({"iat": now - 60, "exp": now + 540, "iss": str(APP_ID)})
    return (signed + b"." + base64.urlsafe_b64encode(key.sign(signed, padding.PKCS1v15(), hashes.SHA256())).rstrip(b"=")).decode()


async def token():
    """The app's installation token while its key is here, renewed five minutes before it runs out; James's otherwise."""
    if not APP_KEY.exists():
        return GH_TOKEN
    if time.time() > app_token["expires"] - 300:
        async with http.post(f"https://api.github.com/app/installations/{INSTALLATION}/access_tokens",
                             headers={"Authorization": f"Bearer {app_jwt()}", "Accept": "application/vnd.github+json"}) as r:
            if r.status >= 400:
                raise RuntimeError(f"GitHub {r.status} minting the app's token: {await r.text()}")
            d = await r.json()
        app_token.update(value=d["token"], expires=datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00")).timestamp())
    return app_token["value"]


async def gh(method, path, as_james=False, **kw):
    tok = GH_TOKEN if as_james else await token()
    headers = {"Accept": "application/vnd.github+json"} | ({"Authorization": f"Bearer {tok}"} if tok else {})
    async with http.request(method, "https://api.github.com" + path, headers=headers, **kw) as r:
        if r.status >= 400:
            raise RuntimeError(f"GitHub {r.status} on {path}: {await r.text()}")
        return await r.json()


async def gh_all(path, **params):
    out, page = [], 1
    while True:
        batch = await gh("GET", path, params={**params, "per_page": 100, "page": page})
        out += batch
        if len(batch) < 100:
            return out
        page += 1


async def load_about():
    for p, r in PRODUCTS.items():
        about[p] = (await gh("GET", f"/repos/{r}"))["description"] or p


async def ask(prompt, model=MODEL, images=()):
    message = {"role": "user", "content": prompt} | ({"images": list(images)} if images else {})
    async with http.post("https://ollama.com/api/chat", headers={"Authorization": f"Bearer {os.environ['OLLAMA_API_KEY']}"},
                         json={"model": model, "stream": False, "messages": [message]}) as r:
        if r.status >= 400:
            raise RuntimeError(f"Ollama {r.status}: {await r.text()}")
        return parse_json((await r.json())["message"]["content"])


def tidy(d):
    """A verdict with its type, category and importance held to the allowed values."""
    d["type"] = "feature" if str(d.get("type", "")).lower().startswith("feat") else "bug"
    d["category"] = pick(d.get("category"), CATEGORIES, "Other")
    d["importance"] = pick(d.get("importance"), IMPORTANCE, "Medium")
    return d


async def classify(text, extra="", model=MODEL, many=False):
    """The model's verdict on a report, or with `many` a list of verdicts, one per separate bug or request in it."""
    products = "\n".join(f"- {p}: {d}" for p, d in about.items())
    shape = ('only JSON like {"reports": [{...}, {...}]}, usually with one object. Make two only for unrelated problems; several '
             'messages about one problem are one report, and a specific case of a broader request is part of that request, '
             'not a report of its own. Each object has these keys' if many else
             "only a JSON object with these keys")
    try:
        d = await ask(f"""You triage bug reports and feature requests for these products:
{products}

Reply with {shape}:
"type": "bug" if something is broken, "feature" for a request, idea or question
"category": one of {CATEGORIES}
  Engine = sound generation, voices, polyphony, CPU. Effects = reverb, variation, insertion. Presets = banks, browser, import, saving.
  UI = the interface, layout, controls. MIDI = MIDI input, CC, automation, tuning. Host/DAW = behaviour inside a specific DAW.
  Platform = an operating system, installing, audio drivers, the standalone app. Docs = manual and documentation.
"importance": one of {IMPORTANCE}
  Critical = crash, data loss or unusable. High = a main feature broken. Medium = annoying with a workaround. Low = cosmetic or nice to have.
{extra}
Report:
{text}""", model)
    except Exception as e:
        print("classify failed:", repr(e))
        d = {}
    if not many:
        return tidy(d)
    reports = d.get("reports") if isinstance(d.get("reports"), list) else [d]  # a lone object is one report
    return [tidy(r) for r in reports if isinstance(r, dict)] or [tidy({})]


def issue_text(item):
    labels = ", ".join(lb["name"] for lb in item["labels"]) or "none"
    return f"Title: {item['title']}\nLabels: {labels}\n\n{(item.get('body') or '')[:4000]}"


# Discord

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
lock = asyncio.Lock()
pulls_lock = asyncio.Lock()  # approvals take only this, so they don't wait for a sync's commit checks
wake = asyncio.Event()
tags = {}


async def ensure_tags():
    for fid, names in [*((f, [*PRODUCTS, *CATEGORIES, *IMPORTANCE]) for f in FORUMS.values()), (PULLS, [*PRODUCTS])]:
        forum = bot.get_channel(fid)
        if forum is None:
            raise RuntimeError(f"forum {fid} is not visible, is the bot in the server?")
        have = {t.name.lower() for t in forum.available_tags}
        missing = [n for n in names if n.lower() not in have]
        if missing:  # one edit for all of them, since each edit sends the whole list and edits in a row overwrite each other
            await forum.edit(available_tags=[*forum.available_tags, *(discord.ForumTag(name=n) for n in missing)])
            forum = await bot.fetch_channel(fid)
        tags[fid] = {t.name.lower(): t for t in forum.available_tags}


def applied(info):
    t = tags[FORUMS[info["type"]]]
    return [t[n.lower()] for n in (info["product"], info["category"], info["importance"])]


def tracked(key):
    """An issue's or a pull request's entry. GitHub numbers both in one sequence, so a key is never both."""
    return state["issues"].get(key) or state["pulls"].get(key)


def link(key):
    """The issue's thread, or its GitHub page once the issue is closed and its thread backed up and deleted."""
    if tid := (tracked(key) or {}).get("thread"):
        return f"<#{tid}>"
    product, n = key.split("#")
    return f"<https://github.com/{PRODUCTS[product]}/issues/{n}>"


def title(item):
    return f"#{item['number']} {item['title']}"[:100]


def plain(body):
    """GitHub markdown as a Discord post shows it: each image becomes [image], since the post carries the image itself."""
    body = re.sub(r"<img[^>]*>", "[image]", (body or "").strip())
    return re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[image: \1]", body)


def starter(key, item, info):
    body = plain(item.get("body"))
    if len(body) > 1500:
        body = body[:1500] + "…"
    who = (item.get("user") or {}).get("login", "ghost")
    return f"**{key}** · {info['category']} · {info['importance']}\n<{item['html_url']}>\nOpened by {who}\n\n{body}"[:2000]


def gist(item):
    """The start of an issue's body on one line, kept so commit checks can see what each issue is about."""
    return re.sub(r"\s+", " ", re.sub(r"<img[^>]*>", "", item.get("body") or "")).strip()[:300]


async def log(text):
    try:
        return await bot.get_channel(LOG).send(text[:2000])
    except Exception as e:
        print("log failed:", repr(e))


async def thread_of(key):
    tid = tracked(key)["thread"]
    try:
        return bot.get_channel(tid) or await bot.fetch_channel(tid)
    except discord.NotFound:
        state["issues"].pop(key, None)  # deleted by hand, the next sync recreates it if the issue is open
        state["pulls"].pop(key, None)
        raise


async def as_files(attachments):
    """Fresh copies of Discord attachments to post again, leaving out any over the upload limit (the issue links those)."""
    return [await a.to_file() for a in attachments if a.size <= 10 * 1024 * 1024][:10]


async def post(key, text, attachments=()):
    if (tracked(key) or {}).get("thread"):  # a closed issue's thread is backed up and deleted, so its notes stay in the log
        await (await thread_of(key)).send(text, files=await as_files(attachments))


known_branches = set()


async def stash(product, attachments):
    """Copies Discord attachments onto the repo's devbot-files branch and returns markdown for each: images show,
    other files link. Discord's own links expire, so an issue can't point at them."""
    repo = PRODUCTS[product]
    if repo not in known_branches:
        try:
            await gh("GET", f"/repos/{repo}/git/ref/heads/{FILES}")
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            # an orphan branch, so it holds the files and none of the code
            tree = await gh("POST", f"/repos/{repo}/git/trees", json={"tree": [{"path": "README.md", "mode": "100644", "type": "blob",
                                                                                "content": "Files devBot copies from Discord for issues.\n"}]})
            commit = await gh("POST", f"/repos/{repo}/git/commits", json={"message": "Files devBot copies from Discord for issues",
                                                                          "tree": tree["sha"], "parents": []})
            await gh("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{FILES}", "sha": commit["sha"]})
        known_branches.add(repo)
    out = []
    for a in attachments:
        path = quote(f"discord/{a.id}-{a.filename}")
        await gh("PUT", f"/repos/{repo}/contents/{path}", json={"message": f"{a.filename} from Discord", "branch": FILES,
                                                                "content": base64.b64encode(await a.read()).decode()})
        url = f"https://raw.githubusercontent.com/{repo}/{FILES}/{path}"
        out.append(f"![{a.filename}]({url})" if (a.content_type or "").startswith("image/") else f"[{a.filename}]({url})")
    return out


def body_images(body):
    return [a or b for a, b in IMG.findall(body or "")]


async def download_images(urls, first=1):
    """GitHub's images as files to post, numbered from `first`, leaving out any that fail or are over the upload limit."""
    files = []
    for i, url in enumerate(urls, first):
        try:
            async with http.get(url) as r:
                r.raise_for_status()
                data, kind = await r.read(), r.content_type
        except Exception as e:
            print("image failed:", url, repr(e))
            continue
        if len(data) <= 10 * 1024 * 1024:
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(kind, "png")
            files.append(discord.File(io.BytesIO(data), f"image-{i}.{ext}"))
    return files


async def attach_images(key, urls):
    """Puts the images in an issue's GitHub body on its thread's first post, which only says [image] for each."""
    th = await thread_of(key)
    first = await th.fetch_message(th.id)
    files = await download_images(urls, len(first.attachments) + 1)
    if th.archived:
        await th.edit(archived=False)
    room = 10 - len(first.attachments)  # a message holds ten files, the rest follow in the thread
    if files[:room]:
        await first.edit(attachments=[*first.attachments, *files[:room]])
    for b in range(room, len(files), 10):
        await th.send("More images from the issue:", files=files[b:b + 10])


async def open_thread(key, item, info, attachments=()):
    info = {k: info[k] for k in ("product", "type", "category", "importance")} | {"title": item["title"], "open": True, "body": gist(item)}
    # a report's files come with the thread, so its body's images count as on it; a GitHub issue's wait for reconcile
    info["images"] = body_images(item.get("body")) if attachments else []
    made = await bot.get_channel(FORUMS[info["type"]]).create_thread(
        name=title(item), content=starter(key, item, info), applied_tags=applied(info), files=await as_files(attachments))
    state["issues"][key] = info | {"thread": made.thread.id}
    save()
    print("created", key, info["type"], info["category"], info["importance"])
    await log(f"Created thread <#{made.thread.id}> for **{key}** ({info['type']}, {info['category']}, {info['importance']}) <{item['html_url']}>")
    return made.thread


def backup_of(key):
    return ARCHIVE / key.replace("#", "-")


async def retire(key, item):
    """Backs up a closed issue's thread to archive/, then deletes it from the forum."""
    th = await thread_of(key)
    reason = (item.get("state_reason") or "closed").replace("_", " ")
    await th.send(f"Closed on GitHub ({reason}).")
    folder = backup_of(key)
    folder.mkdir(parents=True, exist_ok=True)
    said = []
    async for m in th.history(limit=None, oldest_first=True):
        for a in m.attachments:  # Discord's attachment links expire, so the files themselves are kept
            await a.save(folder / f"{m.id}-{a.filename}")
        said.append({"author": gh_name(m.author), "bot": m.author.bot, "at": m.created_at.isoformat(timespec="seconds"),
                     "text": m.content, "files": [f"{m.id}-{a.filename}" for a in m.attachments]})
    record = folder / "thread.json"
    record.write_text(json.dumps({"issue": key, "title": item["title"], "url": item["html_url"], "closed": reason,
                                  "thread": th.name, "messages": said}, indent=1))
    await log(f"**{key}** closed on GitHub ({reason}). Its thread, {len(said)} messages, is backed up and deleted from the forum <{item['html_url']}>")
    await th.delete()
    tracked(key)["thread"] = None


async def restore(key, item):
    """A reopened issue gets a new thread, carrying on the discussion from its backup."""
    th = await open_thread(key, item, state["issues"][key])
    record = backup_of(key) / "thread.json"
    if record.exists():
        msg = "Earlier discussion, restored from the backup of the thread:"
        for m in json.loads(record.read_text())["messages"][1:]:  # the first is the old thread's opening post
            ln = f"\n**{m['author']}** ({m['at'][:10]}): {m['text']}" + "".join(f" [{f}]" for f in m["files"])
            if len(msg) + len(ln) > 1900:
                await th.send(msg)
                msg = "(continued)"
            msg += ln[:1800]
        await th.send(msg)
    await log(f"**{key}** reopened on GitHub, back on the list as {link(key)}")


async def update_issue(key, item):
    info = state["issues"][key]
    is_open = item["state"] == "open"
    if not is_open:
        if info.get("thread"):
            await retire(key, item)
    elif not info.get("thread"):
        return await restore(key, item)  # open_thread gives the issue a fresh entry
    else:
        th = await thread_of(key)
        if th.archived:  # Discord archives a quiet thread on its own
            await th.edit(archived=False)
        msg = await th.fetch_message(th.id)
        if msg.content != (text := starter(key, item, info)):
            await msg.edit(content=text)
        await th.edit(name=title(item), applied_tags=applied(info))
    info.update(open=is_open, title=item["title"], body=gist(item))


async def pr_note(product, item):
    status = "merged" if item["pull_request"].get("merged_at") else "opened" if item["state"] == "open" else "closed"
    for n in set(REF.findall(f"{item['title']}\n{item.get('body') or ''}")):
        key, mark = f"{product}#{n}", f"pr {product}#{item['number']} {status} {n}"
        if key in state["issues"] and mark not in seen:
            await post(key, f"Pull request #{item['number']} {status}: [{item['title']}](<{item['html_url']}>)")
            await log(f"Pull request [#{item['number']}](<{item['html_url']}>) {status}, noted in **{key}** {link(key)}")
            seen.add(mark)


async def sync_issues(product, repo):
    since = state["since"].get(repo)
    started = now_iso(timedelta(minutes=1))
    params = {"state": "all", "since": since, "sort": "updated", "direction": "asc"} if since else \
        {"state": "open", "sort": "created", "direction": "asc"}  # first run is the populate: every open issue
    ok = True
    for item in await gh_all(f"/repos/{repo}/issues", **params):
        key = f"{product}#{item['number']}"
        try:
            if "pull_request" in item:
                await pr_note(product, item)
            elif key in state["issues"]:
                await update_issue(key, item)
            elif item["state"] == "open":
                await open_thread(key, item, await classify(issue_text(item)) | {"product": product})
        except Exception as e:
            print(key, "failed:", repr(e))
            ok = False
        save()
    if ok:  # a failed item keeps the window open so the next sync retries it
        state["since"][repo] = started
    await reconcile(product, repo)


async def sync_comments(product, repo):
    """A new comment on a GitHub issue or pull request goes to its thread, with its images, and so does a comment on a
    pull request's code. Each is posted once, so edits stay put."""
    started = now_iso(timedelta(minutes=1))
    since = state["comments"].get(repo) or now_iso(timedelta(days=1))  # the first run reaches back a day
    said = [*await gh_all(f"/repos/{repo}/issues/comments", since=since, sort="created", direction="asc"),
            *await gh_all(f"/repos/{repo}/pulls/comments", since=since, sort="created", direction="asc")]
    for c in sorted(said, key=lambda c: c["created_at"]):
        key = f"{product}#{(c.get('issue_url') or c['pull_request_url']).rsplit('/', 1)[1]}"
        if (tracked(key) or {}).get("thread"):
            await mirror(key, c)
    state["comments"][repo] = started


async def mirror(key, c):
    """Posts a GitHub comment in its thread once, unless devBot wrote it, since that already has its post there."""
    body = c.get("body") or ""
    if f"comment {c['id']}" not in seen and MARK not in body and not body.startswith(OWN):
        text = plain(body)
        text = text[:1700] + "…" if len(text) > 1700 else text
        where = f" on `{c['path']}`" if c.get("path") else ""
        await (await thread_of(key)).send(f"**{c['user']['login']}** commented{where} on GitHub <{c['html_url']}>\n>>> {text}",
                                          files=await download_images(body_images(body)))
        await log(f"{c['user']['login']}'s GitHub comment on **{key}** posted to {link(key)}")
    seen.add(f"comment {c['id']}")


async def reconcile(product, repo):
    """A deleted issue never shows up as updated, so the open ones devBot tracks are checked against GitHub's list.
    The same list brings each body's images onto threads that don't have them yet."""
    listed = [x for x in await gh_all(f"/repos/{repo}/issues", state="open") if "pull_request" not in x]
    on_github = {f"{product}#{x['number']}" for x in listed}
    for x in listed:
        info = state["issues"].get(key := f"{product}#{x['number']}")
        if info and info.get("thread") and (new := [u for u in body_images(x.get("body")) if u not in info.get("images", [])]):
            try:
                await attach_images(key, new)
            except Exception as e:
                print(key, "attaching images failed:", repr(e))
            info["images"] = [*info.get("images", []), *new]  # tried once, so a broken image can't retry every sync
            save()
    for key in [k for k, i in state["issues"].items() if i["product"] == product and i["open"] and k not in on_github]:
        try:
            item = await gh("GET", f"/repos/{repo}/issues/{key.split('#')[1]}")
        except RuntimeError as e:
            if "GitHub 410" not in str(e):  # 410 Gone is GitHub's answer for a deleted issue, anything else may pass
                raise
            title_ = state["issues"][key]["title"]
            await forget(key)
            await log(f"**{key}** ({title_}) was deleted on GitHub, so its thread is gone too")
            continue
        await update_issue(key, item)  # closed without devBot seeing it
        save()


def pull_starter(key, pr, info):
    body = plain(pr.get("body"))
    body = body[:1200] + "…" if len(body) > 1200 else body
    names = ", ".join(info["approvals"].values())
    return (f"**{key}** · {pr['head']['label']} → {pr['base']['ref']}{' · draft' if pr.get('draft') else ''}\n<{pr['html_url']}>\n"
            f"Review the changes: <{pr['html_url']}/files>\n"
            f"Opened by {(pr.get('user') or {}).get('login', 'ghost')}\n\n{body}\n\n"
            f"**Approvals: {len(info['approvals'])} of {APPROVALS}**{f' ({names})' if names else ''}. The Team and Contributors roles "
            f"approve with `@devBot approve` here, and devBot merges it at {APPROVALS}.")[:2000]


def authored(pr):
    """The author's own approval, when the author is on the team. A pull request devBot opened for claudeOpus is on a
    branch named for whoever asked for the work."""
    user = pr.get("user") or {}
    login = pr["head"]["ref"].split("/")[0] if user.get("type") == "Bot" else user.get("login")
    return {str(GITHUB_TEAM[login]): login} if login in GITHUB_TEAM else {}


async def show_pull(key, pr):
    """Keeps a pull request's thread title and first post, with its approvals, up to date."""
    th = await thread_of(key)
    first = await th.fetch_message(th.id)
    if first.content != (text := pull_starter(key, pr, state["pulls"][key])) or th.name != title(pr):
        if th.archived:
            await th.edit(archived=False)
        await first.edit(content=text)
        await th.edit(name=title(pr))


async def sync_pulls(product, repo):
    """Each open pull request has a thread in pull-requests, which GitHub's reviews go to. A push to its branch starts
    the approvals over, and once it's merged or closed its thread is backed up and deleted like a closed issue's."""
    listed = await gh_all(f"/repos/{repo}/pulls", state="open", sort="created", direction="asc")
    for pr in listed:
        key = f"{product}#{pr['number']}"
        try:
            if not (info := state["pulls"].get(key)):
                info = {"product": product, "sha": pr["head"]["sha"], "approvals": {}}
                made = await bot.get_channel(PULLS).create_thread(name=title(pr), content=pull_starter(key, pr, info),
                                                                  applied_tags=[tags[PULLS][product.lower()]])
                state["pulls"][key] = info = info | {"thread": made.thread.id}
                await log(f"Created thread <#{made.thread.id}> for pull request **{key}** <{pr['html_url']}>")
            elif info["sha"] != pr["head"]["sha"]:  # what was approved isn't what would merge any more
                if info["approvals"] != authored(pr):
                    await post(key, "New commits on the branch, so the approvals start over.")
                info.update(sha=pr["head"]["sha"], approvals={})
            info["approvals"] = authored(pr) | info["approvals"]
            await show_pull(key, pr)
            if not info.get("history"):  # a new thread starts with the conversation so far, and sync_comments brings the rest
                n = pr["number"]
                said = [*await gh_all(f"/repos/{repo}/issues/{n}/comments"), *await gh_all(f"/repos/{repo}/pulls/{n}/comments")]
                for c in sorted(said, key=lambda c: c["created_at"]):
                    await mirror(key, c)
                info["history"] = True
            for r in await gh_all(f"/repos/{repo}/pulls/{pr['number']}/reviews"):
                verdict = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes"}.get(r["state"])
                if f"review {r['id']}" not in seen and (verdict or r.get("body")):  # comments on the code come with sync_comments
                    text = plain(r.get("body"))[:1700]
                    await post(key, f"**{r['user']['login']}** {verdict or 'reviewed'} on GitHub <{r['html_url']}>" + (f"\n>>> {text}" if text else ""))
                seen.add(f"review {r['id']}")
        except Exception as e:
            print(key, "failed:", repr(e))
        save()
    on_github = {f"{product}#{pr['number']}" for pr in listed}
    for key in [k for k, i in state["pulls"].items() if i["product"] == product and k not in on_github]:
        try:
            pr = await gh("GET", f"/repos/{repo}/pulls/{key.split('#')[1]}")
            if pr["state"] != "open":  # an open one missing from the list was opened since
                await retire(key, pr | {"state_reason": "merged" if pr.get("merged_at") else "closed"})
                state["pulls"].pop(key)
        except Exception as e:
            print(key, "failed:", repr(e))
        save()


def open_issues(product):
    return {k: f"({i['type']}) {i['title']}: {i.get('body', '')}" for k, i in state["issues"].items()
            if i["open"] and i["product"] == product}


def keyed(product, found, cands):
    if isinstance(found, list):
        found = dict.fromkeys(found, "")
    if not isinstance(found, dict):
        return {}
    return {k: str(why)[:300] for v, why in found.items() if (k := norm(product, v)) in cands}


async def changes(product, c):
    """The commit's diff as text, since a message can leave out what the code makes plain. Small patches come first:
    a short script says more than a long generated file, and binaries have nothing to read. A merge's diff is all its
    branch brought, against main as it was."""
    repo = PRODUCTS[product]
    full = await gh("GET", f"/repos/{repo}/compare/{c['parents'][0]['sha']}...{c['sha']}" if len(c["parents"]) > 1 else
                    f"/repos/{repo}/commits/{c['sha']}")
    files = sorted(full.get("files", []), key=lambda f: len(f.get("patch") or "") or 10 ** 9)
    return "\n".join(f"--- {f['filename']} ({f['status']})\n{f.get('patch', '(binary)')}" for f in files)


def reported(x):
    """When an issue was first reported: one filed from the Discord scan carries its reports' dates in its body."""
    days = re.findall(r"\(#[^()\n]*?, (\d{4}-\d\d-\d\d)\) ", x.get("body") or "")
    return min(days) if days else x["created_at"]


async def check_commit(product, c, cands):
    """Asks the model which of `cands` (key -> what the issue is) a commit resolves outright and which it works on."""
    if not cands:
        return {}, {}
    diff = await changes(product, c)
    try:
        d = await ask(f"""A commit landed in {product} ({about.get(product, '')}). Decide which of the open issues below it resolves completely, so the issue can be closed, and which it works on without finishing. Read the changes as well as the message, since a message can leave out what the code makes plain.
Resolves: the commit fixes exactly the problem the issue reports, or delivers what it asks for. A commit naming an issue (like "Answers #29") is a strong hint, but check that it covers the whole issue.
Works on: the commit clearly addresses part of that issue.
Touching the same area is neither. Most commits resolve nothing and work on nothing.
Reply with only JSON like {{"resolves": {{"{product}#12": "one-line reason"}}, "works_on": {{}}}}.

Commit {c['sha'][:7]} on {c['commit']['committer']['date'][:10]}:
{c['commit']['message'][:6000]}

Changes (cut short):
{diff[:12000]}

Open issues:
""" + "\n".join(f"{k} {v}" for k, v in cands.items()))
    except Exception as e:
        print("commit check failed:", repr(e))
        return {}, {}
    proposed = keyed(product, d.get("works_on"), cands) | keyed(product, d.get("resolves"), cands)
    resolves = {}
    for k, why in proposed.items():  # flash can undersell a commit too, so glm-5.3 judges every issue it links
        if quote := await confirm(product, c, k, diff):
            resolves[k] = f'{why} The commit shows it: "{quote[:250]}"'
    return resolves, {k: w for k, w in proposed.items() if k not in resolves}


async def confirm(product, c, key, diff=None):
    """A second look at one proposed close with the whole issue, accepted only with a real quote from the commit."""
    path = f"/repos/{PRODUCTS[product]}/issues/{key.split('#')[1]}"
    x, when = await gh("GET", path), c["commit"]["committer"]["date"]
    diff = await changes(product, c) if diff is None else diff
    msg = c["commit"]["message"]
    if reported(x) > when:
        return None  # a commit can't fix a report made after it, and may be what caused it
    # the discussion as it stands, since closing happens now: a maintainer waiting on the reporter keeps it open
    talk = "\n".join(f"{m['user']['login']} ({m['created_at'][:10]}): {m['body'][:600]}"
                     for m in (await gh("GET", path + "/comments", params={"per_page": 100}))[-6:])
    prompt = f"""Does this commit completely fix the problem this issue reports, or deliver what it asks for? Answer yes only when the commit, its message or its changes, shows that fix, not a change nearby. Delivering what the issue asks for in a different way still counts, since an issue's idea of how to build it is a suggestion, not part of the request. Answer no when the commit calls its fix likely, a guess or unconfirmed, or covers only part of what the issue reports. A change that only reports, logs or explains the problem, without making the reported thing work, is not a fix. Answer no as well when the issue is too vague to tell what is wrong, when its comments are waiting on the reporter to try a fix, or when comments dated after the commit show the problem still there or the discussion still going. Quote the sentence of the commit message or its changes that shows the fix, word for word.
Reply with only JSON like {{"fixed": true, "quote": "..."}} or {{"fixed": false, "quote": ""}}.

Issue {key}: {x['title']}
{(x.get('body') or '')[:3000]}

Comments:
{talk or 'none'}

Commit {c['sha'][:7]} on {when[:10]}:
{msg[:6000]}

Changes (cut short):
{diff[:40000]}"""

    async def once():
        try:
            d = await ask(prompt, STRONG)
        except Exception as e:
            print("confirm failed:", repr(e))
            return None
        if str(d.get("fixed")).lower() in ("true", "yes") and grounded(d.get("quote"), msg + "\n" + diff):
            return " ".join(str(d["quote"]).split())

    # two independent yeses: a single run lets the odd wrong close through, a missed close only leaves the issue open
    first, second = await asyncio.gather(once(), once())
    if first and second:
        return first
    print(f"confirm rejected {key} for {c['sha'][:7]}: {bool(first)}, {bool(second)}")


async def close_issue(key, comment):
    product, n = key.split("#")
    repo = PRODUCTS[product]
    await gh("POST", f"/repos/{repo}/issues/{n}/comments", json={"body": comment + "\n\nReopen this if the problem is still there.\n" + MARK})
    await update_issue(key, await gh("PATCH", f"/repos/{repo}/issues/{n}", json={"state": "closed", "state_reason": "completed"}))


GRACE = timedelta(minutes=30)  # a commit's session usually explains the fix itself, often closing the issue too


def schedule_close(key, sha, url, why):
    """Closes `key` GRACE after the latest commit that resolves it, so a later commit joins the same close and the
    session that made it can comment or close first. Returns when the close is due."""
    p = state["pending"].setdefault(key, {"since": now_iso(), "commits": []})
    if sha not in (c["sha"] for c in p["commits"]):
        p["commits"].append({"sha": sha, "url": url, "why": why})
    due = datetime.now(timezone.utc) + GRACE
    p["due"] = due.isoformat()
    save()
    return due


async def close_due():
    """Closes what commits resolved once its grace period is over. Nothing happens to an issue already closed, and
    an issue someone commented on since the commit (claudeOpus explaining its fix, say) closes without a comment."""
    now = datetime.now(timezone.utc)
    for key, p in list(state["pending"].items()):
        if datetime.fromisoformat(p["due"]) > now:
            continue
        state["pending"].pop(key)
        save()
        product, n = key.split("#")
        repo = PRODUCTS[product]
        if (await gh("GET", f"/repos/{repo}/issues/{n}"))["state"] != "open":
            continue
        said = [c for c in await gh("GET", f"/repos/{repo}/issues/{n}/comments", params={"since": p["since"]})
                if MARK not in (c.get("body") or "")]
        if said:
            await update_issue(key, await gh("PATCH", f"/repos/{repo}/issues/{n}", json={"state": "closed", "state_reason": "completed"}))
            await log(f"**{key}** closed, a grace period after the commit that resolved it, with no comment of devBot's since "
                      f"{said[-1]['user']['login']} commented already")
        else:
            commits = "\n".join(f"- {c['sha']} ({c['url']}): {c['why']}" for c in p["commits"])
            await close_issue(key, f"Resolved by:\n{commits}\n\nClosed by devBot.")
            await log(f"**{key}** closed, a grace period after the commit that resolved it")


async def verify_commit(product, c):
    sha, msg = c["sha"][:7], c["commit"]["message"]
    what = f"[`{sha}`](<{c['html_url']}>) by {c['commit']['author']['name']}: *{msg.splitlines()[0][:150]}*"
    cands = open_issues(product)
    note = await log(f"Commit {what}\nPushed. Verifying whether it closes any of the {len(cands)} open {product} bugs and feature requests…")
    resolves, works_on = await check_commit(product, c, cands)
    named = list(dict.fromkeys(k for n in REF.findall(msg) if (k := f"{product}#{n}") in state["issues"] and k not in resolves))
    due = {}
    for k, why in resolves.items():
        due[k] = schedule_close(k, sha, c["html_url"], why)
        await post(k, f"Commit {what}\nResolves this: {why}\n"
                      f"devBot closes the GitHub issue <t:{int(due[k].timestamp())}:R> unless it's closed first.")
    for k in named:  # threads only hear of confirmed closes and of commits that name them, the model's other guesses stay in the log
        await post(k, f"Commit {what}\nNames this issue" + (f": {works_on[k]}" if k in works_on else "."))
    th = lambda k: f"**{k}** {link(k)}"
    outcome = [f"→ closes {th(k)} <t:{int(due[k].timestamp())}:t> unless it's closed first: {w}" for k, w in resolves.items()]
    outcome += [f"→ names {th(k)}" for k in named]
    outcome += [f"→ might relate to {th(k)}: {w}" for k, w in works_on.items() if k not in named]
    if note:
        await note.edit(content=(note.content + "\n" + ("\n".join(outcome) or "→ closes none of them"))[:2000])


async def sync_commits(product, repo):
    first = repo not in state["commits"]
    started = now_iso()
    # ponytail: two-day overlap catches pushes of older commits, `seen` stops repeats
    since = state["commits"].get(repo, started)
    since = (datetime.fromisoformat(since.replace("Z", "+00:00")) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pushed = (await gh_all(f"/repos/{repo}/commits", since=since))[::-1]
    # a merged pull request is checked once, as its commits' messages and its whole diff, before its commits come up
    for c in [c for c in pushed if len(c["parents"]) > 1 and c["sha"] not in seen]:
        brought = (await gh("GET", f"/repos/{repo}/compare/{c['parents'][0]['sha']}...{c['sha']}"))["commits"]
        new = [x for x in brought if x["sha"] not in seen and len(x["parents"]) == 1]
        if new and not first:  # a merge of main into a local main brings nothing new
            msg = "\n\n".join([c["commit"]["message"], *(f"{x['sha'][:7]} {x['commit']['message']}" for x in new)])
            names = ", ".join(dict.fromkeys(x["commit"]["author"]["name"] for x in new))
            await verify_commit(product, c | {"commit": c["commit"] | {"message": msg, "author": c["commit"]["author"] | {"name": names}}})
        seen.update(x["sha"] for x in brought)
        seen.add(c["sha"])
    for c in [c for c in pushed if c["sha"] not in seen]:
        if not first:  # the first run only records history, it doesn't announce it
            await verify_commit(product, c)
        seen.add(c["sha"])
    state["commits"][repo] = started


async def sync_forever():
    await bot.wait_until_ready()
    await replay()
    while True:
        wake.clear()
        try:
            async with lock:
                if not tags:
                    await ensure_tags()
                for product, repo in PRODUCTS.items():
                    await sync_issues(product, repo)
                    async with pulls_lock:
                        await sync_pulls(product, repo)
                    await sync_comments(product, repo)
                    await sync_commits(product, repo)
                await close_due()
                save()
        except Exception as e:
            print("sync failed:", repr(e))
        try:
            await asyncio.wait_for(wake.wait(), POLL)
            await asyncio.sleep(15)  # gh-events fired, give the GitHub API a moment to catch up
        except asyncio.TimeoutError:
            pass


async def replied_to(m):
    if m.reference and m.reference.message_id:
        if isinstance(m.reference.resolved, discord.Message):
            return m.reference.resolved
        return await m.channel.fetch_message(m.reference.message_id)
    return m


async def sources(m):
    """The messages a tag points at: the one it replies to, then any it links."""
    found = [] if (t := await replied_to(m)) is m else [t]
    for c, i in LINK.findall(m.content):
        try:
            found.append(await (bot.get_channel(int(c)) or await bot.fetch_channel(int(c))).fetch_message(int(i)))
        except discord.HTTPException:
            pass
    return found


async def earlier(m):
    """The last 25 messages before a tag in its channel, however old, for a tag like "add the 12 kHz notch to the bug list"
    that names a problem discussed above it."""
    msgs = [p async for p in m.channel.history(limit=25, before=m, oldest_first=False)]
    return [p for p in reversed(msgs) if p.author != bot.user and (p.content or p.attachments)]


sights = {}  # an image attachment's id -> what glm-5.3-flash saw in it, since glm-5.3 can't see


async def look(a):
    """What glm-5.3-flash sees in an image, for the prompts of glm-5.3. Each image is looked at once."""
    if a.id not in sights:
        try:
            d = await ask("This image was posted on Discord with a bug report or feature request for a synthesizer plugin. Say "
                          "what it shows that matters for the report: controls, values, settings, labels, error text or plotted "
                          'data. Reply with only JSON like {"shows": "one or two sentences"}.',
                          MODEL, [base64.b64encode(await a.read()).decode()])
            sights[a.id] = str(d.get("shows") or "")[:400]
        except Exception as e:
            print("look failed:", repr(e))
            sights[a.id] = ""
    return sights[a.id]


async def look_all(msgs):
    """Has glm-5.3-flash look at every image on these messages, all at once."""
    await asyncio.gather(*(look(a) for p in msgs for a in p.attachments
                           if (a.content_type or "").startswith("image/") and a.size < 10_000_000))


def noted(attachments, sep=" "):
    """Files as a prompt shows them, with what glm-5.3-flash saw in each image."""
    return "".join(f"{sep}[attached: {a.filename}" + (f", showing {sights[a.id]}" if sights.get(a.id) else "") + "]"
                   for a in attachments)


def listed(msgs):
    """Messages numbered for a prompt, with their authors, links and files."""
    return "\n".join(f"[{i}] {gh_name(p.author)} ({p.jump_url}): {MENTION.sub('', p.content).strip()}{noted(p.attachments)}"
                     for i, p in enumerate(msgs, 1))


def chosen(d, msgs):
    """The messages the model's "from" picks, counting from 1, however it writes the numbers."""
    return [msgs[i - 1] for i in dict.fromkeys(int(x) for x in re.findall(r"\d+", str(d.get("from") or ""))) if 1 <= i <= len(msgs)]


async def which_issue(target, note):
    """The open issue a message says is fixed or done, as the model reads it."""
    first = next(iter(PRODUCTS))
    listing = "\n".join(f"{k} ({i['type']}) {i['title']}" for k, i in state["issues"].items() if i["open"]) or "none"
    try:
        d = await ask(f"""This Discord message says a bug is fixed or a request is done. Which open issue is it about? Reply with only JSON like {{"issue": "{first}#12"}}, or {{"issue": null}} when none clearly matches.

{gh_name(target.author)}: {MENTION.sub('', target.content).strip()}
{note}

Open issues:
{listing}""", STRONG)
    except Exception as e:
        print("which_issue failed:", repr(e))
        return None
    return norm(first, d.get("issue"))


def can_manage(m):
    return getattr(getattr(m.author, "guild_permissions", None), "manage_threads", False)


async def confirmed(m, question):
    """Asks for a ✅ from the person who gave the command, within two minutes."""
    ask_msg = await m.reply(question + "\nReact ✅ within two minutes.")
    await ask_msg.add_reaction("✅")
    try:
        await bot.wait_for("reaction_add", timeout=120,
                           check=lambda r, u: u.id == m.author.id and r.message.id == ask_msg.id and str(r.emoji) == "✅")
        return True
    except asyncio.TimeoutError:
        return False


async def forget(key):
    """Drops an issue that no longer exists on GitHub, with its thread."""
    if state["issues"][key].get("thread"):
        try:
            await (await thread_of(key)).delete()
        except discord.NotFound:
            pass
    state["issues"].pop(key, None)
    save()


async def delete_command(m):
    """`@devBot delete`: an issue filed in error goes from GitHub and Discord for good, rather than being closed."""
    note = DELETE.sub("", MENTION.sub("", m.content).strip()).strip()
    here = thread_issue(m.channel.id)
    key = named_issue(note, here) or here
    if not key:
        return "Which issue? Tag me in its thread, or name it, like `@devBot delete FSVR#81`."
    if key not in state["issues"]:
        return f"I don't track an issue {key}."
    if not can_manage(m):
        return "Deleting an issue needs permission to manage threads."
    title_ = state["issues"][key]["title"]
    if not await confirmed(m, f"Delete **{key}**: {title_} from GitHub and Discord? It can't be undone."):
        return f"Kept **{key}**."
    product, n = key.split("#")
    async with lock:
        x = await gh("GET", f"/repos/{PRODUCTS[product]}/issues/{n}")
        # GitHub's REST API can't delete an issue, its GraphQL API can, for a repo admin
        d = await gh("POST", "/graphql", as_james=True,  # GitHub lets only a repo admin delete, which the app can't be
                     json={"query": "mutation($id: ID!) { deleteIssue(input: {issueId: $id}) { clientMutationId } }",
                           "variables": {"id": x["node_id"]}})
        if d.get("errors"):
            raise RuntimeError(d["errors"][0].get("message", "GitHub refused the deletion"))
        await forget(key)
    await log(f"**{key}** ({title_}) deleted from GitHub and Discord by {gh_name(m.author)}")
    return f"Deleted **{key}**."


def thread_issue(channel_id):
    return next((k for k, i in state["issues"].items() if i.get("thread") == channel_id), None)


def thread_pull(channel_id):
    return next((k for k, i in state["pulls"].items() if i.get("thread") == channel_id), None)


def team(member):
    """James, rgwan, or someone in the Team or Contributors role."""
    return member.id in AGENT_USERS or any(r.id in AGENT_ROLES for r in getattr(member, "roles", []))


async def approve_command(m):
    """`@devBot approve` in a pull request's thread. Approvals hold for the branch's latest commit, and at two from
    different people in the Team and Contributors roles devBot merges it."""
    key = thread_pull(m.channel.id)
    if not key:
        return "Approve a pull request in its thread in the pull-requests forum."
    if not team(m.author):
        return "Pull requests are approved by the Team and Contributors roles."
    product, n = key.split("#")
    repo, info, who = PRODUCTS[product], state["pulls"][key], gh_name(m.author)
    async with pulls_lock:
        pr = await gh("GET", f"/repos/{repo}/pulls/{n}")
        if pr["state"] != "open":
            return f"**{key}** is already {'merged' if pr.get('merged_at') else 'closed'}."
        if info["sha"] != pr["head"]["sha"]:  # pushed to since the sync saw it, so earlier approvals were for other code
            info.update(sha=pr["head"]["sha"], approvals={})
        info["approvals"] = authored(pr) | info["approvals"]
        if str(m.author.id) not in info["approvals"]:
            info["approvals"][str(m.author.id)] = who
            await gh("POST", f"/repos/{repo}/issues/{n}/comments", json={"body": (
                f"Approved on Discord by {who} ({m.jump_url}), {len(info['approvals'])} of {APPROVALS}, "
                f"at {info['sha'][:7]}.\n{MARK}")})
        save()
        await show_pull(key, pr)
        names = " and ".join(info["approvals"].values())
        if len(info["approvals"]) < APPROVALS:
            return f"Approved, {len(info['approvals'])} of {APPROVALS}."
        try:  # the sha makes GitHub refuse if the branch moved after the approvals
            await gh("PUT", f"/repos/{repo}/pulls/{n}/merge", json={
                "merge_method": "merge", "sha": info["sha"], "commit_message": f"{pr['title']}\n\nApproved on Discord by {names}."})
        except RuntimeError as e:
            said = re.search(r'"message":\s*"([^"]+)"', str(e))  # GitHub's reason, like "Pull Request is not mergeable"
            why = (said.group(1) if said else str(e))[:300]
            await log(f"**{key}** approved by {names}, but GitHub refused the merge: {why} {m.jump_url}")
            return f"Approved by {names}, but GitHub won't merge it: {why}\nOnce that's fixed, `@devBot approve` tries again."
    await log(f"**{key}** merged by devBot, approved by {names} {m.jump_url}")
    wake.set()  # the sync backs the thread up and deletes it
    return f"Merged **{key}**, approved by {names}."


def named_issue(note, here):
    """The issue a command names, like FSVR#49, or #49 for the product of the thread it's in."""
    if found := KEY.search(note):
        return f"{pick(found.group(1), PRODUCTS, (here or next(iter(PRODUCTS))).split('#')[0])}#{found.group(2)}"


async def add_to_issue(key, attachments, heading):
    product, n = key.split("#")
    files = await stash(product, attachments)
    await gh("POST", f"/repos/{PRODUCTS[product]}/issues/{n}/comments", json={"body": heading + ":\n\n" + "\n".join(files) + "\n" + MARK})


RELAY = "On Discord, "  # how a thread message relayed to GitHub opens, so a handover brief leaves out the copy
relaying = asyncio.Lock()  # one at a time, so a thread's messages reach GitHub in the order they were sent


async def relay(m, key):
    """A message in an issue's or pull request's thread goes to GitHub as a comment, with its files."""
    if m.type not in (discord.MessageType.default, discord.MessageType.reply) or not (m.clean_content or m.attachments):
        return
    product, n = key.split("#")
    quoted = "\n".join(f"> {line}" for line in m.clean_content.splitlines())
    async with relaying:
        files = await stash(product, m.attachments) if m.attachments else []
        await gh("POST", f"/repos/{PRODUCTS[product]}/issues/{n}/comments", json={"body": "\n".join(
            [f"{RELAY}**{gh_name(m.author)}** wrote ({m.jump_url}):", "", quoted, "", *files, MARK])})


async def add_command(m):
    """`@devBot add image`: the files on the message, or on the one it replies to, go to an issue and its thread."""
    note = ADD.sub("", MENTION.sub("", m.content).strip()).strip()
    target = await replied_to(m)
    attachments = [*target.attachments, *(m.attachments if target is not m else [])]
    if not attachments:
        return "I don't see any files. Attach them to your message, or reply to the message that has them."
    here = thread_issue(m.channel.id)
    key = named_issue(note, here) or here
    if not key:
        return "Which issue? Tag me in its thread, or name it, like `@devBot add image FSVR#80`."
    if key not in state["issues"] or not state["issues"][key]["open"]:
        return f"I couldn't find an open issue {key}."
    n = len(attachments)
    async with lock:
        await add_to_issue(key, attachments, f"Added from Discord by {gh_name(m.author)} ({target.jump_url})")
        if m.channel.id != state["issues"][key].get("thread"):  # posted elsewhere, so the thread gets copies
            await post(key, f"{gh_name(target.author)} added {n} file{'s' * (n != 1)}: {target.jump_url}", attachments)
    await log(f"{gh_name(m.author)} added {n} file{'s' * (n != 1)} to **{key}** {link(key)} {m.jump_url}")
    return f"Added {n} file{'s' * (n != 1)} to **{key}** and its GitHub issue."


async def revise(m, key):
    """A tag naming an open issue, or in its thread, asking to change it: correct, reword or retitle it, or add details
    or another message to it. Returns None when the model reads the tag as a new report instead."""
    note = MENTION.sub("", m.content).strip()
    found = await sources(m)
    before = [] if found else await earlier(m)
    await look_all([*found, *before, m])
    product, n = key.split("#")
    repo, info, who = PRODUCTS[product], state["issues"][key], gh_name(m.author)
    issue = await gh("GET", f"/repos/{repo}/issues/{n}")
    shown = (f"Messages it points at:\n{listed(found)}" if found else
             f'Earlier messages in the channel, oldest first, for when it points at one ("the above"):\n{listed(before)}' if before else "")
    d = await ask(f"""{who} tagged devBot, which keeps the GitHub issues for {', '.join(PRODUCTS)}, in this Discord message:
{note}{noted(m.attachments)}

{shown}

The issue {key}, filed as a {info['type']} in category {info['category']} with importance {info['importance']}:
Title: {issue['title']}
Body:
{issue.get('body') or ''}

If the message asks to change this issue (correct it, reword or retitle it, add details or another message's content to it), apply exactly what it asks, in the title and the body. Copy the rest of the body unchanged, links and images included, and leave quoted messages (lines starting with >) as their authors wrote them. Credit what you add from a message to its author with its link, like the body's "Original report" line. If the description is a placeholder or wrong and the message supplies the real report, rewrite the whole body from that message instead, with an "Original report" line crediting its author and quoting it.
Reply with only JSON, one of:
{{"action": "edit", "title": "the whole new title", "body": "the whole new body in markdown", "category": one of {CATEGORIES}, "importance": one of {IMPORTANCE}, "summary": "what changed, in one short sentence"}}
{{"action": "new"}} when the message reports a different bug or request instead
{{"action": "task"}} when it asks for work on the issue beyond changing its text, like research, reading the manual, testing or code
{{"action": "none"}} when it asks for anything else, like closing the issue or a question about it""", STRONG)
    if d.get("action") == "new":
        return None
    if d.get("action") == "task":
        return await handover(m, key)
    if d.get("action") != "edit" or not d.get("body"):
        return f"I can change **{key}** when you tag me with the change, like `@devBot reword {key} to say ...`, or close it with `@devBot close {key}`."
    attachments = [*(a for p in found for a in p.attachments), *m.attachments]
    summary = str(d.get("summary") or "Updated the issue.")[:300]
    heading = f"Updated from Discord by {who} ({m.jump_url}): {summary}"
    async with lock:
        item = await gh("PATCH", f"/repos/{repo}/issues/{n}", json={"title": str(d.get("title") or issue["title"])[:200], "body": d["body"]})
        info["category"] = pick(d.get("category"), CATEGORIES, info["category"])
        info["importance"] = pick(d.get("importance"), IMPORTANCE, info["importance"])
        await update_issue(key, item)  # the thread's title, first post and tags follow
        save()
        if attachments:
            await add_to_issue(key, attachments, heading + "\n\nFiles")
        else:
            await gh("POST", f"/repos/{repo}/issues/{n}/comments", json={"body": f"{heading}\n{MARK}"})
        if m.channel.id != info.get("thread"):
            await post(key, heading, attachments)
    await log(f"{who} updated **{key}** {link(key)}: {summary} {m.jump_url}")
    return f"Updated **{key}**: {summary} {link(key)}"


async def handover(m, key):
    """Tagging claudeOpus in an issue's thread, or asking devBot for work on an issue, hands the issue to claudeOpus in
    #scratchpad, with all its thread holds."""
    if not team(m.author):
        return "claudeOpus takes work from the Team and Contributors roles, so someone there needs to hand this over."
    product, n = key.split("#")
    repo = PRODUCTS[product]
    issue = await gh("GET", f"/repos/{repo}/issues/{n}")
    comments = await gh("GET", f"/repos/{repo}/issues/{n}/comments", params={"per_page": 100})
    asked = re.sub(r"@(claudeOpus|devBot)\b", "", m.clean_content).strip()
    who = gh_name(m.author)
    thread = await thread_of(key)
    brief = [f"# {key}: {issue['title']}", "", f"- GitHub: {issue['html_url']}", f"- Discord thread: {thread.jump_url}",
             f"- Handed over by {who} (Discord {m.author.id}): {asked or 'no note'}", "",
             f"## The issue, opened by {(issue.get('user') or {}).get('login', 'ghost')} on {issue['created_at'][:10]}", "",
             issue.get("body") or "(no description)", "", "## GitHub comments", ""]
    # without devBot's marker, which claudeOpus would otherwise copy into its own comments and hide them from the thread
    brief += [f"**{c['user']['login']}** ({c['created_at'][:10]}): {c['body'].replace(MARK, '').strip()}\n" for c in comments
              if not (c["body"] or "").startswith(RELAY)] or ["none", ""]  # relayed thread messages are in the thread below
    brief += ["## The Discord thread", ""]
    files = []
    async for p in thread.history(limit=None, oldest_first=True):
        if p.id == m.id:
            continue
        names = "".join(f" [file: {a.filename}]" for a in p.attachments)
        brief.append(f"**{gh_name(p.author)}** ({p.created_at:%Y-%m-%d}): {p.clean_content}{names}\n")
        files += p.attachments
    doc = discord.File(io.BytesIO("\n".join(brief).encode()), f"{key.replace('#', '-')}-brief.md")
    # the post goes where it was asked for, so claudeOpus works there, or to #scratchpad from a channel claudeOpus doesn't read
    inplace = bool({m.channel.id, getattr(m.channel, "parent_id", None)} & OPUS_CHANNELS)
    here = m.channel if inplace else bot.get_channel(SCRATCHPAD)
    if inplace and not isinstance(here, discord.Thread):  # claudeOpus works in a thread under the request itself
        here = await m.create_thread(name=f"{key} {issue['title']}"[:100])
    sent = await here.send(
        # the first line names claudeOpus's thread, and the tag must be in the text for Hermes to take a bot's message
        f"{key} {issue['title']}"[:150] + f"\n<@{CLAUDE_OPUS}> {asked[:1400] or 'please take this on'}\n"
        f"Handed over by {m.author.mention} from {m.jump_url}. The issue, its GitHub comments and its Discord thread "
        f"are in the attached brief: <{issue['html_url']}>",
        files=[doc, *(await as_files(files))[:9]],
        # the person who handed it over is tagged too, so claudeOpus's thread includes them
        allowed_mentions=discord.AllowedMentions(users=[discord.Object(CLAUDE_OPUS), m.author]))
    # claudeOpus opens its thread on the post, and a thread opened on a message takes the message's id
    handed[sent.id] = m.author.id
    if isinstance(opened := bot.get_channel(sent.id), discord.Thread):  # it may have opened while this was sending
        await join(opened)
    await log(f"{who} handed **{key}** to claudeOpus {sent.jump_url}")
    return "" if inplace else f"Handed to claudeOpus in #scratchpad: {sent.jump_url}"


handed = {}  # a handover post's id -> who handed it over, until claudeOpus opens its thread on it


async def join(thread):
    """Adds the person who handed an issue over to the thread claudeOpus opened for it."""
    if who := handed.pop(thread.id, None):
        try:
            await thread.add_user(discord.Object(who))
        except discord.HTTPException as e:
            print("couldn't add", who, "to thread", thread.id, repr(e))


@bot.event
async def on_thread_create(thread):
    await join(thread)


async def close_command(m):
    """`@devBot close` in an issue's thread, with an issue named, or in reply to a message that says it's fixed."""
    note = CLOSE.sub("", MENTION.sub("", m.content).strip()).strip()
    target = await replied_to(m)
    here = thread_issue(m.channel.id)
    if named := named_issue(note, here):
        key = named
    elif here or target is not m:
        key = here or await which_issue(target, note)
    else:
        return "Which issue? Reply to the message that says it's fixed, tag me in the issue's thread, or name it, like `@devBot close FSVR#49`."
    if key not in state["issues"] or not state["issues"][key]["open"]:
        return f"I couldn't find an open issue {key if named else 'for that'}. Name it, like `@devBot close FSVR#49`."
    product, n = key.split("#")
    info, url = state["issues"][key], f"https://github.com/{PRODUCTS[product]}/issues/{n}"
    quote = "\n> " + MENTION.sub("", target.content).strip().replace("\n", "\n> ")[:1500] if target is not m and target.content else ""
    who = gh_name(m.author)
    if not can_manage(m):
        await post(key, f"{who} says this is resolved: {target.jump_url}{quote}")
        await log(f"{who} says **{key}** is resolved {target.jump_url}, a maintainer can close it")
        return f"Noted on **{key}** {link(key)}. Someone who can manage threads can close it with `@devBot close`."
    # the model picked the issue, so its pick needs a yes before anything closes
    if not (named or here) and not await confirmed(m, f"That looks like **{key}**: {info['title']} <{url}>\nClose it?"):
        return f"Left **{key}** open."
    await m.reply(f"Closing **{key}**: {info['title']} <{url}>")  # before its thread, maybe this one, is backed up and deleted
    async with lock:
        await post(key, f"Closed from Discord by {who}: {target.jump_url}{quote}")
        await close_issue(key, f"Closed from Discord by {who} ({target.jump_url}).{quote}")
        save()
    await log(f"**{key}** closed from Discord by {who} {m.jump_url}")


async def triage(text, private=False, before=()):
    """Sorts a report and checks it against the open issues. Returns the model's verdict, its product, and the open
    issue it duplicates, if any, for each separate bug or request in the report. `private` keeps the sender's personal
    details out of the issue body. `before` holds the messages just above a tag, and each verdict's "from" lists the
    ones that report comes from."""
    first = next(iter(PRODUCTS))
    listing = "\n".join(f"{k} ({i['type']}) {i['title']}" for k, i in state["issues"].items() if i["open"]) or "none"
    above = f'''
"from": the numbers of the earlier messages below that discuss this report's problem, like [3, 7, 8]: the ones the tag points at ("the above", "this") and any others about the same problem, even when the tag only names it ("add the 12 kHz notch to the bug list"). [] when none do. Write the title and body from those messages and the report together, with every detail they give, and leave out talk about something else.

Earlier messages in the channel, oldest first. Always check them for discussion of the report's problem:
{listed(before)}
''' if before else ""
    # ponytail: every open title goes into the prompt, switch to embeddings past a few hundred open issues
    verdicts = await classify(text, f'''"product": one of {list(PRODUCTS)}
"duplicate_of": the key (like {first}#12) of the open issue below that reports the same problem or asks for the same thing, or null
"title": a short issue title
"body": the report rewritten as a clear GitHub issue body in markdown, adding no details the reporter didn't give{
", and leaving out names, email addresses, phone numbers and anything else personal" if private else ""}
"task": true when this asks for work on an open issue below, like research, reading the manual, testing or code, rather than reporting something, with that issue as duplicate_of; otherwise false
{above}
Open issues:
{listing}''', STRONG, many=True)  # the duplicate call decides whether a report gets its own issue
    out = []
    for d in verdicts:
        product = pick(d.get("product"), PRODUCTS, first)
        dup = norm(product, d.get("duplicate_of"))
        out.append((d, product, dup if dup in state["issues"] and state["issues"][dup]["open"] else None))
    return out


# email addresses and phone numbers, international with + or 604-555-0199 style, but not dates or versions
PERSONAL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\+\d[\d\s().-]{7,}\d|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")


async def contact(m):
    """A message from the website's contact form, posted into #contact-inbox by its webhook. glm-5.3-flash sorts
    the bug reports and feature requests from the rest, and a report is filed like one from Discord, without the
    sender's details, since the issue is public. The inbox message gets a reaction saying what happened."""
    e = m.embeds[0] if m.embeds else None
    text = f"{e.title or ''}\n\n{e.description or ''}".strip() if e else m.content
    products = "\n".join(f"- {p}: {d}" for p, d in about.items())
    try:
        d = await ask(f"""This message came through the contact form on musica.studio, the website for these products:
{products}

Is it a bug report or a feature request about one of them, rather than a question, a business enquiry, a thank-you or spam?
Reply with only JSON like {{"report": true, "why": "a few words"}}.

{text}""")  # flash: most of what comes through the form is not a report
    except Exception as ex:
        print("contact check failed:", repr(ex))
        return
    if str(d.get("report")).lower() not in ("true", "yes"):
        await m.add_reaction("➖")
        await log(f"Contact form message {m.jump_url} isn't a bug report or feature request ({str(d.get('why', ''))[:120]})")
        return
    async with lock:
        for verdict, product, dup in await triage(text, private=True):
            summary = PERSONAL.sub("[removed]", verdict.get("body") or "")
            if dup:
                await post(dup, f"Also reported through the musica.studio contact form:\n>>> {summary[:1700]}")
                await m.add_reaction("🔁")
                await log(f"Contact form message {m.jump_url} matches **{dup}**, added to {link(dup)}")
                continue
            item = await gh("POST", f"/repos/{PRODUCTS[product]}/issues", json={
                "title": PERSONAL.sub("[removed]", verdict.get("title") or "Report from the contact form")[:200],
                "body": f"{summary}\n\nReported through the musica.studio contact form.",
                "labels": ["bug" if verdict["type"] == "bug" else "enhancement"]})
            key = f"{product}#{item['number']}"
            await open_thread(key, item, verdict | {"product": product})
            await m.add_reaction("🆕")
            await log(f"Contact form message {m.jump_url} filed as **{key}** <{item['html_url']}>")


async def report(m):
    note = LINK.sub("", MENTION.sub("", m.content)).strip()
    found = await sources(m)
    before = await earlier(m)  # always, since a short tag often names a problem discussed just above it
    await look_all([*found, *before, m])

    def described(found):
        """The report's text and files: the messages it points at with the tagger's note, or the note alone. The text is
        what gets quoted, so the files and what glm-5.3-flash saw in them go to the model separately, through noted()."""
        if found:
            who = (lambda p: f"{gh_name(p.author)}: ") if len(found) > 1 else (lambda p: "")  # a discussion keeps its speakers
            text = "\n\n".join(who(p) + MENTION.sub("", p.content).strip() for p in found)
            text += f"\n\nNote from {gh_name(m.author)}: {note}" if note else ""
        else:
            text = note
        attachments = [*(a for p in found for a in p.attachments), *m.attachments]
        return text, attachments

    text, attachments = described(found)
    if not (text.strip() or attachments):
        return "Reply to a message that describes the bug or request, link it, or write it after the tag."

    async with lock:
        replies = []
        for d, product, dup in await triage(text + noted(attachments, "\n"), before=before):  # one per separate bug or request
            if dup and str(d.get("task")).lower() == "true":  # "in that issue, go through the manual and ...": work for claudeOpus
                replies.append(await handover(m, dup))
                continue
            src = found + [p for p in chosen(d, before) if p not in found]  # with the earlier messages about the same problem
            text, attachments = described(src)
            src = src or [m]
            mentions = " and ".join(dict.fromkeys(p.author.mention for p in src))
            who = " and ".join(dict.fromkeys(gh_name(p.author) for p in src))
            links = " ".join(p.jump_url for p in src)
            if dup:
                await post(dup, f"Also reported by {mentions} in {links}\n>>> {text[:1500]}", attachments)
                if attachments:  # the GitHub issue gets the new screenshots too
                    await add_to_issue(dup, attachments, f"Also reported on Discord by {who} ({links})")
                await log(f"{who}'s report {links} matches **{dup}**, added to {link(dup)}")
                replies.append(f"Already tracked as **{dup}**: {link(dup)}")
                continue

            quoted = text.replace("\n", "\n> ")
            files = "\n\n" + "\n".join(await stash(product, attachments)) if attachments else ""
            item = await gh("POST", f"/repos/{PRODUCTS[product]}/issues", json={
                "title": (d.get("title") or (text.strip() or "Report from Discord").splitlines()[0])[:200],
                "body": f"{d.get('body') or ''}\n\n**Original report** by {who} on Discord ({links}):\n> {quoted}{files}",
                "labels": ["bug" if d["type"] == "bug" else "enhancement"]})
            key = f"{product}#{item['number']}"
            th = await open_thread(key, item, d | {"product": product}, attachments)
            await log(f"{who}'s report {links} filed as **{key}** <{item['html_url']}>")
            replies.append(f"Filed **{key}**: <#{th.id}> · <{item['html_url']}>")
        return "\n".join(filter(None, replies))


@bot.event
async def setup_hook():
    global http
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))  # glm-5.3 can take over three minutes
    await load_about()
    bot.sync_task = asyncio.create_task(sync_forever())


@bot.event
async def on_ready():
    print("logged in as", bot.user, "in", [g.name for g in bot.guilds])


@bot.event
async def on_message(m):
    if m.channel.id == GH_EVENTS:
        wake.set()
        return
    if m.channel.id == CONTACT:
        if m.webhook_id:  # the website's form, not people talking in the inbox
            await contact(m)
        return
    if not m.author.bot and (key := thread_issue(m.channel.id)) and re.search(rf"<@!?{CLAUDE_OPUS}>", m.content):
        return await answer(m, handover(m, key))
    me = [f"<@{bot.user.id}>", f"<@!{bot.user.id}>"]
    if m.guild and m.guild.self_role:
        me.append(f"<@&{m.guild.self_role.id}>")
    # a reply to devBot pings it without the tag being in the text, so check the text itself
    if m.author.bot:
        return
    if not any(t in m.content for t in me):
        if key := thread_issue(m.channel.id) or thread_pull(m.channel.id):
            try:
                await relay(m, key)
            except Exception as e:
                print("relay failed:", repr(e))
        return
    await answer(m, command(m))


async def command(m):
    said = MENTION.sub("", m.content).strip()
    if CLOSE.match(said):
        return await close_command(m)
    if ADD.match(said):
        return await add_command(m)
    if DELETE.match(said):
        return await delete_command(m)
    if APPROVE.match(said):
        return await approve_command(m)
    here = thread_issue(m.channel.id)
    key = named_issue(said, here) or here
    async with m.channel.typing():
        # naming an open issue, or tagging in its thread, asks to change it, unless the model reads a new report
        return (state["issues"].get(key, {}).get("open") and await revise(m, key)) or await report(m)


async def answer(m, work):
    """Runs a command and replies. The command stays in state["inbox"] until then, so one a restart cuts off runs again
    on startup, and one that waits, behind a sync say, shows ⏳ meanwhile."""
    state["inbox"][str(m.id)] = m.channel.id
    save()
    task = asyncio.ensure_future(work)
    waits = not (await asyncio.wait({task}, timeout=3))[0]
    if waits:
        await m.add_reaction("⏳")
    try:
        reply = await task
    except Exception as e:
        print("command failed:", repr(e))
        reply = f"Couldn't do that: {e}"[:2000]
    try:
        if reply:
            await m.reply(reply)
        if waits:
            await m.remove_reaction("⏳", bot.user)
    except discord.HTTPException:
        pass  # the thread it was asked in may have just been backed up and deleted
    state["inbox"].pop(str(m.id), None)
    save()


async def replay():
    """Runs again the commands a restart cut off."""
    for mid, cid in list(state["inbox"].items()):
        try:
            m = await (bot.get_channel(cid) or await bot.fetch_channel(cid)).fetch_message(int(mid))
        except discord.HTTPException:  # deleted since
            state["inbox"].pop(mid, None)
            continue
        print("replaying", mid, m.content[:80])
        asyncio.create_task(on_message(m))


async def preview():
    """Prints how each open issue would be sorted, without touching Discord."""
    global http
    async with aiohttp.ClientSession() as http:
        await load_about()
        for product, repo in PRODUCTS.items():
            for item in await gh_all(f"/repos/{repo}/issues", state="open"):
                if "pull_request" not in item:
                    d = await classify(issue_text(item))
                    print(f"{product}#{item['number']:<4} {d['type']:<8} {d['category']:<9} {d['importance']:<9} {item['title'][:70]}")


if __name__ == "__main__":
    if sys.argv[1:] == ["preview"]:
        asyncio.run(preview())
    else:
        bot.run(os.environ["DISCORD_TOKEN"])
