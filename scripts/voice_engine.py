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
import wave
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

import discord
from discord.opus import Decoder, OpusError
from discord.ext import voice_recv
from discord.ext.voice_recv import gateway as vr_gateway
from discord.ext.voice_recv import opus as vr_opus
from discord.ext.voice_recv import reader as vr_reader
from discord.ext.voice_recv import video as vr_video
from discord.ext.voice_recv import voice_client as vr_vc
from discord.ext.voice_recv.rtp import SilencePacket
import speech_recognition as sr

try:
    import davey
except ImportError:
    davey = None

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


# Silenzia i warning/errori interni innocui di discord-ext-voice-recv (pacchetti RTCP/comfort noise)
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.CRITICAL)
logging.getLogger("discord.ext.voice_recv.opus").setLevel(logging.ERROR)
logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.ERROR)


def patch_voice_recv_for_dave() -> None:
    """
    Applica la patch di compatibilità per il protocollo Discord DAVE (E2EE, Voice Gateway v8, close code 4017)
    a `discord-ext-voice-recv`.
    """
    if getattr(vr_opus.PacketDecoder, "_dave_patched", False):
        return

    def _safe_video_streams_init(self, *, data, vc):
        self.audio_ssrc = data.get("audio_ssrc", 0)
        self.video_ssrc = data.get("video_ssrc", 0)
        self.member = vc.guild.get_member(int(data["user_id"])) if "user_id" in data else None
        self.streams = self._get_streams(data.get("streams") or [])

    vr_video.VoiceVideoStreams.__init__ = _safe_video_streams_init

    _orig_hook = vr_gateway.hook

    async def _safe_dave_hook(ws, msg: Dict[str, Any]):
        if not isinstance(msg, dict):
            return
        op = msg.get("op")
        data = msg.get("d")
        if not isinstance(data, dict):
            return
        try:
            await _orig_hook(ws, msg)
        except Exception as e:
            logging.debug(f"Ignorato errore hook voice_recv su op={op}: {e}")

    vr_gateway.hook = _safe_dave_hook
    vr_vc.hook = _safe_dave_hook

    _orig_init = vr_opus.PacketDecoder.__init__
    _orig_reset = vr_opus.PacketDecoder.reset

    def _patched_init(self, router, ssrc: int):
        _orig_init(self, router, ssrc)
        self._dave_ok: Set[int] = set()
        self._dave_bad: Set[int] = set()
        self._dave_drops: int = 0
        self._opus_errors: int = 0

    def _patched_reset(self) -> None:
        _orig_reset(self)
        if hasattr(self, "_dave_ok"):
            self._dave_ok.clear()
        if hasattr(self, "_dave_bad"):
            self._dave_bad.clear()

    def _dave_decrypt_inner(self, packet: Any) -> bool:
        if packet.is_silence():
            return True

        vc = self.sink.voice_client
        state = getattr(vc, "_connection", None)
        if not getattr(state, "dave_protocol_version", 0):
            return True

        session = getattr(state, "dave_session", None)
        if session is None or not session.ready:
            return False

        user_id = self._cached_id
        if user_id is None:
            user_id = self._cached_id = vc._get_id_from_ssrc(self.ssrc)
        if user_id is None:
            return False

        try:
            packet.decrypted_data = session.decrypt(
                int(user_id), davey.MediaType.audio, bytes(packet.decrypted_data)
            )
            return True
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if "UnencryptedWhenPassthroughDisabled" in (str(e) or repr(e)):
                return True
            self._dave_drops = getattr(self, "_dave_drops", 0) + 1
            return False

    def _dave_decrypt(self, packet: Any, *, peek: bool = False) -> bool:
        if davey is None or not packet or not packet.decrypted_data:
            return True

        seq = packet.sequence
        if not peek:
            if seq in self._dave_ok:
                self._dave_ok.discard(seq)
                return True
            if seq in self._dave_bad:
                self._dave_bad.discard(seq)
                return False

        ok = self._dave_decrypt_inner(packet)
        if peek:
            if len(self._dave_ok) + len(self._dave_bad) > 64:
                self._dave_ok.clear()
                self._dave_bad.clear()
            (self._dave_ok if ok else self._dave_bad).add(seq)
        return ok

    def _decode_safe(self, data: Optional[bytes], *, fec: bool) -> bytes:
        assert self._decoder is not None
        try:
            return self._decoder.decode(data, fec=fec)
        except OpusError:
            self._opus_errors = getattr(self, "_opus_errors", 0) + 1
        try:
            return self._decoder.decode(None, fec=False)
        except OpusError:
            return b"\x00" * Decoder.FRAME_SIZE

    def _patched_decode_packet(self, packet: Any) -> Tuple[Any, bytes]:
        assert self._decoder is not None
        if packet:
            pcm = self._decode_safe(packet.decrypted_data, fec=False)
            return packet, pcm

        next_packet = self._buffer.peek_next()
        if next_packet is not None and self._dave_decrypt(next_packet, peek=True):
            nextdata: bytes = next_packet.decrypted_data  # type: ignore
            pcm = self._decode_safe(nextdata, fec=True)
        else:
            pcm = self._decode_safe(None, fec=False)
        return packet, pcm

    def _patched_process_packet(self, packet: Any) -> Optional[vr_opus.VoiceData]:
        pcm = None
        member = self._get_cached_member()
        if member is None:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
            member = self._get_cached_member()

        self._last_seq = packet.sequence
        self._last_ts = packet.timestamp

        if not self._dave_decrypt(packet):
            return None

        if not self.sink.wants_opus():
            packet, pcm = self._decode_packet(packet)

        return vr_opus.VoiceData(packet, member, pcm=pcm)

    vr_opus.PacketDecoder.__init__ = _patched_init
    vr_opus.PacketDecoder.reset = _patched_reset
    vr_opus.PacketDecoder._dave_decrypt_inner = _dave_decrypt_inner
    vr_opus.PacketDecoder._dave_decrypt = _dave_decrypt
    vr_opus.PacketDecoder._decode_safe = _decode_safe
    vr_opus.PacketDecoder._decode_packet = _patched_decode_packet
    vr_opus.PacketDecoder._process_packet = _patched_process_packet
    vr_opus.PacketDecoder._dave_patched = True
    logging.info("Supporto protocollo vocale Discord DAVE (E2EE v8) attivato.")


