import array
import asyncio
import audioop
import io
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import aiohttp
import discord
from discord.ext import voice_recv
from discord.ext.voice_recv.rtp import SilencePacket
import speech_recognition as sr

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

try:
    from gtts import gTTS
    HAS_GTTS = True
except ImportError:
    HAS_GTTS = False


# Voci neurali italiane disponibili su Edge TTS (gratuite, cloud-based)
ITALIAN_TTS_VOICES: Dict[str, str] = {
    "diego": "it-IT-DiegoNeural",        # Voce maschile moderna (default)
    "elsa": "it-IT-ElsaNeural",          # Voce femminile stile assistente
    "isabella": "it-IT-IsabellaNeural",  # Voce femminile calda/espressiva
    "giuseppe": "it-IT-GiuseppeNeural",  # Voce maschile naturale
}


class InterruptibleOpusSource(discord.AudioSource):
    """
    Sorgente audio Opus per Discord che permette all'assistente vocale di parlare (TTS)
    interrompendo/mettendo in pausa temporaneamente la musica in riproduzione,
    per poi riprendere la canzone dal punto esatto non appena la risposta vocale termina.
    """

    def __init__(self, main_source: discord.AudioSource):
        super().__init__()
        self.main_source = main_source
        self._override_source: Optional[discord.AudioSource] = None
        self._override_start_time: Optional[float] = None
        self._on_override_end: Optional[Callable[[float], None]] = None
        self._lock = threading.Lock()

    def is_opus(self) -> bool:
        return True

    def interrupt_with(
        self,
        override_source: discord.AudioSource,
        on_end: Optional[Callable[[float], None]] = None,
    ) -> None:
        """Sostituisce temporaneamente il flusso musicale con la risposta vocale TTS."""
        with self._lock:
            if self._override_source is not None:
                try:
                    self._override_source.cleanup()
                except Exception:
                    pass
            self._override_source = override_source
            self._override_start_time = time.time()
            self._on_override_end = on_end

    def cancel_interrupt(self) -> None:
        """Interrompe eventuale TTS in corso e torna subito alla musica."""
        with self._lock:
            self._finish_override_locked()

    def _finish_override_locked(self) -> None:
        if self._override_source is not None:
            try:
                self._override_source.cleanup()
            except Exception:
                pass
            self._override_source = None

        elapsed = 0.0
        if self._override_start_time is not None:
            elapsed = max(0.0, time.time() - self._override_start_time)
            self._override_start_time = None

        cb = self._on_override_end
        self._on_override_end = None
        if cb:
            try:
                cb(elapsed)
            except Exception as e:
                logging.debug(f"Errore callback fine TTS: {e}")

    def read(self) -> bytes:
        with self._lock:
            if self._override_source is not None:
                data = self._override_source.read()
                if data:
                    return data
                # Il TTS è terminato: ripristina il flusso musicale principale
                self._finish_override_locked()

            return self.main_source.read()

    def cleanup(self) -> None:
        with self._lock:
            if self._override_source is not None:
                try:
                    self._override_source.cleanup()
                except Exception:
                    pass
                self._override_source = None
            try:
                self.main_source.cleanup()
            except Exception:
                pass


