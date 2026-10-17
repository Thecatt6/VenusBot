import asyncio
from collections import defaultdict, deque
from datetime import datetime
import json
import logging
import os
import re
from typing import Any, Deque, Dict, List, Optional, Tuple
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

ITALIAN_WORD_NUMBERS = {
    "zero": 0,
    "cinque": 5,
    "dieci": 10,
    "quindici": 15,
    "venti": 20,
    "venticinque": 25,
    "trenta": 30,
    "trentacinque": 35,
    "quaranta": 40,
    "quarantacinque": 45,
    "cinquanta": 50,
    "meta": 50,
    "metà": 50,
    "cinquantacinque": 55,
    "sessanta": 60,
    "sessantacinque": 65,
    "settanta": 70,
    "settantacinque": 75,
    "ottanta": 80,
    "ottantacinque": 85,
    "novanta": 90,
    "novantacinque": 95,
    "cento": 100,
    "massimo": 100,
    "minimo": 15,
}

# Correzioni fonetiche istantanee in locale (0ms)
LOCAL_PHONETIC_FIXES = {
    "blanding laits": "The Weeknd - Blinding Lights",
    "blinding laits": "The Weeknd - Blinding Lights",
    "blanding light": "The Weeknd - Blinding Lights",
    "biondi light": "The Weeknd - Blinding Lights",
    "starboi": "The Weeknd - Starboy",
    "boemian rapsodi": "Queen - Bohemian Rhapsody",
    "bohemian rapsodi": "Queen - Bohemian Rhapsody",
    "smels laik tin spirit": "Nirvana - Smells Like Teen Spirit",
    "fein": "Travis Scott - FE!N",
    "fin": "Travis Scott - FE!N",
    "ander presciur": "Queen - Under Pressure",
    "hotel california": "Eagles - Hotel California",
    "biliv": "Cher - Believe",
    "auan mor taim": "Daft Punk - One More Time",
    "get lachi": "Daft Punk - Get Lucky",
    "sceip of iu": "Ed Sheeran - Shape of You",
    "shape of iu": "Ed Sheeran - Shape of You",
    "beliver": "Imagine Dragons - Believer",
    "ander de brig": "Red Hot Chili Peppers - Under the Bridge",
    "nam": "Linkin Park - Numb",
    "in de end": "Linkin Park - In the End",
}

