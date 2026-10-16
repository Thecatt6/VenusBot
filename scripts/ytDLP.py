import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL


def getSongExpiration(url: str) -> Optional[int]:
    if not url or not str(url).startswith("http"):
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
        self.cache_dir = Path(tempfile.gettempdir()) / "venusbot_music"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_base_options(self) -> dict:
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "nocheckcertificate": True,
            "geo_bypass": True,
        }
        # Usa cookies.txt solo se esiste ed è effettivamente popolato
        if self.cookies_path.exists() and self.cookies_path.stat().st_size > 32:
            opts["cookiefile"] = str(self.cookies_path)
        return opts

    def _download_or_extract(self, target: str, is_search: bool = False) -> Optional[dict]:
        """
        Estrae le informazioni del brano e, per i brani non-live di durata <= 30 minuti,
        scarica direttamente il flusso audio tramite il downloader interno di yt-dlp
        (evitando qualsiasi errore HTTP 403 Forbidden di FFmpeg su googlevideo.com).
        Per le live stream (es. radio /lofi) restituisce l'URL di stream + gli header HTTP.
        """
        base_opts = {
            **self._get_base_options(),
            "noplaylist": True,
        }
        if is_search:
            base_opts["default_search"] = "ytsearch"

        # 1. Estrai metadati senza scaricare per verificare se è live o un brano standard
        with YoutubeDL(base_opts) as ytdlp:
            info = ytdlp.extract_info(target, download=False)
            if not info:
                return None
            if "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return None
                video = entries[0]
            else:
                video = info

        duration = int(video.get("duration") or 0)
        is_live = bool(video.get("is_live")) or duration == 0
        webpage_url = video.get("webpage_url") or video.get("url") or target
        headers = video.get("http_headers") or {}

        # 2. Se è un brano standard (non live, <= 30 min), scaricalo in cache locale con yt-dlp
        if not is_live and 0 < duration <= 1800:
            file_token = uuid.uuid4().hex[:12]
            outtmpl = str(self.cache_dir / f"track_{file_token}.%(ext)s")
            dl_opts = {
                **self._get_base_options(),
                "noplaylist": True,
                "outtmpl": outtmpl,
            }
            try:
                with YoutubeDL(dl_opts) as ytdlp_dl:
                    dl_info = ytdlp_dl.extract_info(webpage_url, download=True)
                    if dl_info and "entries" in dl_info:
                        dl_info = [e for e in dl_info["entries"] if e][0]
                    downloaded_path = ytdlp_dl.prepare_filename(dl_info)
                    if downloaded_path and Path(downloaded_path).exists():
                        return {
                            "title": _clean_title(video.get("title", "")),
                            "duration": duration,
                            "thumbnail": video.get("thumbnail"),
                            "link": str(downloaded_path),
                            "url": webpage_url,
                            "http_headers": headers,
                            "is_local_cache": True,
                        }
            except Exception as e:
                logging.warning(f"Download diretto yt-dlp fallito ({e}), uso stream URL con headers...")

        return {
            "title": _clean_title(video.get("title", "")),
            "duration": duration,
            "thumbnail": video.get("thumbnail"),
            "link": video.get("url"),
            "url": webpage_url,
            "http_headers": headers,
            "is_local_cache": False,
        }

    async def getVideoInfoFromURL(self, video_url: str):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_or_extract, video_url, False)

    async def getVideoInfoFromQuery(self, video_query: str):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_or_extract, video_query, True)

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
