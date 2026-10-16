import asyncio
from collections import defaultdict, deque
from datetime import datetime
import json
import logging
import os
import re
from typing import Any, Deque, Dict, Optional
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
    - Riconosce istantaneamente comandi musicali in italiano e inglese (Fast-Path)
    - Risponde a qualsiasi domanda generale, curiosità, calcolo o richiesta complessa
      usando i migliori provider Cloud AI gratuiti:
        1. Groq Cloud (Llama 3.3 70B - gratuito e ultra-rapido con GROQ_API_KEY)
        2. Google Gemini (Gemini 2.0 Flash - gratuito con GEMINI_API_KEY)
        3. Pollinations Cloud AI (Fallback cloud gratuito zero-config senza API key)
    - Mantiene memoria conversazionale per ogni server Discord.
    """

    def __init__(self):
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("'\"")
        self.gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip().strip("'\"")
        self.groq_model = (os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()
        self.gemini_model = (os.getenv("GEMINI_MODEL") or "gemini-2.0-flash").strip()

        # Memoria breve conversazionale per guild_id (ultimi 6 scambi)
        self._history: Dict[int, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=12))

    def clear_history(self, guild_id: int) -> None:
        self._history.pop(guild_id, None)

    def _match_fast_command(self, text: str) -> Optional[AssistantResponse]:
        """
        Analizza se la frase è un comando diretto ed esplicito (musica, volume, ora/data)
        per eseguirlo a latenza zero senza attendere chiamate LLM.
        """
        raw = text.strip()
        # Rimuovi punteggiatura finale per il matching
        clean = re.sub(r"[.!?]+$", "", raw).strip()
        lower = clean.lower()

        if not lower:
            return AssistantResponse(
                action="none",
                voice_reply="Sì? Dimmi pure, ti ascolto.",
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

        # 4. Stop / Ferma tutto
        if re.match(
            r"^(?:stop|stoppa(?:\s+tutto)?|ferma(?:\s+tutto|\s+la\s+musica|\s+la\s+riproduzione)?|basta(?:\s+musica|\s+così)?|svuota(?:\s+la)?\s+coda|cancella(?:\s+la)?\s+coda|spegni(?:\s+la)?\s+musica)$",
            lower,
        ):
            return AssistantResponse(
                action="stop",
                voice_reply="Fermo la musica e svuoto la coda.",
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

        # 11. Riproduzione diretta di una canzone specifica
        # (Se l'utente chiede un consiglio vago come "metti qualcosa di...", lasciamo fare all'AI!)
        m_play = re.match(
            r"^(?:riproduci|metti(?:\s+su)?|suona|fai\s+partire|ascoltiamo|play)\s+(.+)$",
            clean,
            flags=re.IGNORECASE,
        )
        if m_play:
            song_query = m_play.group(1).strip()
            vague_prefixes = (
                "qualcosa ",
                "una canzone ",
                "un brano ",
                "della musica ",
                "un pezzo ",
            )
            if not any(song_query.lower().startswith(vp) for vp in vague_prefixes):
                # Rimuovi eventuale "su youtube" / "su spotify" alla fine
                song_query = re.sub(
                    r"\s+(?:su\s+youtube|su\s+spotify|per\s+favore|per\s+piacere)$",
                    "",
                    song_query,
                    flags=re.IGNORECASE,
                ).strip()
                if song_query:
                    return AssistantResponse(
                        action="play",
                        query=song_query,
                        voice_reply=f"Cerco e riproduco {song_query}.",
                    )

        return None

    async def process_request(
        self,
        guild_id: int,
        user_name: str,
        user_text: str,
        current_song: Optional[str] = None,
        queue_count: int = 0,
    ) -> AssistantResponse:
        """
        Elabora la frase dell'utente:
        1. Prova prima il Fast-Path per comandi diretti immediati
        2. Altrimenti interroga l'assistente AI Cloud per rispondere a domande o interpretare
           richieste naturali/musicali in stile Google Assistant.
        """
        fast = self._match_fast_command(user_text)
        if fast is not None:
            return fast

        system_prompt = (
            "Sei Venus, un assistente vocale intelligente in stile Google Assistant integrato in un canale vocale Discord.\n"
            f"Data e ora attuale in Italia: {get_italian_datetime_str()}.\n"
            f"Utente che sta parlando: {user_name}.\n"
            f"Brano attualmente in riproduzione: {current_song or 'Nessuno'}.\n"
            f"Brani in coda: {queue_count}.\n\n"
            "COMPITO:\n"
            "L'utente ti ha parlato a voce in italiano. Può farti una domanda di cultura generale, attualità, matematica, "
            "conversazione, barzellette, consigli, oppure chiederti di controllare la musica.\n"
            "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido con questa struttura esatta:\n"
            "{\n"
            '  "action": "none" | "play" | "pause" | "resume" | "skip" | "stop" | "loop" | "shuffle" | "volume_up" | "volume_down" | "volume_set" | "now_playing" | "disconnect",\n'
            '  "query": "titolo e artista precisi da cercare su YouTube (solo se action è play) oppure numero 1-100 (solo se action è volume_set), altrimenti stringa vuota",\n'
            '  "voice_reply": "La tua risposta parlata in italiano naturale, amichevole, concisa (massimo 2-3 frasi brevi) e pronta per essere letta ad alta voce da un sintetizzatore vocale. Non usare elenchi puntati, asterischi o markdown."\n'
            "}\n"
            "Esempi:\n"
            '- Se l\'utente chiede "Chi ha scoperto l\'America?": {"action": "none", "query": "", "voice_reply": "L\'America è stata raggiunta da Cristoforo Colombo il 12 ottobre 1492."}\n'
            '- Se l\'utente chiede "Metti una canzone famosa dei Queen": {"action": "play", "query": "Queen Bohemian Rhapsody", "voice_reply": "Certamente, metto subito Bohemian Rhapsody dei Queen!"}\n'
            '- Se l\'utente chiede "Raccontami una battuta": {"action": "none", "query": "", "voice_reply": "Qual è il colmo per un elettricista? Non trovare mai la lampadina giusta per avere un\'idea brillante!"}'
        )

        history = list(self._history[guild_id])
        messages = [{"role": "system", "content": system_prompt}] + history + [
            {"role": "user", "content": user_text}
        ]

        raw_reply = None

        # 1. Prova Groq Cloud (se GROQ_API_KEY presente)
        if self.groq_api_key:
            raw_reply = await self._call_groq(messages)

        # 2. Prova Google Gemini Cloud (se GEMINI_API_KEY presente)
        if not raw_reply and self.gemini_api_key:
            raw_reply = await self._call_gemini(system_prompt, history, user_text)

        # 3. Fallback Cloud Gratuito Zero-Config (Pollinations OpenAI-compatible endpoint)
        if not raw_reply:
            raw_reply = await self._call_free_cloud_fallback(messages)

        parsed = self._parse_ai_response(raw_reply)

        # Aggiorna la memoria conversazionale
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
        payload = {
            "model": self.groq_model,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
        }
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
                    else:
                        err = await resp.text()
                        logging.warning(f"Groq API status {resp.status}: {err}")
        except Exception as e:
            logging.warning(f"Errore chiamata Groq AI: {e}")
        return None

    async def _call_gemini(
        self, system_prompt: str, history: list, user_text: str
    ) -> Optional[str]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent?key={self.gemini_api_key}"
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
                "temperature": 0.4,
                "maxOutputTokens": 300,
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
                    else:
                        err = await resp.text()
                        logging.warning(f"Gemini API status {resp.status}: {err}")
        except Exception as e:
            logging.warning(f"Errore chiamata Gemini AI: {e}")
        return None

    async def _call_free_cloud_fallback(self, messages: list) -> Optional[str]:
        """
        Endpoint Cloud AI gratuito senza API key (Pollinations OpenAI-compatible API).
        Garantisce che il bot possa sempre rispondere alle domande anche senza chiavi nel .env.
        """
        url = "https://text.pollinations.ai/openai"
        payload = {
            "model": "openai",
            "messages": messages,
            "temperature": 0.4,
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

    def _parse_ai_response(self, raw_reply: Optional[str]) -> AssistantResponse:
        if not raw_reply:
            return AssistantResponse(
                action="none",
                voice_reply="Mi dispiace, al momento non riesco a raggiungere il servizio di intelligenza artificiale.",
            )

        cleaned = raw_reply.strip()
        # Estrai eventuale blocco JSON se avvolto in ```json ... ```
        json_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if json_match:
            try:
                obj = json.loads(json_match.group(0))
                if isinstance(obj, dict):
                    action = str(obj.get("action") or "none").strip().lower()
                    query = str(obj.get("query") or "").strip()
                    voice_reply = str(obj.get("voice_reply") or obj.get("reply") or "").strip()
                    if not voice_reply and action == "play" and query:
                        voice_reply = f"Riproduco {query}."
                    return AssistantResponse(
                        action=action,
                        query=query,
                        voice_reply=voice_reply,
                    )
            except Exception as e:
                logging.debug(f"Parsing JSON risposta AI fallito, uso testo diretto: {e}")

        # Se l'AI ha risposto in testo semplice, usalo direttamente come risposta vocale
        plain_text = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL).strip()
        return AssistantResponse(action="none", query="", voice_reply=plain_text)
