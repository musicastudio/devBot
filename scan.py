"""One-off scan of the server's history for unresolved bugs and requests.

python scan.py fetch    saves every message outside the skipped channels to scan_messages.json
python scan.py extract  has the model list the bugs and requests in them, saved to scan_items.json
python scan.py plan     matches them to issues and groups the rest, saved to scan_plan.json and scan_report.md
python scan.py apply    posts the matches into their threads and files the new groups (stop the service first)
python scan.py attach   copies the files on the quoted Discord messages onto their threads and GitHub issues (stop it too)
"""
import re
import asyncio
import json
import sys

import aiohttp
import discord

import bot

SKIP = {1557447421926187118, 1555629515143651498, bot.GH_EVENTS, *bot.FORUMS.values()}  # scratchpad, vop3-ai, gh-events, forums
MESSAGES = bot.HERE / "scan_messages.json"
ITEMS = bot.HERE / "scan_items.json"
PLAN = bot.HERE / "scan_plan.json"
REPORT = bot.HERE / "scan_report.md"
FIRST = next(iter(bot.PRODUCTS))
CHUNK = 30_000  # characters of conversation per model call
OVERLAP = 15  # messages repeated between chunks so a report and its answer can land together


def record(m):
    text = m.content
    text += "".join(f"\n[embed] {e.title or ''} {e.description or ''}" for e in m.embeds if e.title or e.description)
    text += "".join(f"\n[file] {a.filename}" for a in m.attachments)
    return {"id": m.id, "author": m.author.display_name, "bot": m.author.bot, "at": m.created_at.strftime("%Y-%m-%d %H:%M"),
            "reply_to": m.reference.message_id if m.reference else None, "url": m.jump_url, "text": text.strip()}


async def fetch():
    g = bot.bot.guilds[0]
    places = [c for c in g.channels if c.id not in SKIP and isinstance(c, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel))]
    for c in g.channels:
        if c.id not in SKIP and isinstance(c, (discord.TextChannel, discord.ForumChannel)):
            places += c.threads
            places += [t async for t in c.archived_threads(limit=None)]
            if isinstance(c, discord.TextChannel):
                places += [t async for t in c.archived_threads(limit=None, private=True)]
    out = {}
    for c in dict.fromkeys(places):
        name = f"{c.parent.name} / {c.name}" if isinstance(c, discord.Thread) else c.name
        try:
            msgs = [record(m) async for m in c.history(limit=None, oldest_first=True) if m.content or m.embeds or m.attachments]
        except discord.Forbidden:
            print("no access:", name)
            continue
        out[str(c.id)] = {"name": name, "messages": msgs}
        print(f"{len(msgs):>6}  {name}")
    MESSAGES.write_text(json.dumps(out))
    print(sum(len(c["messages"]) for c in out.values()), "messages in", len(out), "channels and threads")


def line(i, m):
    text = m["text"][:200] + "…" if m["bot"] and len(m["text"]) > 200 else m["text"]
    return f"[{i}] {m['at']} {m['author']}{' (bot)' if m['bot'] else ''}: {text}"


def chunks(msgs):
    start = 0
    while start < len(msgs):
        end, size = start, 0
        while end < len(msgs) and (size < CHUNK or end == start):
            size += len(line(end, msgs[end]))
            end += 1
        yield start, end
        if end == len(msgs):
            return
        start = max(end - OVERLAP, start + 1)


