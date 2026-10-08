"""Offline checks for the parts of the pipeline that need no API keys.

Runs in CI before the episode is generated, so a broken change fails fast
instead of producing a bad episode or burning API credit.

    python tests/test_offline.py
"""
import sys, os, json, random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import generate_episode as g

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}\n   got:  {got!r}\n   want: {want!r}")
    else:
        print(f"  ok  {name}")


cfg = g.load_config()

print("\n-- config sanity --")
check("two hosts", len(cfg["hosts"]), 2)
check("every host has a voice", all(h.get("voice_name") for h in cfg["hosts"]), True)
check("sponsor and disclaimer are set",
      all((cfg.get(k) or "").strip() for k in
          ("sponsor_intro_text", "disclaimer_text", "sponsor_outro_text")), True)

print("\n-- money and numbers --")
# The whole point: a text-to-speech engine reads "£2,371" as "two pounds,
# three hundred and seventy one". Every one of these must come out as words.
cases = [
    ("£2,371",
     "two thousand three hundred and seventy one pounds"),
    ("The average asking price hit £371,250 last month.",
     "The average asking price hit three hundred and seventy one thousand "
     "two hundred and fifty pounds last month."),
    ("Fees fell from £1,200 to £950, a drop of 21%.",
     "Fees fell from one thousand two hundred pounds to nine hundred and fifty pounds, "
     "a drop of twenty one percent."),
    ("A £2.5m portfolio.", "A two point five million pounds portfolio."),
    ("A £450k flat.", "A four hundred and fifty thousand pounds flat."),
    ("Rates at 4.75% on £285,000.",
     "Rates at four point seven five percent on two hundred and eighty five thousand pounds."),
    ("1,000,000 homes", "one million homes"),
    ("In 2026, completions rose for the 3rd month.",
     "In twenty twenty six, completions rose for the third month."),
    ("Nothing to change here.", "Nothing to change here."),
]
for src, want in cases:
    check(repr(src), g.normalize_for_speech(src), want)

check("no bare digits survive a money sentence",
      any(c.isdigit() for c in g.normalize_for_speech("Up £12,500 on 2024's £310,000.")), False)
check("a sentence comma is not eaten by the number before it",
      g.normalize_for_speech("It was £950, then it fell.").count(","), 1)

print("\n-- pronunciations --")
check("sponsor name is respelled for the voice",
      g.apply_pronunciations("Brought to you by Dezrez.", cfg),
      "Brought to you by Dez Rez.")
check("case insensitive", g.apply_pronunciations("DEZREZ and dezrez", cfg), "Dez Rez and Dez Rez")
check("other words untouched",
      g.apply_pronunciations("Rightmove, Zoopla, conveyancing.", cfg),
      "Rightmove, Zoopla, conveyancing.")

print("\n-- fixed intro and sign-off --")
script = [{"speaker": "Alex", "text": "Welcome."}, {"speaker": "Sam", "text": "Morning."}]
out = g.add_fixed_bookends(script, cfg)
check("three fixed turns added", len(out) - len(script), 3)
check("sponsor leads", out[0]["text"], cfg["sponsor_intro_text"].strip())
check("disclaimer second", out[1]["text"], cfg["disclaimer_text"].strip())
check("sponsor closes", out[-1]["text"], cfg["sponsor_outro_text"].strip())
check("generated script untouched in the middle", out[2:-1], script)
check("disclaimer survives speech normalisation unchanged",
      g.normalize_for_speech(cfg["disclaimer_text"]), cfg["disclaimer_text"].strip())

print("\n-- pacing --")
rng = random.Random("fixed")
base = cfg.get("turn_gap_ms", 320)
reaction = g.gap_after({"speaker": "Sam", "text": "Right."}, {"speaker": "Alex", "text": "So."}, base, rng)
question = g.gap_after({"speaker": "Sam", "text": "But what does that actually mean for agents?"},
                       {"speaker": "Alex", "text": "Well."}, base, rng)
fixed = g.gap_after({"speaker": "Alex", "text": "x", "fixed": True}, None, base, rng)
check("a short reaction comes back quickly", reaction < base, True)
check("a question leaves a longer beat", question > reaction, True)
check("fixed turns get a deliberate gap", fixed > question, True)

print("\n-- download tracking --")
check("prefix wraps an absolute url",
      g.tracked_url("https://example.github.io/x/ep.mp3", {"download_prefix": "https://op3.dev/e/"}),
      "https://op3.dev/e/https://example.github.io/x/ep.mp3")
check("no prefix leaves the url alone",
      g.tracked_url("https://example.github.io/x/ep.mp3", {}), "https://example.github.io/x/ep.mp3")
check("relative url never wrapped",
      g.tracked_url("episodes/ep.mp3", {"download_prefix": "https://op3.dev/e/"}), "episodes/ep.mp3")

print("\n" + "=" * 60)
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("All offline checks passed.")
