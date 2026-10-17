import array
import asyncio
import audioop
import hashlib
import io
import logging
import math
import os
import re
import struct
import tempfile
import threading
import time
import uuid
import wave
from collections import defaultdict, deque
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
from dotenv import load_dotenv
import requests
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

    def replace_main_source(self, new_main_source: discord.AudioSource) -> None:
        """
        Sostituisce a caldo la sorgente musicale principale (es. per cambio volume istantaneo
        o riavvio del brano da capo) senza interrompere il player di Discord.
        """
        with self._lock:
            old_main = self.main_source
            self.main_source = new_main_source
            try:
                old_main.cleanup()
            except Exception:
                pass

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

        # Cache persistente su disco per voce + testo:
        # le frasi comuni ("Dimmi pure.", "Salto il brano.", "Musica in pausa.", ecc.)
        # vengono restituite in 0ms senza dover attendere la rete!
        cache_key = hashlib.sha1(
            f"{self.voice}:{clean_text.lower()}".encode("utf-8")
        ).hexdigest()[:16]
        cached_path = self.temp_dir / f"cached_{cache_key}.mp3"
        if cached_path.exists() and cached_path.stat().st_size > 256:
            return cached_path

        tmp_path = self.temp_dir / f"tmp_{uuid.uuid4().hex[:10]}.mp3"

        if HAS_EDGE_TTS:
            try:
                communicate = edge_tts.Communicate(
                    text=clean_text,
                    voice=self.voice,
                    rate="+8%",
                )
                await asyncio.wait_for(communicate.save(str(tmp_path)), timeout=8.0)
                if tmp_path.exists() and tmp_path.stat().st_size > 256:
                    try:
                        tmp_path.replace(cached_path)
                        return cached_path
                    except Exception:
                        return tmp_path
            except Exception as e:
                logging.warning(f"Edge-TTS fallito ({e}), passo al fallback gTTS...")

        if HAS_GTTS:
            try:
                loop = asyncio.get_running_loop()

                def _run_gtts():
                    tts = gTTS(text=clean_text, lang="it", slow=False)
                    tts.save(str(tmp_path))

                await asyncio.wait_for(loop.run_in_executor(None, _run_gtts), timeout=8.0)
                if tmp_path.exists() and tmp_path.stat().st_size > 256:
                    try:
                        tmp_path.replace(cached_path)
                        return cached_path
                    except Exception:
                        return tmp_path
            except Exception as e:
                logging.error(f"Anche il fallback gTTS è fallito: {e}")

        return None

    async def prewarm_common_phrases(self) -> None:
        """Pre-genera in background il suono di attivazione e le risposte vocali più frequenti (0ms)."""
        try:
            self.get_activation_chime_path()
        except Exception:
            pass
        common = (
            "Dimmi pure.",
            "Musica in pausa.",
            "Riprendo la musica.",
            "Riprendo la riproduzione.",
            "Salto il brano.",
            "Fermo la musica e svuoto la coda.",
            "Ho mescolato la coda di riproduzione.",
            "Rimetto la canzone dall'inizio.",
            "Ripetizione del brano attivata.",
            "Ripetizione disattivata.",
        )
        for phrase in common:
            try:
                await self.synthesize_to_file(phrase)
            except Exception:
                pass

    def get_activation_chime_path(self) -> Path:
        """
        Genera (e mantiene in cache) un suono di attivazione cristallino stile Google Assistant / Smart Speaker
        (doppio tono armonico ascendente D5 -> A5 da 0.25s a 48kHz stereo).
        """
        chime_path = self.temp_dir / "cached_wake_chime.wav"
        if chime_path.exists() and chime_path.stat().st_size > 1024:
            return chime_path

        sample_rate = 48000
        duration = 0.25
        total_samples = int(sample_rate * duration)
        split_sample = int(sample_rate * 0.105)

        frames = bytearray()
        for i in range(total_samples):
            t = i / sample_rate
            if i < split_sample:
                # Nota 1: Re5 (587.33 Hz) con armonica morbida
                local_t = i / sample_rate
                note_dur = split_sample / sample_rate
                env = math.sin(math.pi * (local_t / note_dur)) ** 0.7
                val = (
                    0.78 * math.sin(2.0 * math.pi * 587.33 * t)
                    + 0.22 * math.sin(2.0 * math.pi * 1174.66 * t)
                ) * env
            else:
                # Nota 2: La5 (880.00 Hz) brillante con decadimento morbido
                local_t = (i - split_sample) / sample_rate
                note_dur = (total_samples - split_sample) / sample_rate
                attack = min(1.0, local_t / 0.010)
                decay = math.exp(-local_t * 10.5)
                env = attack * decay
                val = (
                    0.80 * math.sin(2.0 * math.pi * 880.00 * t)
                    + 0.20 * math.sin(2.0 * math.pi * 1760.00 * t)
                ) * env

            sample_int = max(-32767, min(32767, int(val * 14500)))
            frames.extend(struct.pack("<hh", sample_int, sample_int))

        with wave.open(str(chime_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(frames))

        return chime_path


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
    Sink vocale Discord Multi-Motore ad Alta Precisione (Utterance Collector):
    - Ricostruisce le micro-pause DTX di Discord tra una parola e l'altra (evitando che
      "Venus" e "metti" vengano incollati senza spazio audio).
    - Normalizza il volume sui PICCHI reali (Peak Normalization a ~73% full-scale)
      senza MAI clippare o distorcere la voce.
    - Supporta 4 motori Speech-to-Text combinati:
      1. Deepgram Nova-3 (se DEEPGRAM_API_KEY è presente: bilingue nativo IT+EN ultra-preciso)
      2. Google Gemini 2.0 Flash Audio (se GEMINI_API_KEY è presente: comprensione audio nativa di titoli e artisti)
      3. Groq Whisper Large v3 (32-layer full model + doppio passaggio IT + EN/Multilingua per le canzoni)
      4. Google Speech Recognition Multi-Alternativa (top-5 ipotesi it-IT + ipotesi en-US in parallelo)
    """

    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    SHORT_SILENCE_FLUSH_SECONDS = 0.58  # Per attivazione rapida ("Venus")
    LONG_SILENCE_FLUSH_SECONDS = 0.92   # Per frasi con titolo canzone ("Venus metti ...")
    MAX_PHRASE_SECONDS = 10.0           # Durata massima singola frase
    _GROQ_WHISPER_MODELS = ["whisper-large-v3", "whisper-large-v3-turbo"]

    def __init__(
        self,
        on_transcription: Callable[[discord.Member, str], Any],
        phrase_time_limit: int = 12,
        language: str = "it-IT",
    ):
        self._lock = threading.Lock()
        self._user_buffers: Dict[int, Dict[str, Any]] = {}
        self._preroll_buffers: Dict[int, deque] = defaultdict(lambda: deque(maxlen=15))
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running = False
        super().__init__(None)
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.on_transcription = on_transcription
        self.phrase_time_limit = phrase_time_limit
        self.language = language
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("'\"")
        self.gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip().strip("'\"")
        self.deepgram_api_key = (os.getenv("DEEPGRAM_API_KEY") or "").strip().strip("'\"")

        self._recognizer = sr.Recognizer()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stt-worker")
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

        rms = audioop.rms(mono_pcm, self.SAMPLE_WIDTH)
        now = time.monotonic()

        with self._lock:
            entry = self._user_buffers.get(user.id)
            if entry is None:
                # Conserva sempre gli ultimi ~300ms di pre-roll per non tagliare mai l'inizio della prima parola
                self._preroll_buffers[user.id].append(mono_pcm)
                if rms < 85:
                    return
                preroll_bytes = b"".join(self._preroll_buffers[user.id])
                self._preroll_buffers[user.id].clear()
                entry = {
                    "user": user,
                    "pcm": bytearray(preroll_bytes),
                    "first_ts": now,
                    "last_ts": now,
                    "last_pkt_ts": now,
                }
                self._user_buffers[user.id] = entry
            else:
                entry["user"] = user
                # Se Discord ha sospeso l'invio di pacchetti RTP durante una breve pausa tra due parole
                # (es. "Venus ... metti"), reinserisci i millisecondi di silenzio mancanti così le parole
                # non vengono incollate tra loro!
                pkt_gap = now - entry.get("last_pkt_ts", now)
                if 0.06 <= pkt_gap <= 0.65:
                    gap_samples = int(min(pkt_gap, 0.35) * self.SAMPLE_RATE)
                    entry["pcm"].extend(b"\x00" * (gap_samples * self.SAMPLE_WIDTH))
                entry["last_pkt_ts"] = now
                entry["pcm"].extend(mono_pcm)
                if rms >= 80:
                    entry["last_ts"] = now

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(0.04)
            now = time.monotonic()
            to_process = []

            with self._lock:
                for uid, entry in list(self._user_buffers.items()):
                    silence_elapsed = now - entry["last_ts"]
                    total_elapsed = now - entry["first_ts"]
                    threshold = (
                        self.SHORT_SILENCE_FLUSH_SECONDS
                        if total_elapsed < 0.85
                        else self.LONG_SILENCE_FLUSH_SECONDS
                    )
                    if (
                        silence_elapsed >= threshold
                        or total_elapsed >= self.MAX_PHRASE_SECONDS
                    ):
                        self._user_buffers.pop(uid, None)
                        to_process.append((entry["user"], bytes(entry["pcm"])))

            for user, pcm_bytes in to_process:
                self._executor.submit(self._process_utterance, user, pcm_bytes)

    def _process_utterance(self, user: discord.Member, pcm_bytes: bytes) -> None:
        try:
            duration_sec = len(pcm_bytes) / (self.SAMPLE_RATE * self.SAMPLE_WIDTH)
            if duration_sec < 0.28:
                return
            rms = audioop.rms(pcm_bytes, self.SAMPLE_WIDTH)
            peak = audioop.max(pcm_bytes, self.SAMPLE_WIDTH)
            if rms < 75 or peak < 350:
                return

            # Normalizzazione sul PICCO REALE (Peak Normalization a 23000 / 32767 = ~70% full-scale):
            # amplifica i microfoni bassi ma impedisce MATEMATICAMENTE qualsiasi clipping o distorsione!
            if 400 <= peak < 19000:
                gain = min(2.4, 23000.0 / float(peak))
                try:
                    pcm_bytes = audioop.mul(pcm_bytes, self.SAMPLE_WIDTH, gain)
                except Exception:
                    pass

            # Aggiungi 200ms di silenzio pulito all'inizio e alla fine
            pad = b"\x00" * int(self.SAMPLE_RATE * self.SAMPLE_WIDTH * 0.20)
            framed_pcm = pad + pcm_bytes + pad

            audio = sr.AudioData(framed_pcm, self.SAMPLE_RATE, self.SAMPLE_WIDTH)
            text = self._recognize_italian(self._recognizer, audio)
            if text and text.strip():
                self.on_transcription(user, text.strip())
        except Exception as e:
            logging.debug(f"Errore elaborazione frase vocale: {e}")

    @staticmethod
    def _looks_like_music_request(text: str) -> bool:
        t = text.lower()
        return any(
            w in t
            for w in (
                "metti",
                "mettere",
                "riproduci",
                "riprodurre",
                "suona",
                "suonare",
                "play",
                "cerca",
                "ascoltiamo",
                "fammi ascoltare",
                "voglio ascoltare",
                "canzone",
                "brano",
            )
        )

    def _recognize_italian(self, recognizer: sr.Recognizer, audio: sr.AudioData) -> Optional[str]:
        wav_bytes: Optional[bytes] = None
        try:
            wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
        except Exception:
            wav_bytes = None

        # 1. Deepgram Nova-3 (se DEEPGRAM_API_KEY è impostata: modello bilingue nativo IT+EN)
        if self.deepgram_api_key and wav_bytes:
            dg_text = self._recognize_with_deepgram(wav_bytes)
            if dg_text:
                logging.info(f"[STT: Deepgram Nova-3] '{dg_text}'")
                return dg_text

        # 2. Google Gemini 2.0 Flash Multimodal Audio (se GEMINI_API_KEY è impostata: ascolto nativo)
        if self.gemini_api_key and wav_bytes:
            gem_text = self._recognize_with_gemini_audio(wav_bytes)
            if gem_text:
                logging.info(f"[STT: Gemini Audio] '{gem_text}'")
                return gem_text

        # 3. Groq Whisper Large v3 (modello completo a 32 layer) + trascrizione parallela EN/Multilingua (0ms extra!)
        if self.groq_api_key and wav_bytes:
            en_future = self._executor.submit(self._recognize_google_en, recognizer, audio)
            whisper_it = self._recognize_with_groq_whisper(wav_bytes, language="it")
            if whisper_it:
                if self._looks_like_music_request(whisper_it):
                    en_hint = None
                    try:
                        en_hint = en_future.result(timeout=0.9)
                    except Exception:
                        pass
                    if not en_hint:
                        en_hint = self._recognize_with_groq_whisper(wav_bytes, language=None)
                    if en_hint and en_hint.lower().strip() != whisper_it.lower().strip():
                        logging.info(
                            f"[STT: Groq Whisper-v3 Dual] IT='{whisper_it}' | EN='{en_hint}'"
                        )
                        return f"{whisper_it} [EN_HINT: {en_hint}]"
                logging.info(f"[STT: Groq Whisper-v3] '{whisper_it}'")
                return whisper_it

        # 4. Google Speech Recognition Multi-Alternativa (it-IT top-5 + en-US)
        try:
            func = getattr(recognizer, "recognize_google", None)
            if func:
                raw_it = func(audio, language=self.language, show_all=True)
                best_it = None
                alt_it: list[str] = []
                if isinstance(raw_it, dict) and "alternative" in raw_it:
                    for item in raw_it["alternative"]:
                        tr = (item.get("transcript") or "").strip()
                        if tr and tr not in alt_it:
                            alt_it.append(tr)
                    if alt_it:
                        best_it = alt_it[0]
                elif isinstance(raw_it, str) and raw_it.strip():
                    best_it = raw_it.strip()
                    alt_it = [best_it]

                if best_it:
                    # Se nella seconda/terza ipotesi c'è "venus" o "metti" che mancava nella prima, promuovila!
                    for cand in alt_it[1:]:
                        cl = cand.lower()
                        if ("venus" in cl or "venere" in cl) and (
                            "venus" not in best_it.lower() and "venere" not in best_it.lower()
                        ):
                            best_it = cand
                            break

                    extra_hints = []
                    if len(alt_it) > 1:
                        others = [a for a in alt_it if a.lower() != best_it.lower()][:3]
                        if others:
                            extra_hints.extend(others)

                    if self._looks_like_music_request(best_it) or any(
                        self._looks_like_music_request(a) for a in alt_it
                    ):
                        en_clean = self._recognize_google_en(recognizer, audio)
                        if en_clean and en_clean.lower() != best_it.lower():
                            extra_hints.insert(0, en_clean)

                    if extra_hints:
                        hint_str = " | ".join(extra_hints)
                        logging.info(
                            f"[STT: Google Multi-Hyp] IT='{best_it}' | HINTS='{hint_str}'"
                        )
                        return f"{best_it} [EN_HINT: {hint_str}]"

                    logging.info(f"[STT: Google] '{best_it}'")
                    return best_it
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            logging.warning(f"Errore richiesta Google Speech Recognition: {e}")
        except Exception as e:
            logging.debug(f"Errore trascrizione vocale: {e}")

        return None

    @staticmethod
    def _recognize_google_en(recognizer: sr.Recognizer, audio: sr.AudioData) -> Optional[str]:
        try:
            func = getattr(recognizer, "recognize_google", None)
            if not func:
                return None
            raw_en = func(audio, language="en-US", show_all=True)
            if isinstance(raw_en, dict) and "alternative" in raw_en:
                alts = [
                    (item.get("transcript") or "").strip()
                    for item in raw_en["alternative"]
                    if (item.get("transcript") or "").strip()
                ]
                return alts[0] if alts else None
            if isinstance(raw_en, str) and raw_en.strip():
                return raw_en.strip()
        except Exception:
            pass
        return None

    def _recognize_with_deepgram(self, wav_bytes: bytes) -> Optional[str]:
        try:
            resp = requests.post(
                "https://api.deepgram.com/v1/listen?model=nova-3&language=multi&smart_format=true",
                headers={
                    "Authorization": f"Token {self.deepgram_api_key}",
                    "Content-Type": "audio/wav",
                },
                data=wav_bytes,
                timeout=3.5,
            )
            if resp.status_code == 200:
                data = resp.json()
                channels = data.get("results", {}).get("channels", [])
                if channels:
                    alts = channels[0].get("alternatives", [])
                    if alts:
                        transcript = (alts[0].get("transcript") or "").strip()
                        if transcript:
                            return transcript
        except Exception as e:
            logging.debug(f"Deepgram STT fallback: {e}")
        return None

    def _recognize_with_gemini_audio(self, wav_bytes: bytes) -> Optional[str]:
        import base64

        b64_audio = base64.b64encode(wav_bytes).decode("ascii")
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={self.gemini_api_key}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "Trascrivi fedelmente questo comando vocale in italiano. "
                                "Se l'utente pronuncia il titolo di una canzone o di un artista in inglese o italiano "
                                "(es. dopo 'Venus metti...'), scrivi il titolo e l'artista con lo spelling corretto. "
                                "Restituisci SOLO le parole pronunciate dall'utente, senza commenti né punteggiatura finale."
                            )
                        },
                        {
                            "inlineData": {
                                "mimeType": "audio/wav",
                                "data": b64_audio,
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 60,
            },
        }
        try:
            resp = requests.post(url, json=payload, timeout=3.8)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = (parts[0].get("text") or "").strip().strip("\"'`.")
                        if text and len(text) <= 160:
                            return text
        except Exception as e:
            logging.debug(f"Gemini Audio STT fallback: {e}")
        return None

    def _recognize_with_groq_whisper(
        self, wav_bytes: bytes, language: Optional[str] = "it"
    ) -> Optional[str]:
        # Prompt minimale non inquinante: evita che Whisper allucini titoli di canzoni non pronunciati!
        clean_prompt = (
            "Venus, metti, riproduci, pausa, riprendi, salta, stop, volume."
            if language == "it"
            else "Venus, metti"
        )

        for w_model in self._GROQ_WHISPER_MODELS:
            try:
                form_data = {
                    "model": w_model,
                    "prompt": clean_prompt,
                    "response_format": "json",
                    "temperature": "0.0",
                }
                if language:
                    form_data["language"] = language

                resp = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.groq_api_key}"},
                    files={"file": ("speech.wav", io.BytesIO(wav_bytes), "audio/wav")},
                    data=form_data,
                    timeout=3.8,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    text = (payload.get("text") or "").strip()
                    hallucinations = {
                        "sottotitoli creati dalla comunità amara.org",
                        "sottotitoli e revisione a cura di",
                        "grazie per aver guardato",
                        "thank you for watching",
                        "venus, metti, riproduci, pausa, riprendi, salta, stop, volume.",
                        "venus, metti",
                        "venus.",
                        "...",
                    }
                    if not text or text.lower().strip() in hallucinations:
                        return None
                    return text
                elif resp.status_code in (401, 403, 404):
                    logging.debug(f"Groq Whisper ({w_model}) HTTP {resp.status_code}: {resp.text[:120]}")
                    continue
                else:
                    break
            except Exception as e:
                logging.debug(f"Groq Whisper STT ({w_model}) errore: {e}")
                break
        return None

    @voice_recv.AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: Optional[int]) -> None:
        if member is not None:
            with self._lock:
                self._user_buffers.pop(member.id, None)

    def cleanup(self) -> None:
        self._running = False
        if hasattr(self, "_lock") and hasattr(self, "_user_buffers"):
            with self._lock:
                self._user_buffers.clear()
        try:
            if getattr(self, "_executor", None) is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