async def extract():
    await bot.load_about()
    data = json.loads(MESSAGES.read_text())
    done = json.loads(ITEMS.read_text()) if ITEMS.exists() else {}  # resumable, keyed by channel and chunk
    gate = asyncio.Semaphore(4)  # model calls in flight at once

    async def one(cid, c, start, end):
        msgs = c["messages"]
        key = f"{cid}:{start}"
        async with gate:
            convo = "\n".join(line(i, msgs[i]) for i in range(start, end))
            prompt = f"""These are messages from #{c['name']} on the musica.studio Discord, where people discuss {', '.join(f'{p} ({d})' for p, d in bot.about.items())}. Messages marked (bot) are AI coding agents reporting on their work, cut short.

List every bug report and feature request about the software in these messages. Skip general chat, research notes and work tasks that are not a bug or a request. For each one give:
"summary": one sentence on what is wrong or wanted, specific enough to match against an issue tracker
"type": "bug" or "feature"
"messages": the [numbers] of up to five messages that report it or add detail, the first report first
"status": "resolved" if later messages show it was fixed, answered or not a bug, otherwise "open"
"note": a few words on how it was resolved or where it stands
Reply with only JSON like {{"items": [...]}}, and {{"items": []}} if there are none.

{convo}"""
            for attempt in range(3):
                try:
                    items = (await bot.ask(prompt)).get("items") or []
                    break
                except Exception as e:
                    print(f"  retry {attempt + 1} on {c['name']} {start}: {e!r}"[:300])
                    await asyncio.sleep(20)
            else:
                return  # left out of done, so a rerun picks it up
            for it in items:
                idx = [i for i in it.get("messages") or [] if isinstance(i, int) and start <= i < end]
                it["links"] = [msgs[i]["url"] for i in idx]
                it["at"] = msgs[idx[0]]["at"] if idx else msgs[start]["at"]
                it["channel"] = c["name"]
            done[key] = items
            ITEMS.write_text(json.dumps(done, indent=1))
            print(f"{len(items):>3} items  {c['name'][:60]} [{start}:{end}]", flush=True)

    jobs = [(cid, c, s, e) for cid, c in data.items() if c["messages"] for s, e in chunks(c["messages"]) if f"{cid}:{s}" not in done]
    print(len(jobs), "chunks to read", flush=True)
    await asyncio.gather(*(one(*j) for j in jobs))
    print(sum(len(v) for v in done.values()), "items from", len(done), "chunks")


async def ask(prompt):  # matching and grouping decide what gets filed, so they use the stronger model
    for attempt in range(3):
        try:
            return await bot.ask(prompt, bot.STRONG)
        except Exception as e:
            print(f"  retry {attempt + 1}: {e!r}"[:300])
            await asyncio.sleep(20)
    raise RuntimeError("the model failed three times")


def entry(it):
    return f"- {it['summary']} (#{it['channel'][:50]}, {it['at'][:10]}) " + " ".join(it["links"])


