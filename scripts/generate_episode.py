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


def collect_recent_items(feeds, lookback_hours):
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=lookback_hours)
    items = []
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
            items.append({
                "title": getattr(entry, "title", ""),
                "summary": re.sub("<[^<]+?>", "", getattr(entry, "summary", "")).strip()[:500],
                "link": getattr(entry, "link", ""),
                "source": parsed.feed.get("title", url),
            })
    return items


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

Respond with ONLY a JSON array, no other text, no markdown fences. Each element:
{{"speaker": "{host_a}" or "{host_b}", "text": "..."}}
"""


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


def build_episode_audio(turns, hosts_by_name, language_code, out_path):
    voice_map = {h["name"]: h["voice_name"] for h in hosts_by_name}
    combined = AudioSegment.silent(duration=300)
    pause = AudioSegment.silent(duration=350)
    tmp_dir = os.path.join(ROOT, "_tmp_audio")
    os.makedirs(tmp_dir, exist_ok=True)

    for i, turn in enumerate(turns):
        voice_name = voice_map.get(turn["speaker"])
        if not voice_name:
            raise RuntimeError(
                f"No voice configured for speaker '{turn['speaker']}'. "
                "Edit config.json 'hosts' with real Google TTS voice names."
            )
        audio_bytes = synthesize_turn(turn["text"], voice_name, language_code)
        seg_path = os.path.join(tmp_dir, f"seg_{i}.mp3")
        with open(seg_path, "wb") as f:
            f.write(audio_bytes)
        combined += AudioSegment.from_mp3(seg_path) + pause
        time.sleep(0.3)  # be gentle on rate limits

    combined.export(out_path, format="mp3")


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
        items_xml += f"""
    <item>
      <title>{saxutils.escape(ep['title'])}</title>
      <description>{saxutils.escape(ep['description'])}</description>
      <pubDate>{ep['pub_date']}</pubDate>
      <enclosure url="{saxutils.escape(ep['audio_url'])}" length="{ep['file_size']}" type="audio/mpeg" />
      <guid isPermaLink="false">{ep['guid']}</guid>
    </item>"""

    cover_url = f"{PUBLIC_BASE_URL}/{config['cover_image']}" if PUBLIC_BASE_URL else config['cover_image']

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{saxutils.escape(config['podcast_title'])}</title>
    <description>{saxutils.escape(config['podcast_description'])}</description>
    <language>{config['podcast_language']}</language>
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
    items = collect_recent_items(config["feeds"], config["lookback_hours"])
    print(f"Found {len(items)} recent items.")

    print("Writing script with Claude...")
    prompt = build_script_prompt(items, config)
    turns = call_anthropic(prompt, config["anthropic_model"])
    print(f"Script has {len(turns)} lines.")

    today = datetime.date.today().isoformat()
    mp3_name = f"{today}.mp3"
    mp3_path = os.path.join(EPISODES_DIR, mp3_name)

    print("Generating audio with Google Cloud TTS...")
    build_episode_audio(turns, config["hosts"], config["google_tts_language_code"], mp3_path)

    file_size = os.path.getsize(mp3_path)
    audio_url = f"{PUBLIC_BASE_URL}/episodes/{mp3_name}" if PUBLIC_BASE_URL else f"episodes/{mp3_name}"

    episode_meta = {
        "title": f"{config['podcast_title']} — {today}",
        "description": "Today's AI-in-property discussion, generated from the day's UK estate agency and conveyancing news.",
        "pub_date": datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "audio_url": audio_url,
        "file_size": file_size,
        "guid": f"episode-{today}",
    }

    print("Updating feed.xml...")
    update_feed(config, episode_meta)
    print("Done.")


if __name__ == "__main__":
    main()

