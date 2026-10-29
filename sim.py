"""Dry run of devBot against the live repo and server. Reads everything, writes nothing to Discord, GitHub or state.json.

python sim.py [days]  runs the populate, replays the last `days` of GitHub activity (default 7) and tries some @devBot reports.
"""
import sys
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace as NS

import aiohttp
import discord

import bot

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
ids = count(1)
bot.save = lambda: None
bot.bot = discord.Client(intents=discord.Intents.default())  # the simulation reads no real messages, so no privileged intent


async def open_thread(key, item, info):
    info = {k: info[k] for k in ("product", "type", "category", "importance")} | {"title": item["title"], "open": True, "thread": next(ids)}
    bot.state["issues"][key] = info
    print(f"  NEW THREAD {info['type']:<8} {key:<10} [{', '.join(t.name for t in bot.applied(info))}] {item['title'][:60]}")
    return NS(id=info["thread"])


async def post(key, text):
    print(f"  POST in {key}: {text[:160]!r}")


async def update_issue(key, item):
    info = bot.state["issues"][key]
    is_open = item["state"] == "open"
    if is_open != info["open"]:
        print(f"  {'REOPEN, new thread with the backup' if is_open else 'CLOSE, thread backed up and deleted'} {key} ({item.get('state_reason')})")
    info.update(open=is_open, title=item["title"])


real_gh = bot.gh


async def gh(method, path, **kw):
    if method == "GET":
        return await real_gh(method, path, **kw)
    j = kw["json"]
    print(f"  GITHUB {method} {path} {str(j)[:400]!r}")
    if method == "PATCH":  # closing an issue
        return await real_gh("GET", path) | j
    return {"number": 9999, "title": j.get("title", ""), "body": j["body"], "html_url": "https://github.com/musicastudio/FSVR/issues/9999",
            "user": {"login": "jameshansen"}, "labels": []}


async def log(text):
    print(f"  LOG {text[:160]!r}")

    async def edit(content=""):
        print(f"      → {content[len(text):].strip()[:400]!r}")
    return NS(content=text, edit=edit)


async def ensure_tags():
    for kind, fid in bot.FORUMS.items():
        forum = bot.bot.get_channel(fid)
        p = forum.permissions_for(forum.guild.me)
        have = {t.name.lower(): t for t in forum.available_tags}
        missing = [n for n in [*bot.PRODUCTS, *bot.CATEGORIES, *bot.IMPORTANCE] if n.lower() not in have]
        print(f"{kind}-list #{forum.name}: send={p.send_messages} create_threads={p.create_public_threads} "
              f"manage_channels={p.manage_channels} would create {len(missing)} tags")
        bot.tags[fid] = have | {n.lower(): NS(name=n) for n in missing}


bot.open_thread, bot.post, bot.update_issue, bot.gh, bot.ensure_tags, bot.log = open_thread, post, update_issue, gh, ensure_tags, log


async def quiet(**_):  # a channel with nothing above the tag
    return
    yield


def msg(text, reply_to=None):
    m = NS(content=text, author=NS(display_name="Tester", mention="@Tester", bot=False), attachments=[],
           jump_url="https://discord.com/channels/0/0/0", reference=None, created_at=datetime.now(timezone.utc),
           channel=NS(history=quiet))
    if reply_to:
        target = msg(reply_to)

        async def fetch(_):
            return target
        m.reference, m.channel = NS(message_id=1, resolved=None), NS(fetch_message=fetch, history=quiet)
    return m


async def setup_hook():
    bot.http = aiohttp.ClientSession()
    await bot.load_about()


async def on_message(m):
    pass  # live mentions are ignored while simulating


async def on_ready():
    try:
        repo = bot.PRODUCTS["FSVR"]
        print("logged in as", bot.bot.user, "in", [g.name for g in bot.bot.guilds])
        logch = bot.bot.get_channel(bot.LOG)
        print("log channel:", logch and f"#{logch.name} send={logch.permissions_for(logch.guild.me).send_messages}")
        await bot.ensure_tags()

        print("\n== 1. populate")
        await bot.sync_issues("FSVR", repo)
        await bot.sync_commits("FSVR", repo)
        print(f"  {len(bot.seen)} existing commits recorded silently")

        print(f"\n== 2. replay of the last {DAYS} days")
        since = bot.now_iso(timedelta(days=DAYS))
        for item in await bot.gh_all(f"/repos/{repo}/issues", state="closed", since=since):
            key = f"FSVR#{item['number']}"
            if "pull_request" not in item and key not in bot.state["issues"]:  # pretend it was still open at the start
                bot.state["issues"][key] = {"product": "FSVR", "type": "bug", "category": "Other", "importance": "Medium",
                                            "title": item["title"], "open": True, "thread": next(ids)}
        bot.state["since"][repo] = bot.state["commits"][repo] = since
        bot.seen.clear()
        await bot.sync_issues("FSVR", repo)
        await bot.sync_commits("FSVR", repo)

        print("\n== 3. @devBot reports")
        me = f"<@{bot.bot.user.id}>"
        for case in [msg(f"{me} imported syx banks never show up in the browser on the mac standalone"),
                     msg(f"{me}", reply_to="FSVR crashes Bitwig 6 on Windows 11 as soon as I open the editor window"),
                     msg(f"{me} this one please", reply_to="would love MPE support so I can use my Osmose with it"),
                     msg(f"{me}")]:
            target = case.channel.fetch_message.__closure__[0].cell_contents.content if case.reference else case.content
            print(f" > {target!r}")
            print("  REPLY", await bot.report(case))
    finally:
        await bot.http.close()
        await bot.bot.close()


for f in (setup_hook, on_message, on_ready):
    bot.bot.event(f)
bot.bot.run(bot.os.environ["DISCORD_TOKEN"], log_handler=None)
