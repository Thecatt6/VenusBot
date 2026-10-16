import asyncio
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL


def getSongExpiration(url: str) -> Optional[int]:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.query:
            params = parse_qs(parsed.query)
            expire = params.get("expire") or params.get("expires")
            if expire:
                return int(expire[0])

        match = re.search(r"/expire/(\d+)", parsed.path)
        if match:
            return int(match.group(1))
    except Exception:
        return None
    return None


def _clean_title(title: str) -> str:
    if not title:
        return "Brano sconosciuto"
    cleaned = re.sub(r"[^\w\s\-\(\)\[\]&'.,àèéìòùÀÈÉÌÒÙ]", "", title).strip()
    return cleaned or title.strip()


class VideoSearcher:
    def __init__(self):
        root_dir = Path(__file__).resolve().parent.parent
        self.cookies_path = root_dir / "cookies.txt"

    def _get_base_options(self) -> dict:
        opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
        }
        # Usa cookies.txt solo se esiste ed è effettivamente popolato
        if self.cookies_path.exists() and self.cookies_path.stat().st_size > 32:
            opts["cookiefile"] = str(self.cookies_path)
        return opts

    async def getVideoInfoFromURL(self, video_url: str):
        loop = asyncio.get_running_loop()

        def extract_info():
            yt_dlp_options = {
                **self._get_base_options(),
                "noplaylist": True,
            }
            with YoutubeDL(yt_dlp_options) as ytdlp:
                info = ytdlp.extract_info(video_url, download=False)
                if not info:
                    return None
                return {
                    "title": _clean_title(info.get("title", "")),
                    "duration": int(info.get("duration") or 0),
                    "thumbnail": info.get("thumbnail"),
                    "link": info.get("url"),
                    "url": info.get("webpage_url") or video_url,
                }

        return await loop.run_in_executor(None, extract_info)

    async def getVideoInfoFromQuery(self, video_query: str):
        loop = asyncio.get_running_loop()

        def extract_info():
            yt_dlp_options = {
                **self._get_base_options(),
                "noplaylist": True,
                "default_search": "ytsearch",
            }
            with YoutubeDL(yt_dlp_options) as ytdlp:
                info = ytdlp.extract_info(video_query, download=False)
                if not info:
                    return None
                entries = info.get("entries")
                if entries is not None:
                    entries = [e for e in entries if e]
                    if not entries:
                        return None
                    video = entries[0]
                else:
                    video = info

                return {
                    "title": _clean_title(video.get("title", "")),
                    "duration": int(video.get("duration") or 0),
                    "thumbnail": video.get("thumbnail"),
                    "link": video.get("url"),
                    "url": video.get("webpage_url") or video.get("url"),
                }

        return await loop.run_in_executor(None, extract_info)

    async def getSearchResults(self, video_query: str):
        loop = asyncio.get_running_loop()

        def extract_info():
            yt_dlp_options = {
                **self._get_base_options(),
                "noplaylist": True,
                "default_search": "ytsearch10",
                "ignoreerrors": True,
                "extract_flat": "in_playlist",
            }
            with YoutubeDL(yt_dlp_options) as ytdlp:
                info = ytdlp.extract_info(video_query, download=False)
                if not info:
                    return []
                entries = info.get("entries") if "entries" in info else [info]
                results = []
                for entry in entries or []:
                    if not entry:
                        continue
                    raw_link = entry.get("url") or entry.get("webpage_url") or entry.get("id")
                    if not raw_link:
                        continue
                    if not str(raw_link).startswith("http"):
                        raw_link = f"https://www.youtube.com/watch?v={raw_link}"
                    results.append(
                        {
                            "title": _clean_title(entry.get("title", "")),
                            "link": raw_link,
                        }
                    )
                return results

        return await loop.run_in_executor(None, extract_info)

    async def getPlaylistInfo(self, playlist_url: str):
        loop = asyncio.get_running_loop()

        def extract_info():
            yt_dlp_options = {
                **self._get_base_options(),
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
            }
            with YoutubeDL(yt_dlp_options) as ytdlp:
                playlist = ytdlp.extract_info(playlist_url, download=False)
                if not playlist:
                    return None
                entries = [e for e in (playlist.get("entries") or []) if e]
                metadata = {
                    "playlist_name": _clean_title(playlist.get("title", "Playlist")),
                    "song_count": len(entries),
                    "thumbnail": playlist.get("thumbnail")
                    or (
                        playlist.get("thumbnails", [{}])[0].get("url")
                        if playlist.get("thumbnails")
                        else None
                    ),
                }
                song_urls = []
                for entry in entries:
                    u = entry.get("url") or entry.get("webpage_url") or entry.get("id")
                    if u:
                        if not str(u).startswith("http"):
                            u = f"https://www.youtube.com/watch?v={u}"
                        song_urls.append({"url": u})
                return [metadata] + song_urls

        return await loop.run_in_executor(None, extract_info)
