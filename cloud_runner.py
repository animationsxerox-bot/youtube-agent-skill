import json
import os
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
            context_parts.append(path.read_text(encoding="utf-8")[:3000])
    return "\n\n".join(context_parts)

def call_free_llm(prompt):
    api_key = os.environ["LLM_API_KEY"]
    model = os.environ.get("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    if response.status_code == 429 or response.status_code >= 500:
        print(f"Rate limit reached ({response.status_code}). Saving progress for next run.")
        sys.exit(0)
    response.raise_for_status()
    return json.loads(response.json()["choices"][0]["message"]["content"])

def step_generate_script(state):
    skill_rules = load_skill_context()
    prompt = f"""
    Follow these YouTube skill guidelines:
    {skill_rules}

    Create a YouTube video for this exact topic: {state['active_topic']}
    Channel Niche: {state['niche']}

    Return ONLY valid JSON with these exact keys:
    "title": "High-CTR YouTube title under 70 chars",
    "description": "SEO YouTube description with timestamps and tags",
    "script_text": "Spoken voiceover narration only (no stage directions)"
    """
    result = call_free_llm(prompt)
    state["title"] = result["title"]
    state["description"] = result["description"]
    state["script_text"] = result["script_text"]
    state["current_stage"] = "video"
    save_state(state)

def step_render_video(state):
    subprocess.run(
        [
            "edge-tts",
            "--voice", "en-US-ChristopherNeural",
            "--text", state["script_text"],
            "--write-media", str(AUDIO_FILE),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=0x111827:s=1920x1080:r=30",
            "-i", str(AUDIO_FILE),
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(VIDEO_FILE),
        ],
        check=True,
    )
    state["current_stage"] = "upload"
    save_state(state)

def step_upload_and_schedule(state):
    next_date_str, publish_at_iso = get_next_publish_timestamp(state)
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
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
    if VIDEO_FILE.exists():
        VIDEO_FILE.unlink()
    if AUDIO_FILE.exists():
        AUDIO_FILE.unlink()
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
