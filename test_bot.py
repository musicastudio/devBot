from bot import ADD, CLOSE, DELETE, KEY, LINK, PERSONAL, REF, body_images, chosen, grounded, named_issue, norm, parse_json, pick, reported

assert PERSONAL.sub("[x]", "mail jo.b+fsvr@example.co.uk, call +1 (604) 555-0199 or 604.555.0199; v0.8.3 since 2026-10-26") == \
    "mail [x], call [x] or [x]; v0.8.3 since 2026-10-26"

assert body_images('a <img width="3" alt="x" src="https://github.com/user-attachments/assets/1" /> b ![s](https://raw.x/y.png)') == \
    ["https://github.com/user-attachments/assets/1", "https://raw.x/y.png"]

assert DELETE.match("delete FSVR#81") and not DELETE.match("deleted presets come back")

assert ADD.match("add image FSVR#80") and ADD.match("Add screenshots") and not ADD.match("additional image bug")

body = "Reported on Discord:\n- a (#fsvr-gui, 2026-10-17) https://x\n- b (#chat, 2026-10-12) https://y"
assert reported({"body": body, "created_at": "2026-10-26T08:42:40Z"}) == "2026-10-12"
assert reported({"body": "plain report", "created_at": "2026-10-03T01:00:00Z"}) == "2026-10-03T01:00:00Z"

assert CLOSE.match("close FSVR#49") and CLOSE.match("Close") and not CLOSE.match("closer look at the filter")
assert [m.groups() for m in KEY.finditer("FSVR#49, FM8.plus#3 and #7")] == [("FSVR", "49"), ("FM8.plus", "3"), (None, "7")]

assert grounded("The CPU readout is busy time over audio", "Fix\n\nThe CPU readout is busy\ntime over audio time.")
assert not grounded("Smooths breath control automation", "Run the channel pitch and noise centres off the phase generator")
assert not grounded("short", "short")

assert REF.findall("Answers #29, C#3 and &#39; aside, (#54: capped) #7.") == ["29", "54", "7"]

assert parse_json('```json\n{"type": "bug"}\n```') == {"type": "bug"}
assert norm("FSVR", 12) == norm("FSVR", "#12") == norm("FSVR", "FSVR#12") == "FSVR#12"
assert norm("FSVR", None) == ""
assert pick(" high", ["Critical", "High"], "Medium") == "High"
assert pick("urgent", ["Critical", "High"], "Medium") == "Medium"

# a tag can link the message it means, or point at one above it, or name the issue to change
assert LINK.findall("from this post: https://discord.com/channels/1/22/333 and <https://ptb.discord.com/channels/1/4/5>") == [("22", "333"), ("4", "5")]
assert chosen({"from": 2}, ["a", "b"]) == ["b"] and chosen({"from": "[1]"}, ["a"]) == ["a"]
assert chosen({"from": [3, "4", 3]}, list("abcd")) == ["c", "d"] and chosen({"from": "1, 2"}, ["a", "b"]) == ["a", "b"]
assert chosen({"from": 0}, ["a"]) == chosen({"from": None}, ["a"]) == chosen({"from": 3}, ["a"]) == chosen({"from": []}, ["a"]) == []
assert named_issue("correct #92's 'full-mesh algorithm' to 'full-mesh custom algorithm'", None) == "FSVR#92"
assert named_issue("update FSVR#91 with information from this post: https://discord.com/channels/1/2/3", None) == "FSVR#91"

# the grace period: a second commit joins the first one's close, and each due close does the right thing
import asyncio
import bot
from datetime import datetime, timedelta, timezone

bot.save = lambda: None  # never touches the real state.json
bot.state["pending"] = {}
first = bot.schedule_close("FSVR#1", "aaa", "u1", "w1")
again = bot.schedule_close("FSVR#1", "bbb", "u2", "w2")
bot.schedule_close("FSVR#1", "bbb", "u2", "w2")
assert [c["sha"] for c in bot.state["pending"]["FSVR#1"]["commits"]] == ["aaa", "bbb"] and again >= first

calls = []


async def gh(method, path, **kw):
    n = path.split("/")[5]  # /repos/musicastudio/FSVR/issues/<n>[/comments]
    if path.endswith("/comments"):  # #2 has a comment since the commit, #3 only devBot's own
        return [{"body": "Fixed in 0.8.4", "user": {"login": "devbot-musicastudio[bot]"}}] if n == "2" else [{"body": "x\n" + bot.MARK}]
    if method == "GET":
        return {"state": "closed" if n == "1" else "open"}  # #1 was closed by someone first
    calls.append((method, n))
    return {}


async def update_issue(key, item): calls.append(("update", key))
async def close_issue(key, comment): calls.append(("close", key, comment.count("\n- ")))
async def log(text): pass

