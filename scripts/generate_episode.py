#!/usr/bin/env python3
"""
Daily podcast generator.

1. Pulls recent items from the RSS feeds in config.json
2. Asks Claude (Anthropic API) to write a two-host dialogue script about them,
   focused on AI's impact on UK estate agency and conveyancing
3. Turns that script into audio using ElevenLabs (one voice per host)
4. Stitches the audio together into one episode file
5. Updates docs/episodes.json and regenerates docs/feed.xml (a podcast RSS feed)

Run with:
    ANTHROPIC_API_KEY=... ELEVENLABS_API_KEY=... python scripts/generate_episode.py
"""

import os
import re
import json
import time
import base64
import random
import datetime
import xml.sax.saxutils as saxutils

import feedparser
import requests
from pydub import AudioSegment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DOCS_DIR = os.path.join(ROOT, "docs")
EPISODES_DIR = os.path.join(DOCS_DIR, "episodes")
EPISODES_JSON = os.path.join(DOCS_DIR, "episodes.json")
FEED_XML = os.path.join(DOCS_DIR, "feed.xml")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_TTS_API_KEY = os.environ.get("GOOGLE_TTS_API_KEY")
# The public base URL where docs/ ends up being served, e.g.
# https://yourusername.github.io/podcast-pipeline
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# Feed text goes straight into the model prompt, so a headline or summary carrying
# instructions is a prompt-injection route into a show that publishes unattended.
# Established trade press is unlikely to try it; the Google News feed pulls from
# whoever ranks that day, which is where this actually earns its keep.
INJECTION_PATTERNS = [re.compile(p, re.I | re.M) for p in (
    r"ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:\w+\s+)?(?:instruction|prompt|direction|rule|message)",
    r"disregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:\w+\s+)?(?:instruction|prompt|direction|rule|message)",
    r"forget\s+(?:everything|all\s+(?:previous|prior)|your\s+(?:instruction|prompt|rule))",
    r"\bsystem\s+prompt\b",
    r"\bnew\s+instructions?\s*:",
    r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|roleplay\s+as)\s+(?:an?\s+)?"
    r"(?:ai|assistant|language\s+model|chatbot)\b",
    r"</?(?:system|assistant|user|instructions?)>",
    r"^\s*(?:system|assistant|user)\s*:",
    r"respond\s+(?:only\s+)?with\s+(?:the\s+following|only|json)",
    r"\boutput\s+only\b",
    r"```",
    r'"speaker"\s*:',
)]


def injection_risk(text):
    """Return the pattern that fired on this text, or None if it looks clean."""
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text or ""):
            return pattern.pattern
    return None


def collect_recent_items(feeds, lookback_hours):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=lookback_hours)
    items = []
    quarantined = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"Could not read feed {url}: {e}")
            continue
        for entry in parsed.entries:
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(entry, key, None):
                    published = datetime.datetime(*entry[key][:6])
                    break
            if published and published < cutoff:
                continue
            title = getattr(entry, "title", "")
            summary = re.sub("<[^<]+?>", "", getattr(entry, "summary", "")).strip()[:500]
            source = parsed.feed.get("title", url)
            risk = injection_risk(f"{title} {summary}")
            if risk:
                quarantined.append((title[:70], source, risk))
                continue
            items.append({
                "title": title,
                "summary": summary,
                "link": getattr(entry, "link", ""),
                "source": source,
            })

    if quarantined:
        print(f"  !! {len(quarantined)} item(s) quarantined - text resembling prompt injection:")
        for title, source, pattern in quarantined:
            print(f"     [{source}] {title}")
            print(f"       matched: {pattern}")
    return items
HISTORY_PATH = os.path.join(DOCS_DIR, "covered_links.json")


def load_covered_links(days):
    if not os.path.exists(HISTORY_PATH):
        return set()
    with open(HISTORY_PATH) as f:
        history = json.load(f)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return {h["link"] for h in history if h["date"] >= cutoff}


def save_covered_links(items, days):
    history = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    today = datetime.date.today().isoformat()
    history.extend({"link": it["link"], "date": today} for it in items)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days * 2)).isoformat()
    history = [h for h in history if h["date"] >= cutoff]
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)