async def plan():
    await bot.load_about()
    # a thread a coding agent posts in is its working session, and its outcome reaches devBot as commits
    agent = {c["name"] for c in json.loads(MESSAGES.read_text()).values() if " / " in c["name"] and any(m["bot"] for m in c["messages"])}
    items, firsts = [], set()
    for it in (it for chunk in json.loads(ITEMS.read_text()).values() for it in chunk):
        if it.get("summary") and it.get("links") and it["channel"] not in agent and it["links"][0] not in firsts:  # the chunk overlap finds some twice
            firsts.add(it["links"][0])
            items.append(it | {"id": len(items)})
    issues = {}
    for p, r in bot.PRODUCTS.items():
        issues |= {f"{p}#{x['number']}": x for x in await bot.gh_all(f"/repos/{r}/issues", state="all") if "pull_request" not in x}
    listing = "\n".join(f"{k} ({x['state']}) {x['title']}" for k, x in issues.items())
    products = ", ".join(f"{p} ({d})" for p, d in bot.about.items())

    gate = asyncio.Semaphore(4)

    async def match(batch):
        async with gate:
            d = await ask(f"""Match each Discord report to the GitHub issue that describes the same problem or request, or null when none does. Working on the same area is not enough.

Issues:
{listing}

Reports:
""" + "\n".join(f"{it['id']}. (#{it['channel'][:30]}, {it['type']}) {it['summary']}" for it in batch)
                      + '\n\nReply with only JSON like {"matches": {"0": "' + FIRST + '#12", "1": null}}')
        for it in batch:
            k = bot.norm(FIRST, (d.get("matches") or {}).get(str(it["id"])))
            it["issue"] = k if k in issues else None
        print(f"matched a batch of {len(batch)}", flush=True)

    await asyncio.gather(*(match(items[b:b + 20]) for b in range(0, len(items), 20)))

    loose = {it["id"]: it for it in items if not it["issue"] and it["status"] != "resolved"}

    async def group(ids):
        async with gate:
            d = await ask(f"""These Discord reports match no GitHub issue yet. Group the ones that describe the same problem or request in the same product, and give each group a short issue title and its product. A report can be a group of its own.
Products: {products}. Use "none" for a report about anything else.

""" + "\n".join(f"{i}. (#{loose[i]['channel'][:30]}, {loose[i]['type']}) {loose[i]['summary']} [{loose[i].get('note', '')}]" for i in ids)
                          + '\n\nReply with only JSON like {"groups": [{"title": "...", "product": "' + FIRST + '", "items": [3, 7]}]}')
        print(f"grouped a batch of {len(ids)}", flush=True)
        return [{"title": str(g.get("title") or "")[:200], "product": bot.pick(g.get("product"), bot.PRODUCTS, "none"),
                 "items": [i for i in g.get("items") or [] if i in ids]} for g in d.get("groups") or []]

    ids = list(loose)  # ponytail: batches of 100 keep each call inside the timeout, a merge pass joins groups across them
    batches = await asyncio.gather(*(group(ids[b:b + 100]) for b in range(0, len(ids), 100)))
    raw = [g for b in batches for g in b if g["items"]]
    if len(batches) > 1:
        d = await ask("These issue titles came from separate batches. List the groups that describe the same problem or request "
                      "in the same product, so they can be merged.\n\n" + "\n".join(f"{n}. {g['product']}: {g['title']}" for n, g in enumerate(raw))
                      + '\n\nReply with only JSON like {"merge": [[0, 5], [3, 9, 12]]}, or {"merge": []} if none.')
        for m in d.get("merge") or []:
            m = [n for n in m if isinstance(n, int) and 0 <= n < len(raw) and raw[n]]
            for n in m[1:]:
                raw[m[0]]["items"] += raw[n]["items"]
                raw[n] = None
    groups, placed = [], set()
    for g in filter(None, raw):
        if gi := [i for i in g["items"] if i not in placed]:
            placed.update(gi)
            groups.append(g | {"items": gi, "title": g["title"] or loose[gi[0]]["summary"][:200]})
    groups += [{"title": it["summary"][:200], "items": [i], "product": "none"} for i, it in loose.items() if i not in placed]

    async def sort(g):
        async with gate:
            g |= await bot.classify("\n".join(loose[i]["summary"] for i in g["items"]))
    await asyncio.gather(*(sort(g) for g in groups))
    groups = [{k: g[k] for k in ("title", "product", "items", "type", "category", "importance")} for g in groups]

    attach = {}
    for it in items:
        if it["issue"]:
            attach.setdefault(it["issue"], []).append(it["id"])
    PLAN.write_text(json.dumps({"items": items, "attach": attach, "new": groups, "done": []}, indent=1))

    by = {it["id"]: it for it in items}
    out = [f"# Discord scan\n\n{len(items)} reports found. {sum(map(len, attach.values()))} match {len(attach)} existing issues, "
           f"{len(loose)} unmatched open reports form {len(groups)} proposed new issues, and "
           f"{sum(1 for it in items if not it['issue'] and it['status'] == 'resolved')} unmatched reports were resolved in Discord.\n"]
    out.append("## Added to existing issues\n")
    for k in sorted(attach, key=lambda k: (k.split("#")[0], int(k.split("#")[1]))):
        out.append(f"### {k} ({issues[k]['state']}) {issues[k]['title']}\n")
        out += [entry(by[i]) for i in attach[k]] + [""]
    for heading, filed in (("Proposed new issues", True), ("About something without a repo, not filed", False)):
        out.append(f"## {heading}\n")
        for n, g in enumerate(groups):
            if (g["product"] != "none") == filed:
                out.append(f"### N{n}. {g['product']}: {g['title']} ({g['type']}, {g['category']}, {g['importance']})\n")
                out += [entry(by[i]) for i in g["items"]] + [""]
    out.append("## Resolved in Discord, no action\n")
    out += [entry(it) + f" ({it.get('note', '')})" for it in items if not it["issue"] and it["status"] == "resolved"]
    REPORT.write_text("\n".join(out) + "\n")
    print(out[0])


async def post_lines(key, head, lines):
    msg = head
    for ln in lines:
        if len(msg) + len(ln) > 1900:
            await bot.post(key, msg)
            msg = head + " (continued)"
        msg += "\n" + ln[:1800]
    await bot.post(key, msg)


