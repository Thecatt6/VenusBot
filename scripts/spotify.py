import os
import aiohttp
import re
from dotenv import load_dotenv
from pathlib import Path
import logging


class SpotifyController:
    def __init__(self):
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.__client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip().strip("'\"")
        self.__client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip().strip("'\"")
        self.__refresh_token = (os.getenv("SPOTIFY_CLIENT_REFRESH_TOKEN") or "").strip().strip("'\"")
        self.__token_url = "https://accounts.spotify.com/api/token"
        self.__access_token = None

    def __get_payload(self):
        if self.__refresh_token:
            return {
                "grant_type": "refresh_token",
                "refresh_token": self.__refresh_token,
                "client_id": self.__client_id,
                "client_secret": self.__client_secret,
            }
        return {
            "grant_type": "client_credentials",
            "client_id": self.__client_id,
            "client_secret": self.__client_secret,
        }

    async def refresh_token(self):
        if not self.__client_id or not self.__client_secret:
            return None
        payload = self.__get_payload()
        async with aiohttp.ClientSession() as session:
            async with session.post(self.__token_url, data=payload) as response:
                if response.status == 200:
                    logging.debug("Token Spotify aggiornato con successo.")
                    data = await response.json()
                    self.__access_token = data.get("access_token")
                    return self.__access_token
                else:
                    text = await response.text()
                    logging.debug(f"Impossibile aggiornare token Spotify: {response.status} - {text}")
                    return None

    async def get_access_token(self):
        if self.__access_token is None:
            return await self.refresh_token()
        return self.__access_token

    def extract_track_id(self, spotify_url: str) -> str:
        match = re.search(r"spotify\.com/(?:intl-[a-z]+/)?track/([a-zA-Z0-9]+)", spotify_url)
        if match:
            return match.group(1)
        raise ValueError("URL traccia Spotify non valido")

    async def _fallback_oembed_track(self, spotify_url: str) -> dict | None:
        """Fallback gratuito senza chiavi API tramite Spotify oEmbed."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://open.spotify.com/oembed",
                    params={"url": spotify_url},
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title")
                        if title:
                            return {"title": title, "artist": ""}
        except Exception as e:
            logging.debug(f"Fallback oEmbed Spotify fallito: {e}")
        return None

    async def getSpotifySongInfo(self, spotify_url: str) -> dict:
        track_id = self.extract_track_id(spotify_url)
        api_url = f"https://api.spotify.com/v1/tracks/{track_id}"

        access_token = await self.get_access_token()
        if access_token:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {access_token}"}

                async def fetch_track() -> dict | None:
                    async with session.get(api_url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return {
                                "title": data["name"],
                                "artist": data["artists"][0]["name"] if data.get("artists") else "",
                            }
                        return None

                result = await fetch_track()
                if result is not None:
                    return result

                access_token = await self.refresh_token()
                if access_token:
                    headers = {"Authorization": f"Bearer {access_token}"}
                    result = await fetch_track()
                    if result is not None:
                        return result

        # Fallback oEmbed senza credenziali
        oembed_res = await self._fallback_oembed_track(spotify_url)
        if oembed_res:
            return oembed_res

        raise Exception("Impossibile recuperare informazioni sul brano Spotify.")

    async def getSpotifyPlaylistInfo(self, spotify_url: str) -> list:
        playlist_match = re.search(
            r"spotify\.com/(?:intl-[a-z]+/)?playlist/([a-zA-Z0-9]+)", spotify_url
        )
        album_match = re.search(
            r"spotify\.com/(?:intl-[a-z]+/)?album/([a-zA-Z0-9]+)", spotify_url
        )

        if playlist_match:
            endpoint = f"https://api.spotify.com/v1/playlists/{playlist_match.group(1)}"
        elif album_match:
            endpoint = f"https://api.spotify.com/v1/albums/{album_match.group(1)}"
        else:
            raise ValueError("URL playlist o album Spotify non valido")

        access_token = await self.get_access_token()
        if not access_token:
            raise Exception(
                "Credenziali Spotify non configurate nel file .env per caricare intere playlist."
            )

        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {access_token}"}

            async def fetch_items():
                async with session.get(endpoint, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("name", "Playlist Spotify")
                        thumbnail = data["images"][0]["url"] if data.get("images") else None
                        items = data.get("tracks", {}).get("items", [])
                        track_list = [
                            {
                                "title": (
                                    item["track"]["name"]
                                    if "playlist" in endpoint
                                    else item["name"]
                                ),
                                "artist": (
                                    item["track"]["artists"][0]["name"]
                                    if "playlist" in endpoint
                                    else item["artists"][0]["name"]
                                ),
                            }
                            for item in items
                            if (item.get("track") if "playlist" in endpoint else item.get("name"))
                        ]
                        return [{"title": title, "thumbnail": thumbnail}] + track_list
                    return None

            result = await fetch_items()
            if result is not None:
                return result

            access_token = await self.refresh_token()
            headers = {"Authorization": f"Bearer {access_token}"}
            result = await fetch_items()
            if result is not None:
                return result

            raise Exception("Impossibile recuperare la playlist/album da Spotify.")