PREFERRED_GROQ_CHAT_MODELS = [
    "llama-3.1-8b-instant",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3-32b",
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


def split_en_hint(text: str) -> Tuple[str, Optional[str]]:
    """Separa eventuale [EN_HINT: ...] aggiunto dal doppio riconoscitore vocale it-IT + en-US."""
    m = re.search(r"\[EN_HINT:\s*(.+?)\]$", text.strip(), flags=re.IGNORECASE)
    if m:
        en_hint = m.group(1).strip()
        it_text = text[: m.start()].strip()
        return it_text, en_hint
    return text.strip(), None


def _normalize_command_text(text: str) -> str:
    """
    Pulisce il testo rimuovendo formule di cortesia e riempitivi ("per favore", "puoi", "grazie", ecc.)
    per permettere il riconoscimento locale istantaneo in <0.2ms senza passare per l'LLM.
    """
    s, _ = split_en_hint(text)
    s = re.sub(r"[.!?]+$", "", s.strip().lower()).strip()
    s = re.sub(
        r"\s+(?:per\s+favore|per\s+piacere|per\s+cortesia|cortesemente|grazie(?:\s+mille)?|adesso|ora|subito|un\s+attimo)$",
        "",
        s,
    ).strip()
    s = re.sub(
        r"^(?:per\s+favore|per\s+piacere|cortesemente|mi\s+puoi|puoi|potresti|vorrei\s+che|riesci\s+a|fai)\s+",
        "",
        s,
    ).strip()
    return s


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
    Motore Ibrido per VenusBot:
    1. Comandi di controllo (pausa, salta, stop, volume, loop, shuffle, autoplay, replay, ora/data):
       100% LOCALI in < 0.2ms (zero chiamate di rete!).
    2. Richieste di riproduzione musicale ("metti <canzone>"):
       Usa la doppia trascrizione (Italiano + Inglese) e un micro-risolutore fonetico ultra-veloce
       per identificare con precisione assoluta qualsiasi canzone italiana o inglese.
    """

    def __init__(self):
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("'\"")
        self.gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip().strip("'\"")
        self.custom_groq_model = (os.getenv("GROQ_MODEL") or "").strip()
        self.gemini_model = (os.getenv("GEMINI_MODEL") or "gemini-2.0-flash").strip()

        self._discovered_groq_models: Optional[List[str]] = None
        self._history: Dict[int, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=10))
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6)
            )
        return self._http_session

    def clear_history(self, guild_id: int) -> None:
        self._history.pop(guild_id, None)

    def _match_fast_command(self, text: str) -> Optional[AssistantResponse]:
        """
        Elaborazione 100% LOCALE (< 0.2ms, zero LLM) per tutti i comandi di controllo
        del player, regolazione volume, radio 24/7, orario, data e saluti.
        """
        it_raw, _ = split_en_hint(text)
        lower = _normalize_command_text(it_raw)

        if not lower:
            return AssistantResponse(
                action="listen_followup",
                voice_reply="",
            )

        # Se l'utente ha detto solo "Venus, metti" / "Venus, riproduci" / "Venus, suona" e ha fatto pausa
        # prima di dire il titolo della canzone, apri la finestra di ascolto dedicata al brano!
        if re.match(
            r"^(?:metti(?:\s+su)?|mettere|riproduci|riprodurre|suona|suonare|ascoltiamo|fammi\s+ascoltare|voglio\s+ascoltare|cerca|play)(?:\s+una\s+canzone|\s+un\s+brano|\s+musica)?$",
            lower,
        ):
            return AssistantResponse(
                action="listen_song_followup",
                voice_reply="Dimmi il titolo.",
            )

        # 1. Pausa
        if re.match(
            r"^(?:(?:metti|mettere)\s+in\s+pausa(?:\s+la\s+musica|\s+la\s+canzone|\s+il\s+brano)?|pausa(?:\s+la\s+musica|\s+la\s+canzone)?|pause|ferma\s+un\s+attimo|aspetta(?:\s+un\s+attimo)?)$",
            lower,
        ):
            return AssistantResponse(action="pause", voice_reply="Musica in pausa.")

        # 2. Riprendi / Resume
        if re.match(
            r"^(?:(?:riprendi|riprendere)(?:\s+la\s+musica|\s+la\s+riproduzione|\s+la\s+canzone|\s+pure)?|(?:continua|continuare)(?:\s+la\s+musica|\s+a\s+suonare)?|(?:fai\s+)?ripartire(?:\s+la\s+musica|\s+la\s+canzone)?|(?:togli|togliere)\s+(?:la\s+)?pausa|resume)$",
            lower,
        ):
            return AssistantResponse(action="resume", voice_reply="Riprendo la musica.")

        # 3. Skip / Salta / Cambia canzone
        if re.match(
            r"^(?:(?:salta|saltare|skippa|skippare)(?:\s+questa)?(?:\s+canzone|\s+la\s+canzone|\s+il\s+brano|\s+brano|\s+traccia)?|skip|avanti|vai\s+avanti|(?:vai\s+alla\s+)?prossima(?:\s+canzone|\s+traccia)?|prossimo(?:\s+brano)?|next|(?:cambia|cambiare)(?:\s+canzone|\s+brano|\s+musica|\s+traccia)?)$",
            lower,
        ):
            return AssistantResponse(action="skip", voice_reply="Salto il brano.")

        # 4. Svuota solo la coda
        if re.match(
            r"^(?:(?:svuota|svuotare|cancella|cancellare|pulisci|pulire)\s+(?:la\s+)?coda)$",
            lower,
        ):
            return AssistantResponse(
                action="clear_queue",
                voice_reply="Ho svuotato la coda di riproduzione.",
            )

        # 5. Stop / Ferma tutto
        if re.match(
            r"^(?:stop|(?:stoppa|stoppare)(?:\s+tutto|\s+la\s+musica)?|(?:ferma|fermare)(?:\s+tutto|\s+la\s+musica|\s+la\s+riproduzione|\s+la\s+canzone)?|basta(?:\s+musica|\s+così|\s+canzoni)?|(?:spegni|spegnere)(?:\s+la)?\s+musica)$",
            lower,
        ):
            return AssistantResponse(
                action="stop",
                voice_reply="Fermo la musica e svuoto la coda.",
            )

        # 6. Replay / Ricomincia da capo
        if re.match(
            r"^(?:(?:ricomincia|ricominciare)(?:\s+la\s+canzone|\s+il\s+brano|\s+da\s+capo)?|(?:rimetti|rimettere)(?:\s+da\s+capo|\s+dall'inizio|\s+la\s+canzone)|da\s+capo|dall'inizio|replay|restart)$",
            lower,
        ):
            return AssistantResponse(
                action="replay",
                voice_reply="Rimetto la canzone dall'inizio.",
            )

        # 7. Autoplay
        if re.match(
            r"^(?:(?:attiva|attivare|disattiva|disattivare|metti|togli)\s+(?:l')?autoplay|autoplay|riproduzione\s+automatica)$",
            lower,
        ):
            return AssistantResponse(action="autoplay", voice_reply="")

        # 8. Loop / Ripetizione
        if re.match(
            r"^(?:(?:metti|mettere)\s+in\s+loop|(?:togli|togliere)\s+(?:il\s+)?loop|loop|(?:ripeti|ripetere)(?:\s+la\s+canzone|\s+canzone|\s+il\s+brano|\s+brano|\s+sempre)?|ripetizione)$",
            lower,
        ):
            return AssistantResponse(action="loop", voice_reply="")

        # 9. Shuffle / Mescola
        if re.match(
            r"^(?:(?:mescola|mescolare|mischia|mischiare)(?:\s+la\s+coda|\s+le\s+canzoni|\s+i\s+brani)?|shuffle|riproduzione\s+casuale|ordine\s+casuale)$",
            lower,
        ):
            return AssistantResponse(
                action="shuffle",
                voice_reply="Ho mescolato la coda di riproduzione.",
            )

        # 10. Volume (cifre o parole italiane)
        m_vol_num = re.search(
            r"^(?:(?:imposta|impostare|metti|mettere|porta|portare|abbassa|alza)\s+(?:il\s+)?)?volume\s+(?:a|al|su)?\s*(\d{1,3})\s*(?:%|percento)?$",
            lower,
        )
        if m_vol_num:
            vol = max(5, min(150, int(m_vol_num.group(1))))
            return AssistantResponse(
                action="volume_set",
                query=str(vol),
                voice_reply=f"Volume impostato al {vol} percento.",
            )

        for word_num, val in ITALIAN_WORD_NUMBERS.items():
            if re.search(
                rf"^(?:(?:imposta|metti|porta)\s+(?:il\s+)?)?volume\s+(?:a|al|su|alla)?\s*{re.escape(word_num)}$",
                lower,
            ):
                return AssistantResponse(
                    action="volume_set",
                    query=str(val),
                    voice_reply=f"Volume impostato al {val} percento.",
                )

        if re.match(
            r"^(?:(?:alza|alzare|aumenta|aumentare)(?:\s+un\s+po'|\s+un\s+pochino|\s+tanto)?(?:\s+il\s+volume|\s+la\s+musica|\s+la\s+voce)?|più\s+forte|volume\s+su)$",
            lower,
        ):
            return AssistantResponse(action="volume_up", voice_reply="Alzo il volume.")

        if re.match(
            r"^(?:(?:abbassa|abbassare|diminuisci|riduci)(?:\s+un\s+po'|\s+un\s+pochino|\s+tanto)?(?:\s+il\s+volume|\s+la\s+musica|\s+la\s+voce)?|più\s+piano|volume\s+giù)$",
            lower,
        ):
            return AssistantResponse(action="volume_down", voice_reply="Abbasso il volume.")

        # 11. Che canzone sta suonando?
        if re.match(
            r"^(?:che\s+(?:canzone|brano)\s+(?:e|è)(?:\s+questa|\s+questo)?|(?:che\s+(?:canzone|brano|musica)|cosa)\s+sta\s+(?:suonando|andando|riproducendo)|come\s+si\s+chiama\s+(?:questa\s+canzone|questo\s+brano|la\s+canzone)|chi\s+canta(?:\s+questa\s+canzone)?|titolo(?:\s+della\s+canzone)?)$",
            lower,
        ):
            return AssistantResponse(action="now_playing")

        # 12. Disconnessione vocale
        if re.match(
            r"^(?:(?:esci|uscire)(?:\s+dal\s+canale|\s+dalla\s+vocale)?|disconnettiti|disconnetti|vai\s+via)$",
            lower,
        ):
            return AssistantResponse(
                action="disconnect",
                voice_reply="A presto!",
            )

        # 13. Radio 24/7 (Lofi / Jazz / Synthwave) in locale!
        if re.match(r"^(?:metti|riproduci|avvia|suona)\s+(?:la\s+)?(?:radio\s+)?lofi\s+jazz$", lower):
            return AssistantResponse(
                action="play",
                query="https://www.youtube.com/watch?v=HuFYqnbVbzY",
                voice_reply="Avvio la radio Lofi Jazz.",
            )
        if re.match(r"^(?:metti|riproduci|avvia|suona)\s+(?:la\s+)?(?:radio\s+)?lofi$", lower):
            return AssistantResponse(
                action="play",
                query="https://www.youtube.com/watch?v=jfKfPfyJRdk",
                voice_reply="Avvio la radio Lofi.",
            )
        if re.match(r"^(?:metti|riproduci|avvia|suona)\s+(?:la\s+)?(?:radio\s+)?synthwave$", lower):
            return AssistantResponse(
                action="play",
                query="https://www.youtube.com/watch?v=4xDzrJKXOOY",
                voice_reply="Avvio la radio Synthwave.",
            )

        # 14. Domande rapide su ora / data / saluti (100% in locale, 0ms!)
        if re.match(r"^(?:che\s+ore\s+sono|che\s+ora\s+(?:e|è)|dimmi\s+l'ora|ora\s+esatta)$", lower):
            try:
                now = datetime.now(ZoneInfo("Europe/Rome"))
            except Exception:
                now = datetime.now()
            return AssistantResponse(
                action="none",
                voice_reply=f"Sono le {now.strftime('%H e %M')}.",
            )

        if re.match(
            r"^(?:che\s+giorno\s+(?:e|è)(?:\s+oggi)?|quanti\s+ne\s+abbiamo(?:\s+oggi)?|che\s+data\s+(?:e|è)(?:\s+oggi)?)$",
            lower,
        ):
            return AssistantResponse(
                action="none",
                voice_reply=f"Oggi è {get_italian_datetime_str().split(',')[0]}.",
            )

        if re.match(r"^(?:ciao|ehi|hey|buongiorno|buonasera|salve)$", lower):
            return AssistantResponse(
                action="listen_followup",
                voice_reply="Ciao! Dimmi pure.",
            )

        if re.match(r"^(?:come\s+stai|tutto\s+bene|come\s+va)$", lower):
            return AssistantResponse(
                action="none",
                voice_reply="Tutto benissimo, grazie!",
            )

        if re.match(r"^(?:mi\s+senti|ci\s+sei|sei\s+attiva|funzioni)$", lower):
            return AssistantResponse(
                action="listen_followup",
                voice_reply="Sì, ti sento! Dimmi pure.",
            )

        if re.match(r"^(?:grazie(?:\s+mille|\s+tante)?|perfetto|ottimo|brava|grande)$", lower):
            return AssistantResponse(
                action="none",
                voice_reply="Di nulla!",
            )

        return None

    @staticmethod
    def _extract_Explicit_play_query(text: str) -> Optional[str]:
        """Estrae la canzone/artista da 'metti/riproduci/suona/play ...' preservando le maiuscole."""
        it_raw, _ = split_en_hint(text)
        clean = re.sub(r"[.!?]+$", "", it_raw.strip()).strip()
        clean = re.sub(
            r"^(?:per\s+favore|per\s+piacere|cortesemente|mi\s+puoi|puoi|potresti|vorrei\s+che|riesci\s+a)\s+",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        m_play = re.match(
            r"^(?:riproduci|riprodurre|metti(?:\s+su)?|mettere(?:\s+su)?|suona|suonare|fai\s+partire|ascoltiamo|fammi\s+ascoltare|voglio\s+ascoltare|cerca|play)\s+(.+)$",
            clean,
            flags=re.IGNORECASE,
        )
        if m_play:
            q = m_play.group(1).strip()
            q = re.sub(
                r"^(?:la\s+canzone|il\s+brano|un\s+brano\s+di|una\s+canzone\s+di|qualcosa\s+dei|qualcosa\s+di)\s+",
                "",
                q,
                flags=re.IGNORECASE,
            ).strip()
            q = re.sub(
                r"\s+(?:su\s+youtube|su\s+spotify|per\s+favore|per\s+piacere|grazie(?:\s+mille)?|adesso|ora|subito)$",
                "",
                q,
                flags=re.IGNORECASE,
            ).strip()
            return q or None
        return None

    async def resolve_song_query(self, raw_query: str, en_hint: Optional[str] = None) -> str:
        """
        Risolutore fonetico ultra-veloce (~200ms) specifico per i titoli delle canzoni:
        - Se il titolo è già nel dizionario fonetico locale, risponde in 0ms.
        - Altrimenti usa una micro-chiamata AI (max_tokens=45, temperature=0.0) combinando
          sia la trascrizione italiana sia l'eventuale trascrizione inglese (EN_HINT)
          per ricostruire il vero 'Artista - Titolo Canzone'.
        """
        clean_q = raw_query.strip()
        if not clean_q:
            return clean_q

        if clean_q.lower() in LOCAL_PHONETIC_FIXES:
            return LOCAL_PHONETIC_FIXES[clean_q.lower()]

        # Se non ci sono chiavi AI, usa l'hint inglese (se presente) o la query pulita
        if not self.groq_api_key and not self.gemini_api_key:
            if en_hint:
                en_extracted = self._extract_Explicit_play_query(en_hint)
                if en_extracted:
                    return en_extracted
            return clean_q

        hint_line = f"\nTrascrizione fonetica alternativa (en-US): \"{en_hint}\"" if en_hint else ""
        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "Sei un esperto musicale per un assistente vocale italiano. "
                    "L'utente ha chiesto al microfono di riprodurre una canzone o un artista, ma lo speech-to-text "
                    "potrebbe aver trascritto un titolo inglese o italiano con errori fonetici.\n"
                    "Restituisci SOLO ED ESCLUSIVAMENTE il vero nome nel formato 'Artista - Titolo Canzone' "
                    "(oppure il nome ufficiale dell'Artista se ha chiesto solo un artista), senza virgolette né spiegazioni.\n"
                    "Esempi:\n"
                    "- 'biondi light' -> The Weeknd - Blinding Lights\n"
                    "- 'smels laik tin spirit' -> Nirvana - Smells Like Teen Spirit\n"
                    "- 'boemia rapsodia' -> Queen - Bohemian Rhapsody\n"
                    "- 'nam dei linkin park' -> Linkin Park - Numb\n"
                    "- 'cenere di lazza' -> Lazza - Cenere\n"
                    "- 'geolier' -> Geolier"
                ),
            },
            {
                "role": "user",
                "content": f"Trascrizione vocale (it-IT): \"{clean_q}\"{hint_line}",
            },
        ]

        try:
            if self.groq_api_key:
                session = await self._get_session()
                models = await self._get_groq_candidate_models(session)
                if models:
                    payload = {
                        "model": models[0],
                        "messages": prompt_messages,
                        "temperature": 0.0,
                        "max_tokens": 40,
                    }
                    headers = {
                        "Authorization": f"Bearer {self.groq_api_key}",
                        "Content-Type": "application/json",
                    }
                    async with session.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=2.2),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            resolved = (
                                data["choices"][0]["message"]["content"] or ""
                            ).strip()
                            resolved = re.sub(r"<think>.*?</think>", "", resolved, flags=re.DOTALL).strip()
                            resolved = resolved.strip("\"'`.").split("\n")[0].strip()
                            if resolved and len(resolved) <= 90:
                                logging.info(
                                    f"[Risolutore Canzoni AI] '{clean_q}' -> '{resolved}'"
                                )
                                return resolved
        except Exception as e:
            logging.debug(f"Risolutore rapido canzoni fallback: {e}")

        return clean_q

    async def suggest_related_song(
        self, seed_title: str, exclude_titles: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        Suggerisce tramite l'AI un brano correlato dello stesso genere/artista per la modalità Autoplay.
        """
        if not self.groq_api_key:
            return None
        excluded_str = ", ".join((exclude_titles or [])[-15:])
        messages = [
            {
                "role": "system",
                "content": (
                    "Sei un DJ radiofonico esperto. Dato un brano appena ascoltato, "
                    "consiglia UN SOLO brano famoso molto simile per stile, epoca o artista. "
                    "Rispondi ESCLUSIVAMENTE nel formato 'Artista - Titolo Canzone', senza spiegazioni."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Ultimo brano ascoltato: {seed_title}\n"
                    f"Brani già ascoltati da EVITARE: {excluded_str or 'Nessuno'}\n"
                    "Prossimo brano consigliato (Artista - Titolo):"
                ),
            },
        ]
        try:
            session = await self._get_session()
            models = await self._get_groq_candidate_models(session)
            if models:
                payload = {
                    "model": models[0],
                    "messages": messages,
                    "temperature": 0.6,
                    "max_tokens": 35,
                }
                headers = {
                    "Authorization": f"Bearer {self.groq_api_key}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=3.0),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        suggestion = (
                            data["choices"][0]["message"]["content"] or ""
                        ).strip()
                        suggestion = re.sub(r"<think>.*?</think>", "", suggestion, flags=re.DOTALL).strip()
                        suggestion = suggestion.strip("\"'`.").split("\n")[0].strip()
                        if suggestion and "-" in suggestion and len(suggestion) <= 85:
                            return suggestion
        except Exception as e:
            logging.debug(f"Errore suggest_related_song AI: {e}")
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
                        logging.info(f"Modelli Groq rilevati (priorità velocità): {candidates[:3]}")
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
        force_song_mode: bool = False,
    ) -> AssistantResponse:
        it_text, en_hint = split_en_hint(user_text)

        # 1. Prova sempre prima i comandi di controllo LOCALI ISTANTANEI (< 0.2ms, zero LLM!)
        fast = self._match_fast_command(it_text)
        if fast is not None:
            return fast

        # 2. Se l'utente è nella finestra dopo "Venus, metti..." (force_song_mode)
        # oppure ha pronunciato "metti / riproduci / suona <canzone>", usa il risolutore rapido canzoni!
        raw_song_query = self._extract_Explicit_play_query(it_text)
        if not raw_song_query and force_song_mode:
            raw_song_query = it_text.strip()

        if raw_song_query:
            resolved_song = await self.resolve_song_query(raw_song_query, en_hint=en_hint)
            return AssistantResponse(
                action="play",
                query=resolved_song,
                voice_reply=f"Riproduco {resolved_song}.",
            )

        # 3. Altrimenti usa l'assistente AI completo (per domande generali o se l'utente ha detto
        # direttamente il titolo di una canzone dopo aver detto solo "Venus")
        hint_ctx = f" (Trascrizione alternativa inglese: '{en_hint}')" if en_hint else ""
        system_prompt = (
            "Sei Venus, un assistente vocale intelligente in stile Google Assistant in un canale vocale Discord.\n"
            f"Data e ora attuale in Italia: {get_italian_datetime_str()}.\n"
            f"Utente: {user_name}. Brano attuale: {current_song or 'Nessuno'}. Brani in coda: {queue_count}.\n\n"
            "REGOLE:\n"
            "- Se l'utente chiede di mettere una canzone o pronuncia il titolo di un brano/artista musicale "
            "(anche con errori di trascrizione fonetica in italiano o inglese), imposta \"action\": \"play\" e scrivi "
            "in \"query\" il vero 'Artista - Titolo Canzone'.\n"
            "- Se è una domanda generale, rispondi in italiano naturale e conciso con \"action\": \"none\".\n\n"
            "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido:\n"
            "{\n"
            '  "action": "none" | "play" | "pause" | "resume" | "skip" | "stop" | "replay" | "clear_queue" | "autoplay" | "loop" | "shuffle" | "volume_up" | "volume_down" | "volume_set" | "now_playing" | "disconnect",\n'
            '  "query": "Artista - Titolo Canzone (solo se action è play) oppure numero 1-100 (solo se action è volume_set), altrimenti stringa vuota",\n'
            '  "voice_reply": "Risposta parlata in italiano naturale e concisa (massimo 1-2 frasi brevi), senza asterischi o markdown."\n'
            "}"
        )

        history = list(self._history[guild_id])
        messages = [{"role": "system", "content": system_prompt}] + history + [
            {"role": "user", "content": f"{it_text}{hint_ctx}"}
        ]

        raw_reply = None

        if self.groq_api_key:
            raw_reply = await self._call_groq(messages)

        if not raw_reply and self.gemini_api_key:
            raw_reply = await self._call_gemini(system_prompt, history, f"{it_text}{hint_ctx}")

        if not raw_reply:
            raw_reply = await self._call_free_cloud_fallback(messages)

        parsed = self._parse_ai_response(raw_reply, None)

        self._history[guild_id].append({"role": "user", "content": it_text})
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
        session = await self._get_session()
        candidate_models = await self._get_groq_candidate_models(session)

        for model_name in list(candidate_models):
            payload = {
                "model": model_name,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 220,
                "response_format": {"type": "json_object"},
            }
            try:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]

                    if resp.status in (400, 404):
                        err_text = await resp.text()
                        if (
                            "model_not_found" in err_text
                            or "decommissioned" in err_text
                            or "not exist" in err_text
                        ):
                            if (
                                self._discovered_groq_models
                                and model_name in self._discovered_groq_models
                            ):
                                self._discovered_groq_models.remove(model_name)
                            continue

                        payload_plain = {
                            "model": model_name,
                            "messages": messages,
                            "temperature": 0.3,
                            "max_tokens": 220,
                        }
                        async with session.post(
                            url, headers=headers, json=payload_plain
                        ) as resp2:
                            if resp2.status == 200:
                                data2 = await resp2.json()
                                return data2["choices"][0]["message"]["content"]
                        continue
            except Exception as e:
                logging.debug(f"Errore chiamata Groq ({model_name}): {e}")
        return None

    async def _call_gemini(
        self, system_prompt: str, history: list, user_text: str
    ) -> Optional[str]:
        models_to_try = [self.gemini_model, "gemini-2.0-flash", "gemini-1.5-flash"]
        seen = set()

        contents = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        contents.append({"role": "user", "parts": [{"text": user_text}]})

        session = await self._get_session()
        for model_name in models_to_try:
            if model_name in seen:
                continue
            seen.add(model_name)

            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={self.gemini_api_key}"
            )
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": contents,
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 220,
                    "responseMimeType": "application/json",
                },
            }
            try:
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
            session = await self._get_session()
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
                voice_reply="Mi dispiace, non ho capito bene.",
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
