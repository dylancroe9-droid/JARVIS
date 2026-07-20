"""Generate a practice test per subject, measure cost + key-verification rate."""
import json, sys, time
sys.path.insert(0, "/Users/dylanroe/JARVIS")
import tools.practice_test as pt
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

SUBJECTS = [
    ("AP Calculus AB", "related rates"),
    ("AP Biology", "cellular respiration"),
    ("AP US History", "the New Deal"),
    ("AP Chemistry", "acid-base equilibria"),
    ("AP Physics 1", "rotational motion"),
    ("AP Statistics", "confidence intervals"),
    ("AP World History", "the Columbian Exchange"),
    ("Algebra 2", "logarithms"),
]
IN_R, OUT_R = 3.00, 15.00
client = Anthropic(api_key=ANTHROPIC_API_KEY)
spend = {"usd": 0.0}

_orig = client.messages.create
def metered(**kw):
    r = _orig(**kw)
    u = r.usage
    spend["usd"] += (u.input_tokens*IN_R + u.output_tokens*OUT_R)/1e6
    return r
client.messages.create = metered
pt._claude = lambda: (client, ANTHROPIC_MODEL)
pt._serve = lambda: True
pt._open_or_reload = lambda: None

rows = []
for subject, topic in SUBJECTS:
    before = spend["usd"]; t0 = time.time()
    raw = pt._generate(topic, 6, f"{subject} level")
    kept, dropped = pt._verify(raw)
    rows.append({"subject": subject, "topic": topic, "generated": len(raw),
                 "kept": len(kept), "dropped": dropped,
                 "cost": spend["usd"]-before, "secs": time.time()-t0,
                 "questions": kept})
    r = rows[-1]
    print(f"  {subject:18} {topic:26} gen={r['generated']:>2} kept={r['kept']:>2} "
          f"dropped={r['dropped']:>2}  ${r['cost']:.4f}  {r['secs']:.0f}s", flush=True)

tot_gen = sum(r["generated"] for r in rows); tot_keep = sum(r["kept"] for r in rows)
print(f"\n  TOTAL   generated={tot_gen}  kept={tot_keep}  "
      f"key-check pass rate={tot_keep/max(tot_gen,1)*100:.0f}%")
print(f"  cost/test (6 questions) = ${sum(r['cost'] for r in rows)/len(rows):.4f}")
print(f"  cost/question           = ${sum(r['cost'] for r in rows)/max(tot_keep,1):.4f}")
json.dump(rows, open("testdata/sweep_results.json","w"), indent=1)