class ItalianTTSEngine:
    """
    Motore Text-to-Speech (TTS) cloud gratuito in italiano.
    Usa Microsoft Edge Neural TTS (`edge-tts`) per una voce naturale da vero assistente vocale,
    con fallback automatico a Google TTS (`gTTS`).
    """

    def __init__(self, default_voice: Optional[str] = None):
        env_voice = (os.getenv("TTS_VOICE") or "").strip().lower()
        if default_voice and default_voice.lower() in ITALIAN_TTS_VOICES:
            self.voice = ITALIAN_TTS_VOICES[default_voice.lower()]
        elif env_voice in ITALIAN_TTS_VOICES:
            self.voice = ITALIAN_TTS_VOICES[env_voice]
        elif env_voice.startswith("it-it-"):
            self.voice = os.getenv("TTS_VOICE").strip()
        else:
            self.voice = ITALIAN_TTS_VOICES["elsa"]

        self.temp_dir = Path(tempfile.gettempdir()) / "venusbot_tts"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def set_voice(self, voice_key: str) -> str:
        key = voice_key.strip().lower()
        if key in ITALIAN_TTS_VOICES:
            self.voice = ITALIAN_TTS_VOICES[key]
        else:
            self.voice = voice_key.strip()
        return self.voice

    @staticmethod
    def clean_text_for_speech(text: str) -> str:
        """Pulisce il testo da markdown, URL ed emoji per una pronuncia fluida in italiano."""
        if not text:
            return ""
        # Rimuovi blocchi di codice e URL
        cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        # Rimuovi formattazione markdown (*, _, ~, `, #)
        cleaned = re.sub(r"[*_~`#>]+", "", cleaned)
        # Sostituisci simboli comuni per la lettura vocale
        cleaned = cleaned.replace("%", " percento ")
        cleaned = cleaned.replace("&", " e ")
        # Rimuovi emoji e caratteri non pronunciabili lasciando punteggiatura e lettere accentate
        cleaned = re.sub(
            r"[^\w\s.,!?;:'\"()\-àèéìòùÀÈÉÌÒÙ€$°]",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # Limita la lunghezza massima per evitare monologhi eccessivi in canale vocale
        if len(cleaned) > 650:
            cleaned = cleaned[:647].rsplit(" ", 1)[0] + "."
        return cleaned

    async def synthesize_to_file(self, text: str) -> Optional[Path]:
        """Genera un file audio MP3 con la risposta vocale in italiano."""
        clean_text = self.clean_text_for_speech(text)
        if not clean_text:
            return None

        output_path = self.temp_dir / f"tts_{uuid.uuid4().hex[:10]}.mp3"

        # 1. Prova Edge Neural TTS (qualità neurale superiore, cloud gratuito)
        if HAS_EDGE_TTS:
            try:
                communicate = edge_tts.Communicate(
                    text=clean_text,
                    voice=self.voice,
                    rate="+5%",
                )
                await asyncio.wait_for(communicate.save(str(output_path)), timeout=10.0)
                if output_path.exists() and output_path.stat().st_size > 100:
                    return output_path
            except Exception as e:
                logging.warning(f"Edge-TTS fallito ({e}), passo al fallback gTTS...")

        # 2. Fallback automatico su Google TTS (gTTS) in italiano
        if HAS_GTTS:
            try:
                loop = asyncio.get_running_loop()

                def _run_gtts():
                    tts = gTTS(text=clean_text, lang="it", slow=False)
                    tts.save(str(output_path))

                await asyncio.wait_for(loop.run_in_executor(None, _run_gtts), timeout=10.0)
                if output_path.exists() and output_path.stat().st_size > 100:
                    return output_path
            except Exception as e:
                logging.error(f"Anche il fallback gTTS è fallito: {e}")

        return None


class ItalianDiscordAudioSource(sr.AudioSource):
    """
    AudioSource ottimizzata per Discord a 48kHz stereo -> mono senza clipping.
    Risolve i bug della DiscordSRAudioSource standard:
    - Allineamento rigoroso a 4 byte (frame stereo 16-bit)
    - Conversione stereo->mono con fattore (0.5, 0.5) per evitare distorsioni e saturazione
    - Timeout ridotto a fine parlato per una risposta rapida dell'assistente
    """

    little_endian = True
    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    CHANNELS = 2
    CHUNK = 960  # 20ms a 48kHz

    def __init__(self, buffer: array.array):
        self.buffer = buffer
        self._entered = False

    @property
    def stream(self):
        return self

    def __enter__(self):
        self._entered = True
        return self

    def __exit__(self, *exc) -> None:
        self._entered = False

    def read(self, size: int) -> bytes:
        # Ogni frame stereo 16-bit occupa 4 byte (2 canali * 2 byte)
        bytes_needed = size * self.CHANNELS * self.SAMPLE_WIDTH

        # Attendi fino a ~0.45s che arrivino pacchetti RTP da Discord;
        # quando l'utente smette di parlare, Discord interrompe l'invio e restituiamo b''
        for _ in range(9):
            if len(self.buffer) < bytes_needed:
                time.sleep(0.05)
            else:
                break
        else:
            if len(self.buffer) < 4:
                return b""

        available = min(bytes_needed, len(self.buffer))
        # Allinea sempre a multipli di 4 byte (1 frame stereo 16-bit)
        aligned = available - (available % 4)
        if aligned <= 0:
            return b""

        audiochunk = self.buffer[:aligned].tobytes()
        del self.buffer[:aligned]

        # 0.5 e 0.5 evitano il clipping/overflow quando entrambi i canali sono attivi
        mono_chunk = audioop.tomono(audiochunk, self.SAMPLE_WIDTH, 0.5, 0.5)
        return mono_chunk

    def close(self) -> None:
        del self.buffer[:]


class ItalianSpeechSink(voice_recv.AudioSink):
    """
    Sink vocale Discord progettato per riconoscere con alta precisione l'italiano
    (inclusi titoli di canzoni straniere pronunciati da utenti italiani).

    Supporta:
    1. Groq Cloud Whisper (`whisper-large-v3-turbo`, gratuito con GROQ_API_KEY)
    2. Google Cloud Web Speech API (`it-IT`, gratuito senza alcuna API key) come motore
       predefinito o di fallback immediato.
    """

    def __init__(
        self,
        on_transcription: Callable[[discord.Member, str], Any],
        phrase_time_limit: int = 12,
        language: str = "it-IT",
    ):
        super().__init__(None)
        self.on_transcription = on_transcription
        self.phrase_time_limit = phrase_time_limit
        self.language = language
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("'\"")
        self._stream_data: Dict[int, Dict[str, Any]] = defaultdict(self._create_user_stream)

    @staticmethod
    def _create_user_stream() -> Dict[str, Any]:
        recognizer = sr.Recognizer()
        recognizer.energy_threshold = 220
        recognizer.dynamic_energy_threshold = True
        recognizer.pause_threshold = 0.65
        recognizer.phrase_threshold = 0.2
        recognizer.non_speaking_duration = 0.3
        return {
            "stopper": None,
            "recognizer": recognizer,
            "buffer": array.array("B"),
        }

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[discord.Member], data: voice_recv.VoiceData) -> None:
        if isinstance(data.packet, SilencePacket):
            return
        if user is None or getattr(user, "bot", False):
            return

        sdata = self._stream_data[user.id]
        sdata["buffer"].extend(data.pcm)

        if not sdata["stopper"]:
            source = ItalianDiscordAudioSource(sdata["buffer"])
            sdata["stopper"] = sdata["recognizer"].listen_in_background(
                source,
                self._make_background_callback(user),
                phrase_time_limit=self.phrase_time_limit,
            )

    def _make_background_callback(self, user: discord.Member):
        def callback(recognizer: sr.Recognizer, audio: sr.AudioData):
            text = self._recognize_italian(recognizer, audio)
            if text and text.strip():
                self.on_transcription(user, text.strip())

        return callback

    def _recognize_italian(self, recognizer: sr.Recognizer, audio: sr.AudioData) -> Optional[str]:
        """
        Trascrive l'audio in italiano:
        - Ignora frammenti troppo brevi (< 0.25s) o puro rumore di fondo
        - Usa Groq Whisper Large v3 Turbo se GROQ_API_KEY è impostata
        - Altrimenti (o in fallback) usa Google Speech Recognition con language='it-IT'
        """
        try:
            raw_bytes = audio.frame_data
            duration_sec = len(raw_bytes) / (audio.sample_rate * audio.sample_width)
            if duration_sec < 0.25:
                return None
            if audioop.rms(raw_bytes, audio.sample_width) < 120:
                return None
        except Exception:
            pass

        # 1. Prova Groq Whisper Cloud (se configurato) per precisione superiore in italiano
        if self.groq_api_key:
            whisper_text = self._recognize_with_groq_whisper(audio)
            if whisper_text:
                return whisper_text

        # 2. Google Speech Recognition configurato esplicitamente per l'Italiano (it-IT)
        try:
            func = getattr(recognizer, "recognize_google", None)
            if func:
                text = func(audio, language=self.language)
                if text and isinstance(text, str):
                    return text.strip()
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            logging.warning(f"Errore richiesta Google Speech Recognition: {e}")
        except Exception as e:
            logging.debug(f"Errore trascrizione vocale: {e}")

        return None

    def _recognize_with_groq_whisper(self, audio: sr.AudioData) -> Optional[str]:
        """Usa l'API cloud gratuita di Groq (whisper-large-v3-turbo) per trascrivere l'audio in italiano."""
        import requests

        try:
            # Ricampiona a 16kHz mono per un upload istantaneo (< 30KB)
            wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.groq_api_key}"},
                files={"file": ("speech.wav", io.BytesIO(wav_bytes), "audio/wav")},
                data={
                    "model": "whisper-large-v3-turbo",
                    "language": "it",
                    "response_format": "json",
                    "temperature": "0.0",
                },
                timeout=6,
            )
            if resp.status_code == 200:
                payload = resp.json()
                text = (payload.get("text") or "").strip()
                # Filtra aloni tipici di Whisper sul silenzio
                hallucinations = {
                    "sottotitoli creati dalla comunità amara.org",
                    "sottotitoli e revisione a cura di",
                    "grazie per aver guardato",
                    "...",
                }
                if text.lower() in hallucinations:
                    return None
                return text
        except Exception as e:
            logging.debug(f"Groq Whisper STT fallback a Google STT: {e}")
        return None

    @voice_recv.AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: Optional[int]) -> None:
        if member is not None:
            self._drop(member.id)

    def cleanup(self) -> None:
        for user_id in tuple(self._stream_data.keys()):
            self._drop(user_id)

    def _drop(self, user_id: int) -> None:
        data = self._stream_data.pop(user_id, None)
        if not data:
            return
        stopper = data.get("stopper")
        if stopper:
            try:
                stopper(False)
            except Exception:
                pass
        buf = data.get("buffer")
        if buf is not None:
            del buf[:]