def build_script_prompt(items, config):
    stories_text = "\n\n".join(
        f"- {it['title']} ({it['source']})\n  {it['summary']}\n  {it['link']}"
        for it in items
    ) or "No fresh stories today from the configured feeds."

    host_a, host_b = config["hosts"][0]["name"], config["hosts"][1]["name"]

    return f"""You write scripts for a daily two-host podcast called
"{config['podcast_title']}". The hosts are {host_a} and {host_b}. Their beat is:
how AI is changing UK estate agency and UK conveyancing, with a wider eye on
UK property, mortgages, and general AI industry news that could plausibly
affect the sector.

Here are today's candidate stories, none of which have been covered in the
last several episodes:

{stories_text}

Write a natural, engaging, WIDE-RANGING conversation between {host_a} and {host_b}
(~{config['target_word_count']} words) covering several distinct stories and
angles rather than dwelling on just one. Vary the structure day to day —
sometimes lead with the most significant story, sometimes open with a quick
round-up before going deeper on one or two; occasionally include a short
'quick takes' segment on smaller stories. If few stories are directly about
AI, discuss what AI's impact on the story's topic might be. Keep it
conversational and avoid the hosts repeating each other's points. Include a
short intro (welcome + today's date) and a short sign-off.

ACCURACY AND LEGAL CARE (these matter more than the writing):
- Never invent a number, a name, a quote, a firm's position or an outcome that is
  not in the supplied stories. If a detail is missing, have a host say so plainly.
- Name your source out loud when you introduce a story: "Estate Agent Today is
  reporting...", "according to Today's Conveyancer...". Every story, every time.
- The dangerous claims are insolvency or administration, SRA, FCA, Trading Standards
  or Ombudsman investigations, enforcement action, negligence claims, fraud, and
  misconduct by a named firm or person. Only state one if the supplied story states
  it, and attribute it in the same sentence. Never as a bare assertion.
- Never upgrade a hedge. "Reportedly", "alleged", "is understood to", "faces
  scrutiny over" must survive into the script. An allegation is not a finding, an
  investigation is not a verdict, a warning is not a collapse.
- Never speculate about a named firm's solvency or survival, or about why a named
  individual left a role. Say what is known, attribute it, and stop.
- Name company principals and public figures acting in their public roles. Do not
  name junior staff, clients, buyers, sellers or private individuals who appear
  incidentally in a story.
- Give no regulated advice. Mortgage advice is FCA-regulated and legal advice is
  SRA-regulated, and this show is neither. Report what happened and what it might
  mean commercially for an agency or a firm. Never tell a listener what to do about
  a specific case, transaction or client.

Do NOT write any sponsor message, advertisement, or disclaimer about AI or
accuracy. A fixed sponsor read and disclaimer are added automatically before
and after your script, so writing your own would duplicate them.

Spell out EVERY number, price, percentage and date in words. A text-to-speech
engine reads this aloud and mangles digits, commas and currency symbols:
"£2,371" gets read as "two pounds, three hundred and seventy one". Write
"two thousand three hundred and seventy one pounds", "twelve percent",
"four hundred and fifty thousand pounds", "twenty twenty six".

Respond with ONLY a JSON array, no other text, no markdown fences. Each element:
{{"speaker": "{host_a}" or "{host_b}", "text": "..."}}
"""


def add_fixed_bookends(turns, config):
    """Top and tail the episode with text read verbatim from config.

    The sponsor message and the AI disclaimer are never generated by the model:
    a sponsor is paying for a specific form of words, and the disclaimer is a
    statement of fact about how the show is made. Both have to be identical in
    every episode, which means they cannot come from a language model.
    """
    host = config["hosts"][0]["name"]
    intro = [
        {"speaker": host, "text": config[key].strip(), "fixed": True}
        for key in ("sponsor_intro_text", "disclaimer_text")
        if (config.get(key) or "").strip()
    ]
    outro = [
        {"speaker": host, "text": config["sponsor_outro_text"].strip(), "fixed": True}
    ] if (config.get("sponsor_outro_text") or "").strip() else []
    return intro + turns + outro


def call_anthropic(prompt, model):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def synthesize_turn(text, voice_name, language_code):
    resp = requests.post(
        f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_TTS_API_KEY}",
        headers={"content-type": "application/json"},
        json={
            "input": {"text": text},
            "voice": {"languageCode": language_code, "name": voice_name},
            "audioConfig": {"audioEncoding": "MP3"},
        },
        timeout=120,
    )
    resp.raise_for_status()
    audio_b64 = resp.json()["audioContent"]
    return base64.b64decode(audio_b64)