async def apply():
    p = json.loads(PLAN.read_text())
    by = {it["id"]: it for it in p["items"]}
    await bot.load_about()
    await bot.ensure_tags()
    for key, ids in p["attach"].items():
        if key in p["done"]:
            continue
        if key not in bot.state["issues"]:
            print("no thread for", key, "so its", len(ids), "reports stay in the report only")
        else:
            await post_lines(key, "Reported on Discord:", [entry(by[i]) for i in ids])
            print("posted", len(ids), "reports in", key)
        p["done"].append(key)
        PLAN.write_text(json.dumps(p, indent=1))
    for n, g in enumerate(p["new"]):
        if f"N{n}" in p["done"] or g["product"] == "none":
            continue
        lines = [entry(by[i]) for i in g["items"]]
        item = await bot.gh("POST", f"/repos/{bot.PRODUCTS[g['product']]}/issues", json={
            "title": g["title"], "body": "Reported on Discord:\n" + "\n".join(lines),
            "labels": ["bug" if g["type"] == "bug" else "enhancement"]})
        key = f"{g['product']}#{item['number']}"
        await bot.open_thread(key, item, g)
        if len("\n".join(lines)) > 1400:  # the thread's first post cuts the body short, so the links follow in full
            await post_lines(key, "Reported on Discord:", lines)
        p["done"].append(f"N{n}")
        PLAN.write_text(json.dumps(p, indent=1))
    bot.save()
    posted = [k for k in p["attach"] if k in bot.state["issues"]]
    await bot.log(f"Scanned the server's history outside the coding-agent threads: added Discord links to {len(posted)} open "
                  f"issues ({', '.join(posted)}) and filed {sum(1 for g in p['new'] if g['product'] != 'none')} new ones.")


DISCORD_LINK = re.compile(r"https://discord\.com/channels/\d+/(\d+)/(\d+)")


async def attach():
    """The scan quoted Discord messages by link only, so their screenshots and files never reached the issues."""
    await bot.load_about()
    await bot.ensure_tags()
    copied = []
    for key, info in list(bot.state["issues"].items()):
        if not info["open"] or not info.get("thread"):
            continue
        product, n = key.split("#")
        body = (await bot.gh("GET", f"/repos/{bot.PRODUCTS[product]}/issues/{n}")).get("body") or ""
        th = await bot.thread_of(key)
        said = [m.content async for m in th.history(limit=None) if m.author.id == bot.bot.user.id]
        for cid, mid in dict.fromkeys(DISCORD_LINK.findall(body + "\n" + "\n".join(said))):
            mark = f"discord {cid}/{mid}"
            if mark in info.get("images", []):
                continue
            try:
                src = await (bot.bot.get_channel(int(cid)) or await bot.bot.fetch_channel(int(cid))).fetch_message(int(mid))
            except discord.HTTPException as e:
                print(key, "can't read", mark, repr(e))
                continue
            if src.attachments:
                await bot.post(key, f"Files from {src.author.display_name}'s report {src.jump_url}", src.attachments)
                await bot.add_to_issue(key, src.attachments, f"Files from the Discord report by {src.author.display_name} ({src.jump_url})")
                copied.append(key)
                print(key, "got", [a.filename for a in src.attachments])
            info["images"] = [*info.get("images", []), mark]
            bot.save()
    keys = list(dict.fromkeys(copied))
    await bot.log(f"Copied the files from {len(copied)} quoted Discord reports onto their threads and GitHub issues"
                  + (f": {', '.join(keys)}" if keys else "."))


async def setup_hook():
    bot.http = aiohttp.ClientSession()


async def on_message(m):
    pass


async def on_ready():
    try:
        await {"fetch": fetch, "apply": apply, "attach": attach}[sys.argv[1]]()
    finally:
        await bot.http.close()
        await bot.bot.close()


if sys.argv[1] in ("extract", "plan"):  # these work from the saved files, no Discord login
    async def offline():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as bot.http:  # glm-5.3 on a long list is slow
            await {"extract": extract, "plan": plan}[sys.argv[1]]()
    asyncio.run(offline())
else:
    for f in (setup_hook, on_message, on_ready):
        bot.bot.event(f)
    bot.bot.run(bot.os.environ["DISCORD_TOKEN"], log_handler=None)
