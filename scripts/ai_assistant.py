import asyncio
from collections import defaultdict, deque
from datetime import datetime
import json
import logging
import os
import re
from typing import Any, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from pathlib import Path


ITALIAN_MONTHS = [
    "",
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
]

ITALIAN_WEEKDAYS = [
    "lunedì",
    "martedì",
    "mercoledì",
    "giovedì",
    "venerdì",
    "sabato",
    "domenica",
]

PREFERRED_GROQ_CHAT_MODELS = [
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]


def get_italian_datetime_str() -> str:
    try:
        now = datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:
        now = datetime.now()
    weekday = ITALIAN_WEEKDAYS[now.weekday()]
    month = ITALIAN_MONTHS[now.month]
    return f"{weekday} {now.day} {month} {now.year}, ore {now.strftime('%H:%M')}"


class AssistantResponse:
    def __init__(
        self,
        action: str = "none",
        query: str = "",
        voice_reply: str = "",
    ):
        self.action = (action or "none").strip().lower()
        self.query = (query or "").strip()
        self.voice_reply = (voice_reply or "").strip()

    def to_dict(self) -> Dict[str, str]:
        return {
            "action": self.action,
            "query": self.query,
            "voice_reply": self.voice_reply,
        }


class AIAssistant:
    """
    Cervello intelligente in stile Google Assistant per VenusBot.
    - Riconosce istantaneamente i comandi di controllo (pausa, salta, stop, volume, ora/data)
    - Corregge foneticamente i titoli di canzoni in inglese e italiano pronunciati a voce
      (es. "blanding laits" -> "The Weeknd - Blinding Lights")
    - Risponde a qualsiasi domanda generale parlando nel canale vocale.
    """

    def __init__(self):
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("'\"")
        self.gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip().strip("'\"")
        self.custom_groq_model = (os.getenv("GROQ_MODEL") or "").strip()
        self.gemini_model = (os.getenv("GEMINI_MODEL") or "gemini-2.0-flash").strip()

        self._discovered_groq_models: Optional[List[str]] = None
        self._history: Dict[int, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=12))

    def clear_history(self, guild_id: int) -> None:
        self._history.pop(guild_id, None)

    def _match_fast_command(self, text: str) -> Optional[AssistantResponse]:
        """
        Comandi di controllo istantaneo che non richiedono correzione fonetica di titoli musicali.
        """
        raw = text.strip()
        clean = re.sub(r"[.!?]+$", "", raw).strip()
        lower = clean.lower()

        if not lower:
            return AssistantResponse(
                action="listen_followup",
                voice_reply="Dimmi pure.",
            )

        # 1. Pausa
        if re.match(
            r"^(?:metti\s+in\s+pausa|pausa|pause|ferma\s+un\s+attimo|aspetta\s+un\s+attimo)$",
            lower,
        ):
            return AssistantResponse(action="pause", voice_reply="Metto in pausa.")

        # 2. Riprendi / Resume
        if re.match(
            r"^(?:riprendi(?:\s+la\s+musica|\s+la\s+riproduzione|\s+pure)?|continua(?:\s+la\s+musica)?|fai\s+ripartire|togli\s+(?:la\s+)?pausa|resume|riproduci|play)$",
            lower,
        ):
            return AssistantResponse(action="resume", voice_reply="Riprendo la riproduzione.")

        # 3. Skip / Salta
        if re.match(
            r"^(?:salta(?:\s+canzone|\s+la\s+canzone|\s+il\s+brano|\s+brano|\s+questa)?|skippa|skip|avanti|vai\s+avanti|prossima(?:\s+canzone)?|prossimo(?:\s+brano)?|next|cambia(?:\s+canzone|\s+brano|\s+musica)?)$",
            lower,
        ):
            return AssistantResponse(action="skip", voice_reply="Salto il brano.")

        # 4. Svuota solo la coda (senza fermare il brano attuale)
        if re.match(
            r"^(?:svuota(?:\s+la)?\s+coda|cancella(?:\s+la)?\s+coda|pulisci(?:\s+la)?\s+coda)$",
            lower,
        ):
            return AssistantResponse(
                action="clear_queue",
                voice_reply="Ho svuotato la coda di riproduzione.",
            )

        # 5. Stop / Ferma tutto
        if re.match(
            r"^(?:stop|stoppa(?:\s+tutto)?|ferma(?:\s+tutto|\s+la\s+musica|\s+la\s+riproduzione)?|basta(?:\s+musica|\s+così)?|spegni(?:\s+la)?\s+musica)$",
            lower,
        ):
            return AssistantResponse(
                action="stop",
                voice_reply="Fermo la musica e svuoto la coda.",
            )

        # 6. Replay / Ricomincia da capo
        if re.match(
            r"^(?:ricomincia(?:\s+la\s+canzone|\s+il\s+brano|\s+da\s+capo)?|rimetti(?:\s+da\s+capo|\s+dall'inizio)|da\s+capo|dall'inizio|replay|restart)$",
            lower,
        ):
            return AssistantResponse(
                action="replay",
                voice_reply="Rimetto il brano dall'inizio.",
            )

        # 7. Autoplay (riproduzione automatica di brani correlati)
        if re.match(
            r"^(?:(?:attiva|disattiva|metti|togli)\s+(?:l')?autoplay|autoplay|riproduzione\s+automatica)$",
            lower,
        ):
            return AssistantResponse(
                action="autoplay",
                voice_reply="",
            )

        # 5. Loop / Ripetizione
        if re.match(
            r"^(?:metti\s+in\s+loop|togli\s+(?:il\s+)?loop|loop|ripeti(?:\s+la\s+canzone|\s+canzone|\s+il\s+brano|\s+brano)?|ripetizione)$",
            lower,
        ):
            return AssistantResponse(action="loop", voice_reply="")

        # 6. Shuffle / Mescola
        if re.match(
            r"^(?:mescola(?:\s+la\s+coda|\s+le\s+canzoni)?|mischia(?:\s+la\s+coda|\s+le\s+canzoni)?|shuffle|riproduzione\s+casuale|ordine\s+casuale)$",
            lower,
        ):
            return AssistantResponse(
                action="shuffle",
                voice_reply="Ho mescolato le canzoni in coda.",
            )

        # 7. Volume
        m_vol_set = re.match(
            r"^(?:imposta\s+(?:il\s+)?volume\s+(?:a|al)|metti\s+(?:il\s+)?volume\s+(?:a|al)|volume\s+(?:a|al)?)\s*(\d{1,3})\s*%?$",
            lower,
        )
        if m_vol_set:
            vol = max(1, min(100, int(m_vol_set.group(1))))
            return AssistantResponse(
                action="volume_set",
                query=str(vol),
                voice_reply=f"Imposto il volume al {vol} percento.",
            )

        if re.match(r"^(?:alza(?:\s+un\s+po')?\s+(?:il\s+)?volume|più\s+forte|alza\s+la\s+musica)$", lower):
            return AssistantResponse(action="volume_up", voice_reply="Alzo il volume.")

        if re.match(r"^(?:abbassa(?:\s+un\s+po')?\s+(?:il\s+)?volume|più\s+piano|abbassa\s+la\s+musica)$", lower):
            return AssistantResponse(action="volume_down", voice_reply="Abbasso il volume.")

        # 8. Che canzone sta suonando?
        if re.match(
            r"^(?:che\s+canzone\s+(?:e|è)(?:\s+questa)?|cosa\s+sta\s+(?:suonando|andando)|come\s+si\s+chiama\s+(?:questa\s+canzone|questo\s+brano)|chi\s+canta(?:\s+questa\s+canzone)?|che\s+brano\s+(?:e|è))$",
            lower,
        ):
            return AssistantResponse(action="now_playing")

        # 9. Disconnessione vocale
        if re.match(
            r"^(?:esci(?:\s+dal\s+canale|\s+dalla\s+vocale)?|disconnettiti|vai\s+via)$",
            lower,
        ):
            return AssistantResponse(
                action="disconnect",
                voice_reply="Va bene, mi disconnetto dal canale vocale. A presto!",
            )

        # 10. Domande rapide su ora / data
        if re.match(r"^(?:che\s+ore\s+sono|che\s+ora\s+(?:e|è)|dimmi\s+l'ora)$", lower):
            try:
                now = datetime.now(ZoneInfo("Europe/Rome"))
            except Exception:
                now = datetime.now()
            return AssistantResponse(
                action="none",
                voice_reply=f"Sono le {now.strftime('%H e %M')}.",
            )

        if re.match(r"^(?:che\s+giorno\s+(?:e|è)(?:\s+oggi)?|quanti\s+ne\s+abbiamo(?:\s+oggi)?|che\s+data\s+(?:e|è)(?:\s+oggi)?)$", lower):
            return AssistantResponse(
                action="none",
                voice_reply=f"Oggi è {get_italian_datetime_str().split(',')[0]}.",
            )

        return None

    @staticmethod
    def _extract_Explicit_play_query(text: str) -> Optional[str]:
        """Se la frase inizia con 'metti/riproduci/suona/play ...', estrae la query grezza per eventuale fallback."""
        clean = re.sub(r"[.!?]+$", "", text.strip()).strip()
        m_play = re.match(
            r"^(?:riproduci|metti(?:\s+su)?|suona|fai\s+partire|ascoltiamo|play)\s+(.+)$",
            clean,
            flags=re.IGNORECASE,
        )
        if m_play:
            q = m_play.group(1).strip()
            q = re.sub(
                r"\s+(?:su\s+youtube|su\s+spotify|per\s+favore|per\s+piacere)$",
                "",
                q,
                flags=re.IGNORECASE,
            ).strip()
            return q or None
        return None

    async def _get_groq_candidate_models(self, session: aiohttp.ClientSession) -> List[str]:
        if self._discovered_groq_models:
            return self._discovered_groq_models

        candidates: List[str] = []
        if self.custom_groq_model:
            candidates.append(self.custom_groq_model)

        try:
            headers = {"Authorization": f"Bearer {self.groq_api_key}"}
            async with session.get("https://api.groq.com/openai/v1/models", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    available_ids = [
                        m.get("id", "")
                        for m in (data.get("data") or [])
                        if m.get("id")
                    ]
                    excluded_keywords = (
                        "whisper",
                        "tts",
                        "playai",
                        "guard",
                        "safeguard",
                        "embed",
                        "orpheus",
                        "allam",
                    )
                    chat_ids = [
                        mid
                        for mid in available_ids
                        if not any(ex in mid.lower() for ex in excluded_keywords)
                    ]

                    for pref in PREFERRED_GROQ_CHAT_MODELS:
                        if pref in chat_ids and pref not in candidates:
                            candidates.append(pref)

                    for cid in chat_ids:
                        if cid not in candidates:
                            candidates.append(cid)

                    if candidates:
                        self._discovered_groq_models = candidates
                        logging.info(f"Modelli Groq rilevati: {candidates[:3]}")
                        return candidates
        except Exception as e:
            logging.debug(f"Impossibile elencare i modelli Groq: {e}")

        for pref in PREFERRED_GROQ_CHAT_MODELS:
            if pref not in candidates:
                candidates.append(pref)
        return candidates

    async def process_request(
        self,
        guild_id: int,
        user_name: str,
        user_text: str,
        current_song: Optional[str] = None,
        queue_count: int = 0,
    ) -> AssistantResponse:
        fast = self._match_fast_command(user_text)
        if fast is not None:
            return fast

        explicit_song_fallback = self._extract_Explicit_play_query(user_text)

        system_prompt = (
            "Sei Venus, un assistente vocale intelligente in stile Google Assistant in un canale vocale Discord.\n"
            f"Data e ora attuale in Italia: {get_italian_datetime_str()}.\n"
            f"Utente: {user_name}. Brano attuale: {current_song or 'Nessuno'}. Brani in coda: {queue_count}.\n\n"
            "REGOLA FONDAMENTALE PER LA MUSICA:\n"
            "- Se l'utente chiede di mettere/riprodurre/suonare una canzone o un artista (in italiano o in inglese), "
            "imposta \"action\": \"play\".\n"
            "- Poiché l'utente parla al microfono con accento italiano, i titoli o gli artisti INGLESI potrebbero essere "
            "trascritti foneticamente o storpiati (es. 'blanding laits' -> 'The Weeknd - Blinding Lights', "
            "'smels laik tin spirit' -> 'Nirvana - Smells Like Teen Spirit', 'boemian rapsodi' -> 'Queen - Bohemian Rhapsody', "
            "'fein' -> 'Travis Scott - FE!N', 'starboi' -> 'The Weeknd - Starboy', 'nam' -> 'Linkin Park - Numb').\n"
            "- Ricostruisci SEMPRE il vero titolo ufficiale della canzone e l'artista musicale reale nel campo \"query\" "
            "usando il formato 'Artista - Titolo Canzone'. NON cercare MAI video di YouTuber, gameplay o vlog: solo vere canzoni!\n\n"
            "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido:\n"
            "{\n"
            '  "action": "none" | "play" | "pause" | "resume" | "skip" | "stop" | "replay" | "clear_queue" | "autoplay" | "loop" | "shuffle" | "volume_up" | "volume_down" | "volume_set" | "now_playing" | "disconnect",\n'
            '  "query": "Artista - Titolo Canzone (solo se action è play) oppure numero 1-100 (solo se action è volume_set), altrimenti stringa vuota",\n'
            '  "voice_reply": "Risposta parlata in italiano naturale e concisa (1-2 frasi brevi), senza asterischi o markdown."\n'
            "}"
        )

        history = list(self._history[guild_id])
        messages = [{"role": "system", "content": system_prompt}] + history + [
            {"role": "user", "content": user_text}
        ]

        raw_reply = None

        if self.groq_api_key:
            raw_reply = await self._call_groq(messages)

        if not raw_reply and self.gemini_api_key:
            raw_reply = await self._call_gemini(system_prompt, history, user_text)

        if not raw_reply:
            raw_reply = await self._call_free_cloud_fallback(messages)

        if not raw_reply and explicit_song_fallback:
            return AssistantResponse(
                action="play",
                query=explicit_song_fallback,
                voice_reply=f"Riproduco {explicit_song_fallback}.",
            )

        parsed = self._parse_ai_response(raw_reply, explicit_song_fallback)

        self._history[guild_id].append({"role": "user", "content": user_text})
        self._history[guild_id].append(
            {"role": "assistant", "content": parsed.voice_reply}
        )

        return parsed

    async def _call_groq(self, messages: list) -> Optional[str]:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                models = await self._get_groq_candidate_models(session)
                for model_name in models[:4]:
                    payload = {
                        "model": model_name,
                        "messages": messages,
                        "temperature": 0.3,
                        "max_tokens": 350,
                    }
                    async with session.post(url, headers=headers, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            content = data["choices"][0]["message"].get("content") or ""
                            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                            if content:
                                return content
                        elif resp.status == 404:
                            if self._discovered_groq_models and model_name in self._discovered_groq_models:
                                self._discovered_groq_models.remove(model_name)
                            continue
                        else:
                            err = await resp.text()
                            logging.debug(f"Groq modello {model_name} status {resp.status}: {err}")
        except Exception as e:
            logging.warning(f"Errore chiamata Groq AI: {e}")
        return None

    async def _call_gemini(
        self, system_prompt: str, history: list, user_text: str
    ) -> Optional[str]:
        for model_name in (self.gemini_model, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"):
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={self.gemini_api_key}"
            )
            contents = []
            for msg in history:
                role = "user" if msg["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": msg["content"]}]})
            contents.append({"role": "user", "parts": [{"text": user_text}]})

            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": contents,
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 350,
                    "responseMimeType": "application/json",
                },
            }
            try:
                timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                if parts:
                                    return parts[0].get("text")
                        elif resp.status == 404:
                            continue
            except Exception as e:
                logging.debug(f"Errore chiamata Gemini ({model_name}): {e}")
        return None

    async def _call_free_cloud_fallback(self, messages: list) -> Optional[str]:
        url = "https://text.pollinations.ai/openai"
        payload = {
            "model": "openai",
            "messages": messages,
            "temperature": 0.3,
            "jsonMode": True,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "choices" in data:
                            return data["choices"][0]["message"]["content"]
                        return await resp.text()
        except Exception as e:
            logging.warning(f"Errore fallback cloud AI: {e}")
        return None

    def _parse_ai_response(
        self, raw_reply: Optional[str], explicit_song_fallback: Optional[str] = None
    ) -> AssistantResponse:
        if not raw_reply:
            if explicit_song_fallback:
                return AssistantResponse(
                    action="play",
                    query=explicit_song_fallback,
                    voice_reply=f"Riproduco {explicit_song_fallback}.",
                )
            return AssistantResponse(
                action="none",
                voice_reply="Mi dispiace, al momento non riesco a elaborare la risposta.",
            )

        cleaned = raw_reply.strip()
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

        json_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if json_match:
            try:
                obj = json.loads(json_match.group(0))
                if isinstance(obj, dict):
                    action = str(obj.get("action") or "none").strip().lower()
                    query = str(obj.get("query") or "").strip()
                    voice_reply = str(obj.get("voice_reply") or obj.get("reply") or "").strip()
                    if action == "play" and not query and explicit_song_fallback:
                        query = explicit_song_fallback
                    if not voice_reply and action == "play" and query:
                        voice_reply = f"Riproduco {query}."
                    return AssistantResponse(
                        action=action,
                        query=query,
                        voice_reply=voice_reply,
                    )
            except Exception as e:
                logging.debug(f"Parsing JSON risposta AI fallito, uso testo diretto: {e}")

        if explicit_song_fallback:
            return AssistantResponse(
                action="play",
                query=explicit_song_fallback,
                voice_reply=f"Riproduco {explicit_song_fallback}.",
            )

        plain_text = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL).strip()
        return AssistantResponse(action="none", query="", voice_reply=plain_text)