bot.gh, bot.update_issue, bot.close_issue, bot.log = gh, update_issue, close_issue, log
now = datetime.now(timezone.utc)
one = [{"sha": "a", "url": "u", "why": "w"}]
bot.state["pending"] = {k: {"since": "2026-10-26T20:00:00Z", "commits": one, "due": (now - timedelta(minutes=1)).isoformat()}
                        for k in ("FSVR#1", "FSVR#2", "FSVR#3")}
bot.state["pending"]["FSVR#4"] = {"since": "x", "commits": one, "due": (now + timedelta(minutes=10)).isoformat()}
asyncio.run(bot.close_due())
assert calls == [("PATCH", "2"), ("update", "FSVR#2"), ("close", "FSVR#3", 1)], calls
assert list(bot.state["pending"]) == ["FSVR#4"]  # not due yet

# a message in an issue's thread goes to GitHub quoted, and a system message (a rename, say) doesn't
from types import SimpleNamespace as NS
sent = []
async def gh(method, path, **kw): sent.append((path, kw["json"]["body"]))
bot.gh = gh
said = NS(type=bot.discord.MessageType.default, clean_content="still crackles\nat 48k", attachments=[],
          author=NS(id=0, display_name="quadraf"), jump_url="https://discord.com/channels/1/2/3")
asyncio.run(bot.relay(said, "FSVR#91"))
asyncio.run(bot.relay(NS(**{**vars(said), "type": bot.discord.MessageType.channel_name_change}), "FSVR#91"))
assert sent == [("/repos/musicastudio/FSVR/issues/91/comments", "On Discord, **quadraf** wrote (https://discord.com/channels/1/2/3):"
                 "\n\n> still crackles\n> at 48k\n\n" + bot.MARK)], sent

# one tag can hold several reports: the model's list is split, a lone object is one report, and each is filed with its own credit
replies = [{"reports": [{"type": "feature", "category": "ui"}, "junk", {"type": "bug"}]}, {"type": "feature"}]
async def ask(prompt, model=None, images=()): return replies.pop(0)
bot.ask = ask
assert [(d["type"], d["category"]) for d in asyncio.run(bot.classify("x", many=True))] == [("feature", "UI"), ("bug", "Other")]
assert [d["type"] for d in asyncio.run(bot.classify("x", many=True))] == ["feature"]

def said(text, name, url): return NS(content=text, attachments=[], author=NS(id=0, display_name=name, mention=f"@{name}"), jump_url=url)
above = [said("the reverb tail cuts off", "quadraf", "u1"), said("also the filter knob", "rgwan", "u2"), said("and it jumps at 0", "quadraf", "u3")]
replies = [{"reports": [{"from": [1], "title": "Reverb tail cuts off", "product": "FSVR"},
                        {"from": "2, 3", "title": "Filter knob jumps at 0", "product": "FSVR"}]}]
filed = []
async def gh(method, path, **kw):
    filed.append((path, kw["json"]["body"]))
    return {"number": len(filed), "html_url": f"h{len(filed)}"}
async def open_thread(key, item, info, attachments=()): return NS(id=item["number"])
async def sources(m): return []
async def earlier(m): return above
bot.gh, bot.open_thread, bot.sources, bot.earlier = gh, open_thread, sources, earlier
reply = asyncio.run(bot.report(said("<@1> make bug reports for the above", "jameshansen", "u4")))
assert reply == "Filed **FSVR#1**: <#1> · <h1>\nFiled **FSVR#2**: <#2> · <h2>", reply
assert [p for p, _ in filed] == ["/repos/musicastudio/FSVR/issues"] * 2
assert "by quadraf on Discord (u1):\n> the reverb tail cuts off" in filed[0][1]
assert "by rgwan and quadraf on Discord (u2 u3):\n> rgwan: also the filter knob\n> \n> quadraf: and it jumps at 0" in filed[1][1], filed[1][1]

# an image goes to glm-5.3 as what glm-5.3-flash saw in it, and is looked at once
replies = [{"shows": "a spectrum with a deep notch at 12 kHz"}]
async def read(): return b"png"
shot = NS(id=7, filename="a.png", content_type="image/png", size=3, read=read)
asyncio.run(bot.look_all([NS(attachments=[shot, NS(id=8, filename="gap.wav", content_type="audio/wav", size=3)])]))
asyncio.run(bot.look(shot))
assert bot.noted([shot, NS(id=8, filename="gap.wav")], "\n") == "\n[attached: a.png, showing a spectrum with a deep notch at 12 kHz]\n[attached: gap.wav]"