# ------------------------------------------------- speech normalisation

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def _under_hundred(n):
    if n < 20:
        return ONES[n]
    tens, ones = divmod(n, 10)
    return TENS[tens] + (f" {ONES[ones]}" if ones else "")


def _under_thousand(n):
    hundreds, rest = divmod(n, 100)
    if not hundreds:
        return _under_hundred(rest)
    out = f"{ONES[hundreds]} hundred"
    return out + (f" and {_under_hundred(rest)}" if rest else "")


def number_to_words(n):
    if n == 0:
        return "zero"
    parts = []
    for value, name in SCALES:
        if n >= value:
            count, n = divmod(n, value)
            parts.append(f"{_under_thousand(count)} {name}")
    if n:
        parts.append(("and " if parts and n < 100 else "") + _under_thousand(n))
    return " ".join(parts)


ORDINALS = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth"}


def ordinal_to_words(n):
    if n in ORDINALS:
        return ORDINALS[n]
    words = number_to_words(n)
    last = words.rsplit(" ", 1)[-1]
    head = words[: len(words) - len(last)]
    suffixed = {"one": "first", "two": "second", "three": "third", "five": "fifth",
                "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}.get(last)
    if suffixed:
        return head + suffixed
    if last.endswith("y"):
        return head + last[:-1] + "ieth"
    return head + last + "th"


def year_to_words(n):
    if 2000 <= n <= 2009:
        return "two thousand" + (f" and {ONES[n - 2000]}" if n % 10 else "")
    first, second = divmod(n, 100)
    if second == 0:
        return f"{_under_hundred(first)} hundred"
    return f"{_under_hundred(first)} {'oh ' + ONES[second] if second < 10 else _under_hundred(second)}"


CURRENCY = {"£": "pounds", "$": "dollars", "€": "euros"}
SUFFIXES = {"k": "thousand", "m": "million", "bn": "billion", "b": "billion"}
UNITS = {
    "mm": "millimetres", "cm": "centimetres", "km": "kilometres", "m": "metres",
    "kg": "kilograms", "lb": "pounds", "lbs": "pounds", "ft": "feet",
    "mph": "miles per hour", "kph": "kilometres per hour", "km/h": "kilometres per hour",
}


def _digits_to_words(raw):
    """'12,500' -> 'twelve thousand five hundred'; '9.5' -> 'nine point five'."""
    raw = raw.replace(",", "")
    if "." in raw:
        whole, frac = raw.split(".", 1)
        whole_words = number_to_words(int(whole)) if whole else "zero"
        frac_words = " ".join(ONES[int(d)] for d in frac if d.isdigit())
        return f"{whole_words} point {frac_words}"
    return number_to_words(int(raw))


def normalize_for_speech(text):
    """Safety net for anything Claude left as digits or symbols — TTS reads these badly."""
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"[\*\_`#\[\]]", "", text)
    text = text.replace("&", " and ").replace("…", "...").replace("—", ", ").replace("–", "-")

    # Currency, optionally with a k/m/bn suffix: £2.5m -> two point five million pounds
    def _currency(m):
        unit = CURRENCY[m.group(1)]
        amount = _digits_to_words(m.group(2))
        scale = SUFFIXES.get((m.group(3) or "").lower())
        return f"{amount} {scale} {unit}" if scale else f"{amount} {unit}"

    # The suffix group must not swallow the following space when there is no suffix.
    text = re.sub(r"([£$€])\s?(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s?(bn|[kmb])\b)?", _currency, text, flags=re.I)
    text = re.sub(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s?%", lambda m: f"{_digits_to_words(m.group(1))} percent", text)

    # Units usually run straight into the digits ("140mm"), so handle them before
    # the generic number pass — \b would not fire between "0" and "m".
    unit_pattern = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
    text = re.sub(
        rf"(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?)\s?({unit_pattern})(?![\w])",
        lambda m: f"{_digits_to_words(m.group(1))} {UNITS[m.group(2).lower()]}",
        text, flags=re.I,
    )

    # "in" only counts as inches when glued to the digits — "5 in the morning" is not.
    text = re.sub(r"(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?)in(?![\w])",
                  lambda m: f"{_digits_to_words(m.group(1))} inches", text, flags=re.I)

    text = re.sub(r"\b(\d+)(st|nd|rd|th)\b", lambda m: ordinal_to_words(int(m.group(1))), text, flags=re.I)
    text = re.sub(r"\b(19|20)(\d{2})\b", lambda m: year_to_words(int(m.group(0))), text)

    def _plain(m):
        try:
            words = _digits_to_words(m.group(0))
        except (ValueError, IndexError, KeyError):
            return m.group(0)
        # Keep a gap if the number was glued to a word, e.g. "50cc" -> "fifty cc".
        tail = m.string[m.end():m.end() + 1]
        return words + (" " if tail.isalpha() else "")

    # No trailing \b: it would miss digits glued to letters, e.g. "50cc", "10x".
    text = re.sub(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?", _plain, text)
    return re.sub(r"\s+", " ", text).strip()


def apply_pronunciations(text, config):
    """Respell words the voice says wrong, just before synthesis.

    Chirp 3: HD does not support SSML, so there is no phoneme tag to reach for -
    changing the spelling the engine sees is the only lever. Applied at synthesis
    time only, so the written form stays correct in the script, the transcript,
    the show notes and the RSS feed.
    """
    for written, spoken in (config.get("pronunciations") or {}).items():
        text = re.sub(rf"\b{re.escape(written)}\b", spoken, text, flags=re.I)
    return text


def gap_after(turn, next_turn, base, rng):
    """How long to leave before the next turn.

    An identical pause after every turn is most of what makes stitched speech
    sound mechanical - real conversation has a rhythm. A two-word reaction comes
    back almost instantly, a question leaves a beat before the answer, and the
    same speaker carrying on barely pauses at all.
    """
    if turn.get("fixed") or (next_turn is not None and next_turn.get("fixed")):
        # The sponsor read and disclaimer are separate statements, not conversation.
        # They need a clear beat around them, even though one host reads both.
        return int(base * 2.0)

    text = turn["text"].rstrip()
    words = len(text.split())
    if next_turn is not None and next_turn["speaker"] == turn["speaker"]:
        scale = 0.35          # same voice continuing - just a breath
    elif words <= 4:
        scale = 0.45          # "Go on." - the reply lands on top of it
    elif text.endswith("?"):
        scale = 1.3           # let a question hang
    elif text.endswith(("...", "-")):
        scale = 0.4           # trailing off, interrupted
    else:
        scale = 1.0
    return max(90, int(base * scale * rng.uniform(0.82, 1.22)))


def build_episode_audio(turns, hosts_by_name, language_code, out_path, config):
    voice_map = {h["name"]: h["voice_name"] for h in hosts_by_name}
    combined = AudioSegment.silent(duration=300)
    base_gap = config.get("turn_gap_ms", 320)
    # Seeded per episode so a given script always stitches identically.
    rng = random.Random(f"{config['podcast_title']}-{datetime.date.today().isoformat()}")
    tmp_dir = os.path.join(ROOT, "_tmp_audio")
    os.makedirs(tmp_dir, exist_ok=True)

    for i, turn in enumerate(turns):
        voice_name = voice_map.get(turn["speaker"])
        if not voice_name:
            raise RuntimeError(
                f"No voice configured for speaker '{turn['speaker']}'. "
                "Edit config.json 'hosts' with real Google TTS voice names."
            )
        spoken = apply_pronunciations(normalize_for_speech(turn["text"]), config)
        audio_bytes = synthesize_turn(spoken, voice_name, language_code)
        seg_path = os.path.join(tmp_dir, f"seg_{i}.mp3")
        with open(seg_path, "wb") as f:
            f.write(audio_bytes)
        combined += AudioSegment.from_mp3(seg_path) + AudioSegment.silent(
            duration=gap_after(turn, turns[i + 1] if i + 1 < len(turns) else None, base_gap, rng)
        )
        time.sleep(0.3)  # be gentle on rate limits

    combined.export(out_path, format="mp3")


def tracked_url(url, config):
    """Wrap an episode URL in a download-analytics prefix.

    GitHub Pages keeps no access logs, so without a prefix there is no way to
    know whether anyone is listening. Applied when the feed is written rather
    than when the URL is stored, so past episodes are counted too and removing
    the prefix is a config change rather than a migration.
    """
    prefix = (config.get("download_prefix") or "").strip()
    if not prefix or not url.startswith("http"):
        return url
    return prefix.rstrip("/") + "/" + url


def corrections_line(config):
    """A visible, working route to report an error. Acting on a complaint quickly is
    the cheapest defence there is; being unreachable is what escalates one."""
    policy = (config.get("corrections_policy") or "").strip()
    email = (config.get("corrections_email") or "").strip()
    if not policy:
        return ""
    return policy.replace("{email}", email) if email else policy


def update_feed(config, episode_meta):
    os.makedirs(DOCS_DIR, exist_ok=True)
    episodes = []
    if os.path.exists(EPISODES_JSON):
        with open(EPISODES_JSON) as f:
            episodes = json.load(f)
    episodes.insert(0, episode_meta)
    with open(EPISODES_JSON, "w") as f:
        json.dump(episodes, f, indent=2)

    items_xml = ""
    for ep in episodes:
        audio = tracked_url(ep["audio_url"], config)
        items_xml += f"""
    <item>
      <title>{saxutils.escape(ep['title'])}</title>
      <description>{saxutils.escape(ep['description'])}</description>
      <pubDate>{ep['pub_date']}</pubDate>
      <enclosure url="{saxutils.escape(audio)}" length="{ep['file_size']}" type="audio/mpeg" />
      <guid isPermaLink="false">{ep['guid']}</guid>
    </item>"""

    cover_url = f"{PUBLIC_BASE_URL}/{config['cover_image']}" if PUBLIC_BASE_URL else config['cover_image']

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{saxutils.escape(config['podcast_title'])}</title>
    <description>{saxutils.escape((config['podcast_description'] + " " + corrections_line(config)).strip())}</description>
    <language>{config['podcast_language']}</language>
    <managingEditor>{saxutils.escape(config.get('corrections_email', ''))} ({saxutils.escape(config['podcast_author'])})</managingEditor>
    <itunes:author>{saxutils.escape(config['podcast_author'])}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{saxutils.escape(cover_url)}" />
    <image>
      <url>{saxutils.escape(cover_url)}</url>
      <title>{saxutils.escape(config['podcast_title'])}</title>
    </image>{items_xml}
  </channel>
</rss>
"""
    with open(FEED_XML, "w") as f:
        f.write(feed_xml)


def main():
    if not ANTHROPIC_API_KEY or not GOOGLE_TTS_API_KEY:
        raise SystemExit("Set ANTHROPIC_API_KEY and GOOGLE_TTS_API_KEY environment variables.")

    config = load_config()
    os.makedirs(EPISODES_DIR, exist_ok=True)

    print("Fetching feeds...")
    all_items = collect_recent_items(config["feeds"], config["lookback_hours"])
    covered = load_covered_links(config.get("history_days", 7))
    items = [it for it in all_items if it["link"] not in covered] or all_items
    print(f"Found {len(all_items)} recent items, {len(items)} not yet covered.")
    print("Writing script with Claude...")
    prompt = build_script_prompt(items, config)
    turns = call_anthropic(prompt, config["anthropic_model"])
    turns = add_fixed_bookends(turns, config)
    print(f"Script has {len(turns)} lines (including the fixed intro and sign-off).")

    today = datetime.date.today().isoformat()
    mp3_name = f"{today}.mp3"
    mp3_path = os.path.join(EPISODES_DIR, mp3_name)

    print("Generating audio with Google Cloud TTS...")
    build_episode_audio(turns, config["hosts"], config["google_tts_language_code"], mp3_path, config)

    file_size = os.path.getsize(mp3_path)
    audio_url = f"{PUBLIC_BASE_URL}/episodes/{mp3_name}" if PUBLIC_BASE_URL else f"episodes/{mp3_name}"

    episode_meta = {
        "title": f"{config['podcast_title']} — {today}",
        "description": (
            "Today's AI-in-property discussion, generated from the day's UK estate agency "
            "and conveyancing news. "
            + (config.get("disclaimer_text") or "").strip()
            + (" " + corrections_line(config) if corrections_line(config) else "")
        ),
        "pub_date": datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "audio_url": audio_url,
        "file_size": file_size,
        "guid": f"episode-{today}",
    }

    print("Updating feed.xml...")
    update_feed(config, episode_meta)
    save_covered_links(items, config.get("history_days", 7))
    print("Done.")


if __name__ == "__main__":
    main()

