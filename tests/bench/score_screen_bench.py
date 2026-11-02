import json, re
from pathlib import Path

rows = [json.loads(l) for l in Path(__file__).with_name("screen_bench_out.jsonl").read_text(encoding="utf-8").splitlines()
        if l.startswith("{")]


def has(a, *words):
    a = a.lower()
    return all(w.lower() in a for w in words)


def none_ans(a):
    a = a.lower().strip().strip(".")
    return a == "none" or a.startswith("none") or "no phone" in a or "no error" in a or "there is no" in a


CHECKS = {
    1: ("pop-up there? (yes)", lambda a: a.lower().startswith("yes")),
    2: ("pop-up buttons (Got it, Customize)", lambda a: has(a, "got it", "customize")),
    3: ("which number dismisses (4)", lambda a: re.fullmatch(r"\D*4\D*", a.strip()) is not None),
    4: ("pop-up on results? (no)", lambda a: a.lower().startswith("no")),
    5: ("temperature (78°F)", lambda a: "78" in a),
    6: ("Wed 7 high/low (85/56)", lambda a: "85" in a and "56" in a),
    7: ("which number is AccuWeather (2)", lambda a: re.fullmatch(r"\D*2\D*", a.strip()) is not None),
    8: ("TRICK phone number (none)", none_ans),
    9: ("dialog open? (yes)", lambda a: a.lower().startswith("yes")),
    10: ("File name box text", lambda a: has(a, "search weather in springfield")),
    11: ("save button (Save)", lambda a: a.strip().strip('".').lower() == "save"),
    12: ("TRICK red error (none)", none_ans),
}
for model in dict.fromkeys(r["model"] for r in rows):
    mine = [r for r in rows if r["model"] == model]
    ok = 0
    print(f"=== {model}")
    for r in mine:
        name, check = CHECKS[r["q"]]
        good = (not r["answer"].startswith("ERROR")) and check(r["answer"])
        ok += good
        print(f"  {'OK ' if good else 'BAD'} Q{r['q']:2} {name:34} {r['seconds']:5}s  {r['answer'][:90]!r}")
    secs = [r["seconds"] for r in mine]
    print(f"  score {ok}/{len(mine)} | median {sorted(secs)[len(secs)//2]}s per answer")