# asking for work on an issue hands it to claudeOpus instead of filing it
bot.state["issues"]["FSVR#95"] = {"open": True, "type": "feature", "title": "Grey out what doesn't apply"}
replies = [{"reports": [{"task": True, "duplicate_of": "#95", "product": "FSVR"}]}]
async def handover(m, key): return f"handed {key}"
bot.handover = handover
assert asyncio.run(bot.report(said("<@1> in that issue, go through the manual", "evildragon", "u5"))) == "handed FSVR#95"

# two people in the Team or Contributors roles merge a pull request, and only at the commit they approved
bot.state["pulls"]["FSVR#99"] = {"product": "FSVR", "sha": "old", "approvals": {"5": "rgwan"}, "thread": 42}
calls = []
async def gh(method, path, **kw):
    calls.append((method, path.rsplit("/", 1)[1], kw.get("json", {}).get("sha")))
    return {"state": "open", "head": {"sha": "new"}, "title": "Perf tooling"} if method == "GET" else {}
async def show_pull(key, pr): pass
bot.gh, bot.show_pull = gh, show_pull
def by(uid, *roles): return NS(author=NS(id=uid, display_name=f"u{uid}", roles=[NS(id=r) for r in roles]), channel=NS(id=42), jump_url="j")
role = next(iter(bot.AGENT_ROLES))
assert asyncio.run(bot.approve_command(by(1))) == "Pull requests are approved by the Team and Contributors roles."
assert asyncio.run(bot.approve_command(by(1, role))) == "Approved, 1 of 2."  # rgwan approved the commit before the push
assert asyncio.run(bot.approve_command(by(1, role))) == "Approved, 1 of 2."  # one approval each
assert asyncio.run(bot.approve_command(by(2, role))) == "Merged **FSVR#99**, approved by u1 and u2."
assert [c for c in calls if c[0] != "GET"] == [("POST", "comments", None), ("POST", "comments", None), ("PUT", "merge", "new")], calls
# opening a pull request is its author's approval when the author is on the team, and devBot's are on a branch named for them
assert bot.authored({"user": {"login": "WouterVanNifterick", "type": "User"}, "head": {"ref": "perf"}}) == {"1375865258169733201": "WouterVanNifterick"}
assert bot.authored({"user": {"login": "devbot-musicastudio[bot]", "type": "Bot"}, "head": {"ref": "jameshansen/fix"}}) == {"779115813805490189": "jameshansen"}
assert bot.authored({"user": {"login": "dezzo", "type": "User"}, "head": {"ref": "x"}}) == {}
# devBot names the team by their GitHub logins, anyone else by their Discord name
assert bot.gh_name(NS(id=779115813805490189, display_name="Qjimbo")) == "jameshansen" and bot.gh_name(NS(id=0, display_name="quadraf")) == "quadraf"

# a GitHub comment goes to its thread once, and devBot's own don't come back
sent = []
async def thread_of(key): return NS(send=lambda text, files: sent.append(text) or asyncio.sleep(0))
bot.thread_of = thread_of
c1 = {"id": 1, "body": "looks good", "user": {"login": "rgwan"}, "html_url": "h"}
for c in (c1, {"id": 2, "body": "Approved on Discord\n" + bot.MARK, "user": {"login": "x"}, "html_url": "h"}, c1):
    asyncio.run(bot.mirror("FSVR#99", c))
assert sent == ["**rgwan** commented on GitHub <h>\n>>> looks good"], sent

# a merged branch is checked once, with all its commits' messages and its whole diff, and its commits aren't checked again
checked = []
async def verify_commit(product, c): checked.append((c["sha"], c["commit"]["message"], c["commit"]["author"]["name"]))
def commit(sha, msg, name, *parents): return {"sha": sha, "parents": [{"sha": p} for p in parents], "commit": {"message": msg, "author": {"name": name}}}
pushed = [commit("m", "Merge pull request #5", "devBot", "base", "b2"), commit("b2", "Second", "Wouter", "b1"),
          commit("b1", "First", "Wouter", "base"), commit("solo", "Fix x", "James", "base")]
async def gh_all(path, **kw): return pushed
async def gh(method, path, **kw): return {"commits": [pushed[2], pushed[1], pushed[0]]}  # compare base...m
bot.gh_all, bot.gh, bot.verify_commit = gh_all, gh, verify_commit
bot.state["commits"]["r"] = "2026-10-28T00:00:00Z"
asyncio.run(bot.sync_commits("FSVR", "r"))
assert checked == [("m", "Merge pull request #5\n\nb1 First\n\nb2 Second", "Wouter"), ("solo", "Fix x", "James")], checked

# a command stays in the inbox until it's answered, so a restart in between runs it again
replied = []
async def reply(text): replied.append(text)
async def work():
    assert bot.state["inbox"]["9"] == 42
    return "done"
asyncio.run(bot.answer(NS(id=9, channel=NS(id=42), reply=reply), work()))
assert replied == ["done"] and "9" not in bot.state["inbox"]
print("ok")
