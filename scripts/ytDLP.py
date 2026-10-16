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


# Parole tipiche di video di YouTuber / Gameplay / Vlog da escludere quando si cerca musica
NON_MUSIC_BLACKLIST = (
    "gameplay",
    "walkthrough",
    "let's play",
    "lets play",
    "vlog",
    "reaction",
    "reazione",
    "episodio",
    "minecraft",
    "fortnite",
    "roblox",
    "gta",
    "brawl stars",
    "clash royale",
    "recensione",
    "tutorial",
    "unboxing",
    "intervista",
    "podcast",
    "telecronaca",
    "highlights",
    "challenge",
    "prank",
    "scherzo",
)


def _score_music_candidate(entry: dict, original_query: str) -> int:
    """
    Calcola un punteggio per distinguere un vero brano musicale da un video di YouTuber.
    Restituisce un punteggio negativo se il video non è musicale.
    """
    title = (entry.get("title") or "").lower()
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    duration = int(entry.get("duration") or 0)
    query_lower = original_query.lower()

    # Escludi video troppo corti (< 35s, es. Shorts/Meme) o troppo lunghi (> 12 min, es. Vlog/Gameplay)
    if duration > 0 and (duration < 35 or duration > 720):
        return -100

    # Escludi parole da YouTuber/Gameplay a meno che non siano esplicitamente nella query
    for bad_word in NON_MUSIC_BLACKLIST:
        if bad_word in title and bad_word not in query_lower:
            return -100

    score = 0
    # Canali ufficiali di YouTube Music ("Artista - Topic") o VEVO hanno priorità assoluta
    if "- topic" in channel or "vevo" in channel:
        score += 50
    if "official" in channel or "records" in channel or "music" in channel:
        score += 20

    # Titoli tipici di tracce musicali
    if "official audio" in title or "audio ufficiale" in title:
        score += 35
    elif "lyrics" in title or "testo" in title or "lyric video" in title:
        score += 25
    elif "official video" in title or "video ufficiale" in title or "official music video" in title:
        score += 20

    # Durata tipica di una canzone (1m30s - 6m00s)
    if 90 <= duration <= 360:
        score += 15

    return score


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
            "source_address": "0.0.0.0",  # Forza IPv4 per evitare Read Timeout su googlevideo.com in Docker
            "socket_timeout": 8,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_vr", "ios", "tv", "web"],
                }
            },
        }
        if self.cookies_path.exists() and self.cookies_path.stat().st_size > 32:
            opts["cookiefile"] = str(self.cookies_path)
        return opts

    def _select_best_music_url(self, query: str) -> str:
        """
        Cerca su YouTube filtrando esclusivamente brani musicali (Topic, VEVO, Official Audio)
        ed escludendo video di YouTuber, gameplay o vlog.
        """
        search_query = query
        if not any(
            w in query.lower()
            for w in ("official", "audio", "lyrics", "song", "canzone", "brano")
        ):
            search_query = f"{query} official audio"

        flat_opts = {
            **self._get_base_options(),
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "default_search": "ytsearch6",
            "ignoreerrors": True,
        }
        try:
            with YoutubeDL(flat_opts) as ytdlp:
                info = ytdlp.extract_info(search_query, download=False)
                entries = [e for e in (info.get("entries") or []) if e] if info else []
                if entries:
                    scored = sorted(
                        entries,
                        key=lambda e: _score_music_candidate(e, query),
                        reverse=True,
                    )
                    best = scored[0]
                    raw_url = best.get("url") or best.get("webpage_url") or best.get("id")
                    if raw_url:
                        if not str(raw_url).startswith("http"):
                            raw_url = f"https://www.youtube.com/watch?v={raw_url}"
                        logging.info(
                            f"[Filtro Musica] Scelto brano: '{best.get('title')}' "
                            f"(canale: {best.get('channel') or best.get('uploader')})"
                        )
                        return raw_url
        except Exception as e:
            logging.debug(f"Ricerca selettiva musicale fallback: {e}")

        return f"ytsearch1:{search_query}"

    def _download_or_extract(self, target: str, is_search: bool = False) -> Optional[dict]:
        """
        Estrae e scarica rapidamente la traccia audio con IPv4 forzato e client anti-403.
        """
        resolved_target = self._select_best_music_url(target) if is_search else target

        file_token = uuid.uuid4().hex[:12]
        outtmpl = str(self.cache_dir / f"track_{file_token}.%(ext)s")

        # 1. Estrai metadati rapidi per capire se è una live stream (es. /lofi) o una canzone
        meta_opts = {
            **self._get_base_options(),
            "noplaylist": True,
        }
        with YoutubeDL(meta_opts) as ytdlp:
            info = ytdlp.extract_info(resolved_target, download=False)
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
        webpage_url = video.get("webpage_url") or video.get("url") or resolved_target
        headers = video.get("http_headers") or {}

        # 2. Se è una canzone standard (non live, <= 20 min), scaricala in locale con IPv4
        if not is_live and 0 < duration <= 1200:
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
                logging.warning(f"Download rapido fallito ({e}), uso stream diretto...")

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
                info = ytdlp.extract_info(f"{video_query} official audio", download=False)
                if not info:
                    return []
                entries = info.get("entries") if "entries" in info else [info]
                results = []
                for entry in entries or []:
                    if not entry:
                        continue
                    if _score_music_candidate(entry, video_query) < 0:
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
