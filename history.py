"""devBot's commit check run over past commits, to close issues that commits already resolved.

python history.py        checks every commit since the oldest open issue and saves the verdicts to history.json
python history.py apply  closes what history.json says was resolved, with notes in the threads and the log channel
                         (stop the service first, it shares state.json)
"""
import asyncio
import json
import sys

import aiohttp

import bot

OUT = bot.HERE / "history.json"


async def check():
    await bot.load_about()
    verdicts, total = [], 0
    for product, repo in bot.PRODUCTS.items():
        issues = {f"{product}#{x['number']}": x for x in await bot.gh_all(f"/repos/{repo}/issues", state="open") if "pull_request" not in x}
        if not issues:
            continue
        commits = [c for c in await bot.gh_all(f"/repos/{repo}/commits", since=min(bot.reported(x) for x in issues.values()))
                   if len(c["parents"]) == 1]
        total += len(commits)
        print(f"{product}: {len(commits)} commits against {len(issues)} open issues", flush=True)
        gate = asyncio.Semaphore(4)

        async def one(c):
            when = c["commit"]["committer"]["date"]
            cands = {k: f"({bot.state['issues'].get(k, {}).get('type', 'issue')}) {x['title']}: {bot.gist(x)}"
                     for k, x in issues.items() if bot.reported(x) < when}  # a commit can't resolve an issue reported after it
            if not cands:
                return
            async with gate:
                resolves, works_on = await bot.check_commit(product, c, cands)
            if resolves or works_on:
                verdicts.append({"product": product, "sha": c["sha"][:7], "url": c["html_url"], "date": when,
                                 "subject": c["commit"]["message"].splitlines()[0], "resolves": resolves, "works_on": works_on})
                print(f"{c['sha'][:7]} {when[:10]} resolves {list(resolves)} works on {list(works_on)}", flush=True)

        await asyncio.gather(*(one(c) for c in commits))
    verdicts.sort(key=lambda v: v["date"])
    OUT.write_text(json.dumps({"checked": total, "verdicts": verdicts}, indent=1))
    print(f"\n{total} commits checked. Would close:")
    for k, (v, why) in closes(verdicts).items():
        print(f"  {k} {bot.state['issues'].get(k, {}).get('title', '')[:60]!r}\n    by {v['sha']} {v['date'][:10]} {v['subject'][:90]}\n    because {why}")


def closes(verdicts):
    out = {}
    for v in verdicts:  # the first commit that resolves an issue is the one that closes it
        for k, why in v["resolves"].items():
            out.setdefault(k, (v, why))
    return out


async def apply():
    run = json.loads(OUT.read_text())
    await bot.load_about()
    await bot.ensure_tags()
    lines = []
    for k, (v, why) in closes(run["verdicts"]).items():
        info = bot.state["issues"].get(k)
        if not info or not info["open"]:
            continue
        await bot.post(k, f"Commit [`{v['sha']}`](<{v['url']}>): {v['subject'][:150]} resolved this: {why}\nClosing the GitHub issue.")
        await bot.close_issue(k, f"Resolved by {v['sha']} ({v['url']}). {why}\n\nClosed by devBot.")
        lines.append(f"→ closed **{k}** {bot.link(k)} via [`{v['sha']}`](<{v['url']}>): {why}")
        bot.save()
    msg = f"Checked {run['checked']} past commits against the open issues and closed {len(lines)} they had resolved."
    for ln in lines:
        if len(msg) + len(ln) > 1900:
            await bot.log(msg)
            msg = "(continued)"
        msg += "\n" + ln
    await bot.log(msg)


async def setup_hook():
    bot.http = aiohttp.ClientSession()


async def on_message(m):
    pass


async def on_ready():
    try:
        await apply()
    finally:
        await bot.http.close()
        await bot.bot.close()


if sys.argv[1:] == ["apply"]:
    for f in (setup_hook, on_message, on_ready):
        bot.bot.event(f)
    bot.bot.run(bot.os.environ["DISCORD_TOKEN"], log_handler=None)
else:
    async def offline():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as bot.http:
            await check()
    asyncio.run(offline())
