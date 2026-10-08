import os
import re
import struct
from urllib.parse import quote

import requests
import zlib

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

app = FastAPI(title="Glassmorphism Audio Streamer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

_ICON_CACHE: dict[int, bytes] = {}


def _make_app_icon(size: int) -> bytes:
    cached_icon = _ICON_CACHE.get(size)
    if cached_icon is not None:
        return cached_icon

    pixels = bytearray()
    radius = size * 0.22
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            nearest_x = min(max(x, radius), size - radius)
            nearest_y = min(max(y, radius), size - radius)
            inside = (x - nearest_x) ** 2 + (y - nearest_y) ** 2 <= radius ** 2
            if not inside:
                pixels.extend((0, 0, 0, 0))
                continue
            pixels.extend((180, 80, 220, 255))

    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = chunk_type + data
        return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(pixels), level=9))
        + png_chunk(b"IEND", b"")
    )
    _ICON_CACHE[size] = png
    return png


@app.get("/", response_class=HTMLResponse)
def index():
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Chưa tìm thấy file index.html. Vui lòng tạo index.html cùng thư mục với server.py!</h1>"


@app.get("/manifest.json")
def app_manifest():
    return JSONResponse(
        {
            "id": "/",
            "name": "Glass Audio - Trình nghe nhạc",
            "short_name": "Glass Audio",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "display_override": ["standalone"],
            "orientation": "portrait",
            "background_color": "#272044",
            "theme_color": "#272044",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@app.get("/icon-192.png")
def app_icon_192():
    return Response(content=_make_app_icon(192), media_type="image/png")


@app.get("/icon-512.png")
def app_icon_512():
    return Response(content=_make_app_icon(512), media_type="image/png")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return Response(content=_make_app_icon(180), media_type="image/png")


@app.get("/service-worker.js")
def service_worker():
    worker_script = """
const CACHE_NAME = "glass-audio-shell-v1";
const APP_SHELL = "/";

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.add(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(key => key.startsWith("glass-audio-shell-") && key !== CACHE_NAME)
        .map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname !== APP_SHELL) {
    return;
  }
  event.respondWith(
    fetch(request)
      .then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(APP_SHELL, copy));
        }
        return response;
      })
      .catch(() => caches.match(APP_SHELL))
  );
});
"""
    return Response(
        content=worker_script,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


def _extract_video_id(url: str):
    match = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})', url)
    if not match:
        raise HTTPException(status_code=400, detail="Link YouTube không hợp lệ")
    return match.group(1)


@app.get("/api/extract")
def extract_audio(url: str = Query(...)):
    video_id = _extract_video_id(url)

    if yt_dlp is None:
        raise HTTPException(status_code=500, detail="Thiếu thư viện yt-dlp. Chạy lệnh: pip install yt-dlp")

    try:
        ydl_opts = {
            "quiet": True,
            "noplaylist": True,
            "skip_download": True,
            "no_warnings": True,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "extract_flat": False,
            "extractor_args": {
                "youtube": {
                    "player_client": ["ios", "android"],
                }
            },
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise ValueError("Không lấy được dữ liệu video")

        audio_url = info.get("url")
        if not audio_url and "formats" in info:
            audio_formats = [f for f in info["formats"] if f.get("acodec") != "none" and f.get("url")]
            if audio_formats:
                audio_url = max(audio_formats, key=lambda x: x.get("abr") or x.get("tbr") or 0).get("url")

        if not audio_url:
            raise ValueError("Không tìm thấy dạng âm thanh phù hợp")

        title = info.get("title") or "Bản nhạc YouTube"
        artist = info.get("uploader") or "Nghệ sĩ YouTube"
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
        thumbnail = info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        return JSONResponse({
            "title": title,
            "artist": artist,
            "thumbnail": thumbnail,
            "stream_url": f"/api/proxy-audio?audio_src={quote(audio_url, safe='')}",
            "download_url": f"/api/download?audio_src={quote(audio_url, safe='')}&filename={quote(clean_title, safe='')}",
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi trích xuất: {str(e)}")


@app.get("/api/proxy-audio")
def proxy_audio(request: Request, audio_src: str):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
        }
        range_header = request.headers.get("range")
        if range_header:
            headers["Range"] = range_header

        req = requests.get(audio_src, stream=True, headers=headers)
        response_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": req.headers.get("Content-Type", "audio/mp4"),
        }

        if "Content-Range" in req.headers:
            response_headers["Content-Range"] = req.headers["Content-Range"]
            status_code = 206
        else:
            status_code = req.status_code

        if "Content-Length" in req.headers:
            response_headers["Content-Length"] = req.headers["Content-Length"]

        return StreamingResponse(
            req.iter_content(chunk_size=1024 * 64),
            status_code=status_code,
            headers=response_headers,
            media_type=response_headers["Content-Type"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download")
def download_audio(audio_src: str, filename: str):
    return RedirectResponse(url=audio_src)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)