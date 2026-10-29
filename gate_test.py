"""Runs the close-confirmation step with each model over commit and issue pairs whose answers were checked by hand.

python gate_test.py [model ...]   defaults to the flash model and the strong one
"""
import asyncio
import sys
import time

import aiohttp

import bot

YES, NO, PARTIAL = "fix", "no", "partial"
PAIRS = [  # (commit, issue, expected), each read against the issue's full text
    ("72f6209", 6, YES), ("782e527", 7, YES), ("d7f11e4", 8, YES), ("a69fbef", 25, YES),
    ("9b15281", 26, YES), ("268c0ff", 53, YES), ("b6cb7f9", 54, YES),
    ("794b0db", 3, NO),    # the right fix, but the thread now waits on the reporter to confirm it
    ("35f9823", 56, NO),   # pitch and noise centres, not breath-control smoothing
    ("1afdb44", 56, NO),   # a sample step when the engine sleeps
    ("879149e", 42, NO),   # silence templates, not an init performance
    ("b9b3ae5", 49, NO),   # only reports the unreadable library
    ("5df860b", 55, NO),   # the editor follows the wheel, the host still isn't told
    ("c9ee269", 47, NO),   # one of the issue's three complaints
    ("691d03a", 40, NO),   # "neither host has confirmed it yet"
    ("c7985f6", 3, NO),    # CI and install notes around the fix
    ("b51740a", 29, NO),   # renaming only, the issue's second problem is separate
    ("f7d1e71", 29, NO),   # the second problem only
    ("935aebe", 57, NO),   # three days older than the report, and likely its cause
    ("29de92e", 34, NO),   # never mentions the M6, and the thread waits on the reporter
    ("58e295d", 19, NO),   # an aside in an editor overhaul, and later comments show the limit still there
    ("75a1d05", 19, PARTIAL),  # banks past 16,384 load, host-visible numbers still stop there
    ("e98eee1", 42, YES),  # the message says voice, the code makes each template a part 1 performance
    ("9471d8c", 76, YES),  # "the name takes all 25 cells", on an issue filed from a Discord report of the same day
]


async def run(model):
    bot.STRONG = model
    repo = bot.PRODUCTS["FSVR"]
    gate = asyncio.Semaphore(4)

    async def one(sha, n, expected):
        c = await bot.gh("GET", f"/repos/{repo}/commits/{sha}")
        async with gate:
            quote = await bot.confirm("FSVR", c, f"FSVR#{n}")
        said = YES if quote else NO
        mark = "  " if expected == PARTIAL else "ok" if said == expected else "XX"
        return f"{mark} {sha} #{n:<3} expected {expected:<7} said {said:<4} {(quote or '')[:90]}", expected, said

    t = time.time()
    rows = await asyncio.gather(*(one(*p) for p in PAIRS))
    scored = [r for r in rows if r[1] != PARTIAL]
    print(f"\n== {model}: {sum(r[1] == r[2] for r in scored)}/{len(scored)} right, "
          f"{sum(r[1] == NO and r[2] == YES for r in scored)} wrong closes, "
          f"{sum(r[1] == YES and r[2] == NO for r in scored)} missed fixes, {time.time() - t:.0f}s")
    for r in rows:
        print(" ", r[0])


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as bot.http:
        await bot.load_about()
        for model in sys.argv[1:] or [bot.MODEL, bot.STRONG]:
            await run(model)


asyncio.run(main())