patch_voice_recv_for_dave()


ITALIAN_TTS_VOICES: Dict[str, str] = {
    "diego": "it-IT-DiegoNeural",
    "elsa": "it-IT-ElsaNeural",
    "isabella": "it-IT-IsabellaNeural",
    "giuseppe": "it-IT-GiuseppeNeural",
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
                try:
                    data = self._override_source.read()
                except Exception:
                    data = b""
                if data:
                    return data
                self._finish_override_locked()

            try:
                return self.main_source.read()
            except Exception:
                return b""

    def cleanup(self) -> None:
        with self._lock:
            self._finish_override_locked()
            try:
                self.main_source.cleanup()
            except Exception:
                pass


class ItalianTTSEngine:
    """
    Motore Text-to-Speech (TTS) cloud gratuito in italiano.
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
        if not text:
            return ""
        cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        cleaned = re.sub(r"[*_~`#>]+", "", cleaned)
        cleaned = cleaned.replace("%", " percento ")
        cleaned = cleaned.replace("&", " e ")
        cleaned = re.sub(
            r"[^\w\s.,!?;:'\"()\-àèéìòùÀÈÉÌÒÙ€$°]",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) > 650:
            cleaned = cleaned[:647].rsplit(" ", 1)[0] + "."
        return cleaned

    async def synthesize_to_file(self, text: str) -> Optional[Path]:
        clean_text = self.clean_text_for_speech(text)
        if not clean_text:
            return None

        output_path = self.temp_dir / f"tts_{uuid.uuid4().hex[:10]}.mp3"

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
    """Mantenuta per compatibilità con test unitari stereo->mono."""

    little_endian = True
    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    CHANNELS = 2
    CHUNK = 960

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
        bytes_needed = size * self.CHANNELS * self.SAMPLE_WIDTH
        available = min(bytes_needed, len(self.buffer))
        aligned = available - (available % 4)
        if aligned <= 0:
            return b""
        audiochunk = self.buffer[:aligned].tobytes()
        del self.buffer[:aligned]
        return audioop.tomono(audiochunk, self.SAMPLE_WIDTH, 0.5, 0.5)

    def close(self) -> None:
        del self.buffer[:]


class ItalianSpeechSink(voice_recv.AudioSink):
    """
    Sink vocale Discord con raccoglitore di frasi (Utterance Collector) per-utente:
    - Accumula i pacchetti PCM di ciascun utente finché l'utente non fa una vera pausa
      di fine frase (1.15 secondi di silenzio), evitando di spezzare "Venus... metti [canzone]"
      in due tronconi separati!
    - Usa Groq Whisper Large v3 Turbo con prompt bilingue (Italiano + titoli canzoni Inglesi)
      e Google Speech Recognition (it-IT + supporto en-US per i titoli) in fallback.
    """

    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    SILENCE_FLUSH_SECONDS = 1.15   # Attende 1.15s di silenzio prima di considerare conclusa la frase
    MAX_PHRASE_SECONDS = 12.0      # Durata massima singola frase

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

        self._lock = threading.Lock()
        self._user_buffers: Dict[int, Dict[str, Any]] = {}
        self._recognizer = sr.Recognizer()
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="stt-worker")
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="italian-speech-flusher"
        )
        self._flush_thread.start()

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[discord.Member], data: voice_recv.VoiceData) -> None:
        if not self._running:
            return
        if isinstance(data.packet, SilencePacket):
            return
        if user is None or getattr(user, "bot", False):
            return
        if not data.pcm or len(data.pcm) < 4:
            return

        # Allinea a 4 byte (stereo 16-bit) e converti in mono senza clipping (0.5, 0.5)
        aligned_len = len(data.pcm) - (len(data.pcm) % 4)
        if aligned_len <= 0:
            return
        mono_pcm = audioop.tomono(data.pcm[:aligned_len], self.SAMPLE_WIDTH, 0.5, 0.5)

        # Ignora puro silenzio digitale iniziale
        rms = audioop.rms(mono_pcm, self.SAMPLE_WIDTH)
        now = time.monotonic()

        with self._lock:
            entry = self._user_buffers.get(user.id)
            if entry is None:
                if rms < 100:
                    return
                entry = {
                    "user": user,
                    "pcm": bytearray(),
                    "first_ts": now,
                    "last_ts": now,
                }
                self._user_buffers[user.id] = entry

            entry["user"] = user
            entry["pcm"].extend(mono_pcm)
            if rms >= 90:
                entry["last_ts"] = now

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(0.10)
            now = time.monotonic()
            to_process = []

            with self._lock:
                for uid, entry in list(self._user_buffers.items()):
                    silence_elapsed = now - entry["last_ts"]
                    total_elapsed = now - entry["first_ts"]
                    if (
                        silence_elapsed >= self.SILENCE_FLUSH_SECONDS
                        or total_elapsed >= self.MAX_PHRASE_SECONDS
                    ):
                        self._user_buffers.pop(uid, None)
                        to_process.append((entry["user"], bytes(entry["pcm"])))

            for user, pcm_bytes in to_process:
                self._executor.submit(self._process_utterance, user, pcm_bytes)

    def _process_utterance(self, user: discord.Member, pcm_bytes: bytes) -> None:
        try:
            duration_sec = len(pcm_bytes) / (self.SAMPLE_RATE * self.SAMPLE_WIDTH)
            if duration_sec < 0.35:
                return
            if audioop.rms(pcm_bytes, self.SAMPLE_WIDTH) < 110:
                return

            audio = sr.AudioData(pcm_bytes, self.SAMPLE_RATE, self.SAMPLE_WIDTH)
            text = self._recognize_italian(self._recognizer, audio)
            if text and text.strip():
                self.on_transcription(user, text.strip())
        except Exception as e:
            logging.debug(f"Errore elaborazione frase vocale: {e}")

    def _recognize_italian(self, recognizer: sr.Recognizer, audio: sr.AudioData) -> Optional[str]:
        # 1. Prova Groq Whisper Cloud con prompt bilingue (italiano + titoli musicali in inglese)
        if self.groq_api_key:
            whisper_text = self._recognize_with_groq_whisper(audio)
            if whisper_text:
                return whisper_text

        # 2. Fallback Google Speech Recognition (it-IT)
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
        import requests

        if getattr(self, "_groq_whisper_disabled", False):
            return None

        whisper_models = getattr(
            self,
            "_groq_whisper_models",
            ["whisper-large-v3-turbo", "whisper-large-v3"],
        )

        # Prompt che guida Whisper a riconoscere comandi italiani + titoli di canzoni in inglese e italiano
        bilingual_prompt = (
            "Venus, metti, riproduci, suona, pausa, riprendi, salta, stop, volume. "
            "Richieste vocali in italiano e titoli di canzoni o artisti in inglese e italiano "
            "(es. Blinding Lights, The Weeknd, Bohemian Rhapsody, Queen, Eminem, Travis Scott, Drake, Linkin Park, Geolier, Vasco Rossi)."
        )

        try:
            wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
            for w_model in list(whisper_models):
                resp = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.groq_api_key}"},
                    files={"file": ("speech.wav", io.BytesIO(wav_bytes), "audio/wav")},
                    data={
                        "model": w_model,
                        "language": "it",
                        "prompt": bilingual_prompt,
                        "response_format": "json",
                        "temperature": "0.0",
                    },
                    timeout=6,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    text = (payload.get("text") or "").strip()
                    hallucinations = {
                        "sottotitoli creati dalla comunità amara.org",
                        "sottotitoli e revisione a cura di",
                        "grazie per aver guardato",
                        "venus, metti, riproduci, suona, pausa, riprendi, salta, stop, volume.",
                        "...",
                    }
                    if not text or text.lower() in hallucinations:
                        return None
                    return text
                elif resp.status_code in (403, 404):
                    if w_model in whisper_models:
                        whisper_models.remove(w_model)
                    self._groq_whisper_models = whisper_models
                    if not whisper_models:
                        self._groq_whisper_disabled = True
                    continue
                else:
                    break
        except Exception as e:
            logging.debug(f"Groq Whisper STT fallback a Google STT: {e}")
        return None

    @voice_recv.AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: Optional[int]) -> None:
        if member is not None:
            with self._lock:
                self._user_buffers.pop(member.id, None)

    def cleanup(self) -> None:
        self._running = False
        with self._lock:
            self._user_buffers.clear()
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
