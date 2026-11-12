import json
import os
import re
import subprocess
import sys
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

STATE_FILE = Path("pipeline-state.json")
VIDEO_FILE = Path("output.mp4")
AUDIO_FILE = Path("voiceover.mp3")
SUBS_FILE = Path("captions.vtt")
BG_VIDEO_FILE = Path("background.mp4")

def load_state():
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def get_next_publish_timestamp(state):
    today_utc = datetime.now(timezone.utc).date()
    tomorrow_utc = today_utc + timedelta(days=1)
    publish_hour = state.get("publish_hour_utc", "14:00:00Z")

    last_date_str = state.get("last_scheduled_date", "")
    if last_date_str:
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
        next_date = max(tomorrow_utc, last_date + timedelta(days=1))
    else:
        next_date = tomorrow_utc

    return next_date.strftime("%Y-%m-%d"), f"{next_date.strftime('%Y-%m-%d')}T{publish_hour}"

def load_skill_context():
    context_parts = []
    for skill_name in ["yt-script", "yt-package"]:
        for path in Path(".").rglob(f"*{skill_name}*/SKILL.md"):
            context_parts.append(path.read_text(encoding="utf-8")[:2500])
    return "\n\n".join(context_parts)

def call_free_llm(prompt):
    api_key = os.environ["LLM_API_KEY"].strip()
    model = os.environ.get("LLM_MODEL", "openrouter/free")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if response.status_code == 429 or response.status_code >= 500:
        print(f"Rate limit reached ({response.status_code}). Saving progress for next run.")
        sys.exit(0)
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError(f"AI did not return JSON. Raw output: {content}")
    return json.loads(match.group(0))

def step_generate_script(state):
    skill_rules = load_skill_context()
    prompt = f"""
    Follow these YouTube hook and retention rules:
    {skill_rules}

    Write a fast-paced YouTube Short (35 to 45 seconds long) for this topic: {state['active_topic']}
    Channel Niche: {state['niche']}

    STRICT SHORTS RULES:
    1. "script_text" MUST be between 90 and 110 spoken words total (this equals exactly 35 to 45 seconds of speech).
    2. Start with a 1-sentence scroll-stopping hook in the first 3 seconds.
    3. Write ONLY spoken words in "script_text" (no [brackets], no scene directions, no emojis).
    4. Include "#Shorts" at the end of the title and inside the description.
    5. Include "search_keyword": a 1 to 2 word visual noun (like "technology", "coding", "robot", "money") to search for background stock video.

    Return ONLY valid JSON with these exact keys:
    {{
      "title": "Catchy Short Title under 60 chars #Shorts",
      "description": "Short punchy description with hashtags #Shorts",
      "script_text": "90 to 110 words of spoken voiceover narration...",
      "search_keyword": "technology"
    }}
    """
    result = call_free_llm(prompt)
    title = result["title"]
    if "#shorts" not in title.lower():
        title = f"{title[:85]} #Shorts"

    state["title"] = title
    state["description"] = result["description"] + "\n\n#Shorts"
    state["script_text"] = result["script_text"]
    state["search_keyword"] = result.get("search_keyword", "technology")
    state["current_stage"] = "video"
    save_state(state)

def download_vertical_broll(keyword):
    pexels_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not pexels_key:
        return False
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": pexels_key},
            params={"query": keyword, "orientation": "portrait", "size": "medium", "per_page": 5},
            timeout=30,
        )
        videos = r.json().get("videos", [])
        for v in videos:
            for vf in v.get("video_files", []):
                if vf.get("height", 0) >= vf.get("width", 0) and vf.get("link"):
                    vid_data = requests.get(vf["link"], timeout=60).content
                    BG_VIDEO_FILE.write_bytes(vid_data)
                    return True
    except Exception as e:
        print(f"Pexels download skipped ({e}), using fallback vertical canvas.")
    return False

def step_render_video(state):
    # 1. Generate energetic voiceover AND word-timed subtitles (.vtt)
    subprocess.run(
        [
            "edge-tts",
            "--voice", "en-US-ChristopherNeural",
            "--rate=+8%",
            "--text", state["script_text"],
            "--write-media", str(AUDIO_FILE),
            "--write-subtitles", str(SUBS_FILE),
        ],
        check=True,
    )

    # 2. Download vertical 9:16 Pexels stock video if key is provided
    has_broll = download_vertical_broll(state.get("search_keyword", "technology"))

    # 3. Render 1080x1920 (9:16 vertical Short) capped at 48 seconds max with burned-in captions
    vf_filter = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "drawbox=x=0:y=0:w=1080:h=1920:color=black@0.35:t=fill,"
        f"subtitles={SUBS_FILE}:force_style='Alignment=10,Fontname=DejaVu Sans,Fontsize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1'"
    )

    if has_broll and BG_VIDEO_FILE.exists():
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", str(BG_VIDEO_FILE),
            "-i", str(AUDIO_FILE),
            "-vf", vf_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-t", "48",
            str(VIDEO_FILE),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=0x0f172a:s=1080x1920:r=30",
            "-i", str(AUDIO_FILE),
            "-vf", f"subtitles={SUBS_FILE}:force_style='Alignment=10,Fontname=DejaVu Sans,Fontsize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2'",
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-t", "48",
            str(VIDEO_FILE),
        ]

    subprocess.run(cmd, check=True)
    state["current_stage"] = "upload"
    save_state(state)

def step_upload_and_schedule(state):
    next_date_str, publish_at_iso = get_next_publish_timestamp(state)
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"].strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"].strip(),
        client_secret=os.environ["YT_CLIENT_SECRET"].strip(),
    )
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": state["title"][:100],
            "description": state["description"],
            "categoryId": "28",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at_iso,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(VIDEO_FILE), chunksize=1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()

    state["last_scheduled_date"] = next_date_str
    state["completed_topics"].append(state["active_topic"])
    state["active_topic"] = ""
    state["title"] = ""
    state["description"] = ""
    state["script_text"] = ""
    state["current_stage"] = "idle"
    for f in [VIDEO_FILE, AUDIO_FILE, SUBS_FILE, BG_VIDEO_FILE]:
        if f.exists():
            f.unlink()
    save_state(state)

def main():
    state = load_state()
    new_topic = os.environ.get("INPUT_TOPIC", "").strip()
    if new_topic and new_topic not in state["pending_topics"] and new_topic != state["active_topic"]:
        state["pending_topics"].append(new_topic)
        save_state(state)

    if state["current_stage"] == "idle":
        if not state["pending_topics"]:
            return
        state["active_topic"] = state["pending_topics"].pop(0)
        state["current_stage"] = "script"
        save_state(state)

    if state["current_stage"] == "script":
        step_generate_script(state)
    if state["current_stage"] == "video":
        step_render_video(state)
    if state["current_stage"] == "upload":
        step_upload_and_schedule(state)

if __name__ == "__main__":
    main()
