import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional, Tuple

import discord
from discord.ext import voice_recv

from embed_views.music_buttons import MusicButtons
from management.banned_users import BannedUsers
from management.bot_keywords import BotKeywords
from management.word_counter import WordCounter
from scripts.ai_assistant import AIAssistant
from scripts.spotify import SpotifyController
from scripts.voice_engine import (
    ITALIAN_TTS_VOICES,
    InterruptibleOpusSource,
    ItalianSpeechSink,
    ItalianTTSEngine,
)
from scripts.ytDLP import VideoSearcher, getSongExpiration


class Song:
    def __init__(
        self,
        title: str,
        url: str,
        link: str,
        thumbnail: Optional[str],
        duration: int,
        user: discord.User,
        isFile: bool,
        http_headers: Optional[dict] = None,
        is_local_cache: bool = False,
    ):
        self.title = title
        self.url = url
        self.link = link
        self.thumbnail = thumbnail
        self.duration = duration
        self.user = user
        self.isFile = isFile
        self.http_headers = http_headers or {}
        self.is_local_cache = is_local_cache

    def cleanup_cache_file(self) -> None:
        if self.is_local_cache and self.link:
            try:
                p = Path(self.link)
                if p.exists():
                    p.unlink()
            except Exception:
                pass


class MusicController:
    def __init__(self, client: discord.Client, guild: discord.Guild):
        logging.info(f"Creato MusicController per il server: {guild.name} ({guild.id})")
        self.client = client
        self.guild = guild
        self.spotify = SpotifyController()
        self.ai_assistant = AIAssistant()
        self.tts_engine = ItalianTTSEngine()

        self.voiceChannel: Optional[discord.VoiceChannel] = None
        self.textChannel: Optional[discord.TextChannel] = None
        self.transcriptionChannel: Optional[discord.TextChannel] = None

        self.songQueue: list[Song] = []
        self.isLooping: bool = False
        self.isMajorityVote: bool = False
        self.volume: float = 0.70

        self.start_time: Optional[int] = None
        self.pause_start: Optional[int] = None
        self.pause_duration: int = 0

        self.current_audio_source: Optional[InterruptibleOpusSource] = None
        self._tts_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._active_listener_until: dict[int, float] = {}
        self._watchdog_task: Optional[asyncio.Task] = None

    def _bot_avatar_url(self) -> Optional[str]:
        if self.client.user:
            return self.client.user.display_avatar.url
        return None

    def _clear_queue_and_cache(self) -> None:
        for s in self.songQueue:
            s.cleanup_cache_file()
        self.songQueue.clear()

    def _get_voice_client(self) -> Optional[voice_recv.VoiceRecvClient]:
        return self.guild.voice_client or discord.utils.get(
            self.client.voice_clients, guild=self.guild
        )

    def isConnectedToVC(self) -> bool:
        """
        Verifica se il bot è REALMENTE connesso a un canale vocale su Discord.
        Controlla sia il VoiceClient locale sia lo stato vocale effettivo del membro bot nel server
        per evitare falsi positivi (zombie voice_client) dopo disconnessioni o crash di rete.
        """
        voice_client = self._get_voice_client()
        if not voice_client or not voice_client.is_connected() or not voice_client.channel:
            return False

        bot_member = self.guild.me
        if bot_member is not None:
            if not bot_member.voice or not bot_member.voice.channel:
                return False

        return True

    async def _cleanup_stale_voice_client(self) -> None:
        """
        Pulisce forzatamente qualsiasi VoiceClient zombie rimasto in memoria
        quando il bot non è più realmente nel canale vocale.
        """
        if self.current_audio_source is not None:
            try:
                self.current_audio_source.cancel_interrupt()
            except Exception:
                pass
            self.current_audio_source = None

        voice_client = self._get_voice_client()
        if voice_client is not None:
            try:
                if hasattr(voice_client, "stop_listening") and voice_client.is_listening():
                    voice_client.stop_listening()
            except Exception:
                pass
            try:
                if hasattr(voice_client, "stop_playing"):
                    voice_client.stop_playing()
            except Exception:
                pass
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                pass
            try:
                voice_client.cleanup()
            except Exception:
                pass

    async def handleBotDisconnectedFromVC(self) -> None:
        """
        Chiamato da on_voice_state_update quando il bot stesso viene disconnesso o espulso dal canale vocale.
        """
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None
        self._clear_queue_and_cache()
        self.isLooping = False
        await self._cleanup_stale_voice_client()

    def _ensure_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.client.loop.create_task(self._voice_watchdog_loop())

    async def _voice_watchdog_loop(self) -> None:
        """
        Controlla periodicamente che l'ascolto vocale (ItalianSpeechSink) sia sempre attivo
        mentre il bot è nel canale vocale, riavviandolo automaticamente se si interrompe.
        """
        try:
            while True:
                await asyncio.sleep(4.0)
                if not self.isConnectedToVC():
                    await self._cleanup_stale_voice_client()
                    break

                voice_client = self._get_voice_client()
                if (
                    voice_client
                    and hasattr(voice_client, "is_listening")
                    and not voice_client.is_listening()
                ):
                    logging.info(
                        f"[Watchdog Vocale] Riavvio automatico ascolto vocale su {self.guild.name}..."
                    )
                    await self.startVoiceRecording()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.debug(f"Watchdog vocale terminato: {e}")

    def getVideoAndTextChannel(self) -> Tuple[Optional[discord.VoiceChannel], Optional[discord.TextChannel]]:
        return self.voiceChannel, self.textChannel

    def getSongQueue(self) -> list[Song]:
        return self.songQueue

    def getIsMajorityVote(self) -> bool:
        return self.isMajorityVote

    def setVolume(self, volume_percent: int) -> int:
        clamped = max(5, min(150, int(volume_percent)))
        self.volume = clamped / 100.0
        return clamped

    def setTTSVoice(self, voice_name: str) -> str:
        return self.tts_engine.set_voice(voice_name)

    async def getCurrentDuration(self) -> str:
        def format_duration(seconds: int) -> str:
            seconds = max(0, int(seconds))
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            if hours > 0:
                return f"{hours:02}:{minutes:02}:{secs:02}"
            return f"{minutes:02}:{secs:02}"

        if self.songQueue and self.start_time is not None:
            song_duration = self.songQueue[0].duration
            voice_client = self._get_voice_client()
            if voice_client and voice_client.is_paused() and self.pause_start is not None:
                elapsed = int(self.pause_start - self.start_time - self.pause_duration)
            else:
                elapsed = int(time.time() - self.start_time - self.pause_duration)
            elapsed = max(0, elapsed)
            if song_duration == 0:
                return f"*{format_duration(elapsed)}* in riproduzione (stream live)."
            return f"*{format_duration(elapsed)}* **/** *{format_duration(song_duration)}*"
        return "Nessun brano attualmente in riproduzione."

    async def setTranscriptionChannel(
        self, transcriptionChannel: Optional[discord.TextChannel]
    ) -> Optional[discord.TextChannel]:
        self.transcriptionChannel = transcriptionChannel
        return self.transcriptionChannel

    async def setLooping(self) -> bool:
        self.isLooping = not self.isLooping
        return self.isLooping

    async def setMajorityVote(self) -> bool:
        self.isMajorityVote = not self.isMajorityVote
        return self.isMajorityVote

    async def shuffleQueue(self):
        if self.isConnectedToVC() and len(self.songQueue) > 1:
            first_song = self.songQueue[0]
            rest = self.songQueue[1:]
            random.shuffle(rest)
            self.songQueue[:] = [first_song] + rest

    async def searchSongs(self, query: str):
        searcher = VideoSearcher()
        try:
            result = await searcher.getSearchResults(query)
        except Exception as e:
            logging.error(f"Errore ricerca brani: {e}")
            if self.textChannel:
                await self.textChannel.send(f"❌ Impossibile cercare il brano: {e}")
            return None
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Nessun risultato trovato su YouTube.")
            return None
        return result

    async def speakInVC(self, text: str) -> None:
        """
        Pronuncia il testo in italiano direttamente nel canale vocale (modalità Solo Voce):
        - Se c'è musica in riproduzione, la mette temporaneamente in pausa tramite InterruptibleOpusSource,
          parla e poi riprende la canzone dal punto esatto in cui si era fermata.
        - Se non c'è musica in riproduzione, riproduce direttamente l'audio TTS sul voice_client.
        """
        if not text or not text.strip():
            return
        if not self.isConnectedToVC():
            return

        async with self._tts_lock:
            tts_file: Optional[Path] = None
            try:
                tts_file = await self.tts_engine.synthesize_to_file(text)
                if not tts_file or not tts_file.exists():
                    return

                voice_client = self._get_voice_client()
                if not voice_client or not self.isConnectedToVC():
                    return

                tts_opus = discord.FFmpegOpusAudio(
                    str(tts_file),
                    before_options="-loglevel error",
                    options='-vn -loglevel error -filter:a "volume=1.15"',
                )

                done_event = asyncio.Event()
                loop = self.client.loop

                # Caso 1: Musica attiva o in pausa gestita da InterruptibleOpusSource
                if (
                    (voice_client.is_playing() or voice_client.is_paused())
                    and self.current_audio_source is not None
                ):
                    was_paused = voice_client.is_paused()

                    def _on_tts_end(elapsed_sec: float):
                        if not was_paused:
                            self.pause_duration += int(round(elapsed_sec))
                        else:
                            try:
                                voice_client.pause()
                            except Exception:
                                pass
                        loop.call_soon_threadsafe(done_event.set)

                    self.current_audio_source.interrupt_with(tts_opus, on_end=_on_tts_end)
                    if was_paused:
                        voice_client.resume()

                    start_wait = time.monotonic()
                    while not done_event.is_set() and (time.monotonic() - start_wait) < 20.0:
                        if not self.isConnectedToVC() or (
                            not voice_client.is_playing() and not voice_client.is_paused()
                        ):
                            break
                        try:
                            await asyncio.wait_for(done_event.wait(), timeout=0.25)
                        except asyncio.TimeoutError:
                            continue

                    if not done_event.is_set() and self.current_audio_source is not None:
                        self.current_audio_source.cancel_interrupt()

                # Caso 2: Nessuna canzone in riproduzione
                else:
                    def _after_standalone_tts(error):
                        if error:
                            logging.debug(f"Errore riproduzione TTS: {error}")
                        loop.call_soon_threadsafe(done_event.set)

                    if not voice_client.is_playing():
                        voice_client.play(tts_opus, after=_after_standalone_tts)
                        start_wait = time.monotonic()
                        while not done_event.is_set() and (time.monotonic() - start_wait) < 20.0:
                            if not self.isConnectedToVC() or not voice_client.is_playing():
                                break
                            try:
                                await asyncio.wait_for(done_event.wait(), timeout=0.25)
                            except asyncio.TimeoutError:
                                continue

                        if not done_event.is_set() and voice_client.is_playing():
                            if hasattr(voice_client, "stop_playing"):
                                voice_client.stop_playing()
            except Exception as e:
                logging.error(f"Errore in speakInVC: {e}")
            finally:
                if tts_file and tts_file.exists():
                    try:
                        tts_file.unlink()
                    except Exception:
                        pass

    async def skipSong(self, message: discord.Message = None):
        if not self.isConnectedToVC():
            return
        voice_client = self._get_voice_client()
        if not self.isMajorityVote:
            if self.current_audio_source:
                self.current_audio_source.cancel_interrupt()
            if hasattr(voice_client, "stop_playing"):
                voice_client.stop_playing()
            else:
                voice_client.stop()
            return

        if not message and self.textChannel:
            message = await self.textChannel.send("⏭️ Saltare la canzone corrente? Vota qui sotto:")

        if not message:
            return

        await message.add_reaction("✅")
        await message.add_reaction("❌")

        def check(reaction, user):
            return (
                reaction.message.id == message.id
                and str(reaction.emoji) in ["✅", "❌"]
                and user in voice_client.channel.members
                and not user.bot
            )

        eligible_users = [m for m in voice_client.channel.members if not m.bot]
        required_votes = (len(eligible_users) // 2) + 1

        vote_counts = {"✅": 0, "❌": 0}
        voters = set()

        try:
            while True:
                reaction, user = await self.client.wait_for("reaction_add", timeout=30, check=check)
                if user.id in voters:
                    continue
                vote_counts[str(reaction.emoji)] += 1
                voters.add(user.id)

                if vote_counts["✅"] >= required_votes:
                    if self.textChannel:
                        await self.textChannel.send("✅ Maggioranza raggiunta. Salto il brano.")
                    if hasattr(voice_client, "stop_playing"):
                        voice_client.stop_playing()
                    else:
                        voice_client.stop()
                    return
                elif vote_counts["❌"] >= required_votes:
                    if self.textChannel:
                        await self.textChannel.send("❌ Maggioranza contraria. Il brano continua.")
                    return
                elif len(voters) >= len(eligible_users):
                    if self.textChannel:
                        await self.textChannel.send("ℹ️ Nessuna maggioranza raggiunta. Il brano continua.")
                    return
        except asyncio.TimeoutError:
            if self.textChannel:
                await self.textChannel.send("⌛ Tempo per votare scaduto. Il brano continua.")

    async def pauseSong(self) -> bool:
        if self.isConnectedToVC():
            voice_client = self._get_voice_client()
            if not voice_client.is_paused():
                voice_client.pause()
                self.pause_start = int(time.time())
                return True
            else:
                voice_client.resume()
                if self.pause_start is not None:
                    self.pause_duration += int(time.time()) - self.pause_start
                    self.pause_start = None
                return False
        return False

    async def resumeSong(self):
        if self.isConnectedToVC():
            voice_client = self._get_voice_client()
            if voice_client.is_paused():
                voice_client.resume()
                if self.pause_start is not None:
                    self.pause_duration += int(time.time()) - self.pause_start
                    self.pause_start = None

    async def stopAllSongs(self, message: discord.Message = None):
        if not self.isConnectedToVC():
            return
        voice_client = self._get_voice_client()
        if not self.isMajorityVote:
            self._clear_queue_and_cache()
            self.isLooping = False
            if self.current_audio_source:
                self.current_audio_source.cancel_interrupt()
            if hasattr(voice_client, "stop_playing"):
                voice_client.stop_playing()
            else:
                voice_client.stop()
            return

        if not message and self.textChannel:
            message = await self.textChannel.send(
                "⏹️ Fermare la musica e svuotare la coda? ***Richiede il voto di tutti***"
            )
        if not message:
            return

        await message.add_reaction("✅")
        await message.add_reaction("❌")

        def check(reaction, user):
            return (
                reaction.message.id == message.id
                and str(reaction.emoji) in ["✅", "❌"]
                and user in voice_client.channel.members
                and not user.bot
            )

        eligible_users = [m for m in voice_client.channel.members if not m.bot]
        required_votes = max(1, len(eligible_users))

        vote_counts = {"✅": 0, "❌": 0}
        voters = set()

        try:
            while True:
                reaction, user = await self.client.wait_for("reaction_add", timeout=45, check=check)
                if user.id in voters:
                    continue
                vote_counts[str(reaction.emoji)] += 1
                voters.add(user.id)

                if vote_counts["✅"] >= required_votes:
                    if self.textChannel:
                        await self.textChannel.send("⏹️ Votazione approvata. Fermo la musica e svuoto la coda.")
                    self._clear_queue_and_cache()
                    self.isLooping = False
                    if hasattr(voice_client, "stop_playing"):
                        voice_client.stop_playing()
                    else:
                        voice_client.stop()
                    return
                elif vote_counts["❌"] >= required_votes:
                    if self.textChannel:
                        await self.textChannel.send("❌ Votazione respinta. La riproduzione continua.")
                    return
                elif len(voters) >= len(eligible_users):
                    if self.textChannel:
                        await self.textChannel.send("ℹ️ Nessuna unanimità raggiunta. La riproduzione continua.")
                    return
        except asyncio.TimeoutError:
            if self.textChannel:
                await self.textChannel.send("⌛ Tempo per votare scaduto. Il brano continua.")

    async def softDisconnect(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None
        self._clear_queue_and_cache()
        self.isLooping = False
        await self._cleanup_stale_voice_client()
        logging.debug(f"{self.guild.name} MusicController disconnesso (soft).")

    async def hardDisconnect(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None
        self._clear_queue_and_cache()
        self.isLooping = False
        self.voiceChannel = None
        self.textChannel = None
        await self._cleanup_stale_voice_client()
        logging.debug(f"{self.guild.name} MusicController disconnesso (hard).")

    async def two_four_seven(
        self, voiceChannel: discord.VoiceChannel, textChannel: discord.TextChannel
    ) -> Optional[discord.VoiceClient]:
        """
        Collega o riallinea il bot al canale vocale indicato, pulendo eventuali
        sessioni zombie e garantendo che l'ascolto vocale sia attivo.
        Restituisce None se ha effettuato una nuova connessione, oppure il voice_client se era già lì.
        """
        async with self._connect_lock:
            self.voiceChannel = voiceChannel
            self.textChannel = textChannel

            if self.isConnectedToVC():
                voice_client = self._get_voice_client()
                if voice_client and voice_client.channel and voice_client.channel.id == voiceChannel.id:
                    if not (hasattr(voice_client, "is_listening") and voice_client.is_listening()):
                        await self.startVoiceRecording()
                    self._ensure_watchdog()
                    return voice_client

            # Non connesso o connesso a un altro canale / zombie client: pulisci e riconnetti
            await self._cleanup_stale_voice_client()
            await voiceChannel.connect(
                cls=voice_recv.VoiceRecvClient, reconnect=True, timeout=15.0
            )
            logging.info(f"Bot connesso al canale vocale: {voiceChannel.name}")
            await self.startVoiceRecording()
            self._ensure_watchdog()
            return None

    async def startVoiceRecording(self):
        """Avvia l'ascolto continuo in italiano nel canale vocale."""
        def on_transcription(user: discord.Member, text: str):
            asyncio.run_coroutine_threadsafe(
                self.handleTranscribedAudio(user, text), self.client.loop
            )

        voice_client = self._get_voice_client()
        if not voice_client or not self.isConnectedToVC():
            return
        try:
            if hasattr(voice_client, "is_listening") and voice_client.is_listening():
                voice_client.stop_listening()
            sink = ItalianSpeechSink(on_transcription=on_transcription, language="it-IT")
            voice_client.listen(sink)
            logging.info(f"Ascolto vocale in italiano attivo su: {self.guild.name}")
        except Exception as e:
            logging.exception(f"Errore avvio ascolto vocale: {e}")

    async def handleTranscribedAudio(self, user: discord.Member, text: str):
        if not text or not text.strip():
            return

        logging.info(f"[Voce] {user.display_name}: {text}")

        if self.transcriptionChannel:
            try:
                await self.transcriptionChannel.send(f"🗣️ **{user.display_name}**: {text}")
            except Exception as e:
                logging.debug(f"Errore invio trascrizione: {e}")

        word_counters_data = await WordCounter().loadWordCounters()
        keywords_map = word_counters_data[0] if word_counters_data else {}
        text_lower = text.lower()
        if any(
            re.search(rf"\b{re.escape(kw)}\b", text_lower)
            for kw in keywords_map.keys()
        ):
            await self.handle_word_counter_keyword(user, text_lower)

        kw_manager = BotKeywords()
        activated, matched_wake, remainder = await kw_manager.extractCommandAfterWakeWord(text)

        # Controlla se l'utente è nella finestra di ascolto attivo (8 secondi dopo aver detto solo "Venus")
        now_mono = time.monotonic()
        if not activated and self._active_listener_until.get(user.id, 0.0) > now_mono:
            activated = True
            matched_wake = "followup"
            remainder = text.strip()
            self._active_listener_until.pop(user.id, None)

        if not activated:
            return

        banned_ids = await BannedUsers().loadBannedUserIDs()
        if user.id in banned_ids:
            await self.speakInVC(f"Mi dispiace {user.display_name}, non sei autorizzato a usare i comandi.")
            return

        logging.info(
            f"[Assistente Attivato da '{matched_wake}'] {user.display_name} -> '{remainder}'"
        )

        current_song_title = self.songQueue[0].title if self.songQueue else None
        queue_count = max(0, len(self.songQueue) - 1)

        assistant_resp = await self.ai_assistant.process_request(
            guild_id=self.guild.id,
            user_name=user.display_name,
            user_text=remainder,
            current_song=current_song_title,
            queue_count=queue_count,
        )

        await self.executeAssistantResponse(user, assistant_resp)

    async def executeAssistantResponse(self, user: discord.Member, resp):
        action = resp.action
        query = resp.query
        voice_reply = resp.voice_reply

        if action == "listen_followup":
            await self.speakInVC(voice_reply or "Dimmi pure.")
            self._active_listener_until[user.id] = time.monotonic() + 8.0
            return

        if action == "none":
            await self.speakInVC(voice_reply)
            return

        if action == "now_playing":
            if self.songQueue:
                dur = await self.getCurrentDuration()
                clean_dur = dur.replace("*", "")
                spoken = f"In questo momento sta suonando {self.songQueue[0].title}. Durata: {clean_dur}."
            else:
                spoken = "Al momento non c'è nessuna canzone in riproduzione."
            await self.speakInVC(spoken)
            return

        if action == "play":
            if not query:
                await self.speakInVC("Riprendo la riproduzione.")
                await self.resumeSong()
                return

            # Pronuncia prima la conferma vocale e poi avvia il caricamento/riproduzione
            # per evitare qualsiasi conflitto sul player audio di Discord
            await self.speakInVC(voice_reply or f"Riproduco {query}.")
            await self.determineSongSource(user, query)
            return

        if action == "pause":
            voice_client = self._get_voice_client()
            if voice_client and not voice_client.is_paused():
                await self.pauseSong()
                await self.speakInVC("Musica in pausa.")
            else:
                await self.resumeSong()
                await self.speakInVC("Riprendo la musica.")
            return

        if action == "resume":
            await self.resumeSong()
            await self.speakInVC(voice_reply or "Riprendo la riproduzione.")
            return

        if action == "skip":
            await self.speakInVC(voice_reply or "Salto il brano.")
            await self.skipSong()
            return

        if action == "stop":
            await self.stopAllSongs()
            await self.speakInVC(voice_reply or "Fermo la musica e svuoto la coda.")
            return

        if action == "loop":
            enabled = await self.setLooping()
            msg = "Ripetizione del brano attivata." if enabled else "Ripetizione disattivata."
            await self.speakInVC(msg)
            return

        if action == "shuffle":
            await self.shuffleQueue()
            await self.speakInVC(voice_reply or "Ho mescolato la coda di riproduzione.")
            return

        if action == "volume_up":
            new_vol = self.setVolume(int(self.volume * 100) + 20)
            await self.speakInVC(f"Volume alzato al {new_vol} percento.")
            return

        if action == "volume_down":
            new_vol = self.setVolume(int(self.volume * 100) - 20)
            await self.speakInVC(f"Volume abbassato al {new_vol} percento.")
            return

        if action == "volume_set":
            try:
                target = int(re.sub(r"\D", "", query) or "70")
            except ValueError:
                target = 70
            new_vol = self.setVolume(target)
            await self.speakInVC(f"Volume impostato al {new_vol} percento.")
            return

        if action == "disconnect":
            await self.speakInVC(voice_reply or "A presto!")
            await self.hardDisconnect()
            return

        if voice_reply:
            await self.speakInVC(voice_reply)

    async def handle_word_counter_keyword(self, user: discord.Member, text_lower: str):
        wordCounterClass = WordCounter()
        mapping = (await wordCounterClass.loadWordCounters())[0]

        for keyword, counter_name in mapping.items():
            matches = re.findall(rf"\b{re.escape(keyword)}\b", text_lower)
            count = len(matches)
            if count > 0:
                for _ in range(count):
                    await wordCounterClass.incrementCounterForKeyword(keyword)
                    new_count = await wordCounterClass.getCount(counter_name)
                    if self.textChannel:
                        embed = discord.Embed(
                            title=f"🔢 Contatore {counter_name.capitalize()}:",
                            description=f"**{new_count}** — pronunciato da {user.display_name}",
                            color=0xA600FF,
                        )
                        if self._bot_avatar_url():
                            embed.set_thumbnail(url=self._bot_avatar_url())
                        await self.textChannel.send(embed=embed)

    def _build_song_from_result(self, result: dict, user: discord.User, fallback_url: Optional[str] = None) -> Song:
        return Song(
            title=result["title"],
            url=result.get("url") or fallback_url or result["link"],
            link=result["link"],
            thumbnail=result.get("thumbnail") or self._bot_avatar_url(),
            duration=result.get("duration", 0),
            user=user,
            isFile=False,
            http_headers=result.get("http_headers"),
            is_local_cache=bool(result.get("is_local_cache")),
        )

    async def handleFile(self, user: discord.User, file: discord.Attachment):
        fileSong = Song(
            file.filename,
            file.url,
            file.url,
            self._bot_avatar_url(),
            0,
            user,
            isFile=True,
        )
        await self.queueSong(fileSong)

    async def determineSongSource(self, user: discord.User, query: str):
        query_lower = query.lower().strip()

        youtube_pattern = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/")
        youtube_playlist_pattern = re.compile(
            r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/playlist\?list=[\w-]+"
        )
        spotify_pattern = re.compile(r"(https?://)?(open\.)?spotify\.com/")
        spotify_playlist_pattern = re.compile(
            r"(https?://)?(open\.)?spotify\.com/(?:intl-[a-z]+/)?(playlist|album)/[a-zA-Z0-9]+"
        )
        soundcloud_pattern = re.compile(r"(https?://)?(www\.)?soundcloud\.com/")
        soundcloud_playlist_pattern = re.compile(
            r"^(https?://)?(www\.)?soundcloud\.com/[^/]+/sets/[^/]+/?"
        )

        if youtube_pattern.search(query_lower):
            if youtube_playlist_pattern.search(query_lower):
                return await self.handleYoutubePlaylist(user, query)
            return await self.handleYoutubeLink(user, query)
        elif spotify_pattern.search(query_lower):
            if spotify_playlist_pattern.search(query_lower):
                return await self.handleSpotifyPlaylist(user, query)
            return await self.handleSpotifyLink(user, query)
        elif soundcloud_pattern.search(query_lower):
            if soundcloud_playlist_pattern.search(query_lower):
                return await self.handleSoundCloudPlaylist(user, query)
            return await self.handleSoundCloudLink(user, query)
        else:
            return await self.handleYoutubeSearch(user, query)

    async def handleYoutubeLink(self, user, url):
        searcher = VideoSearcher()
        try:
            result = await searcher.getVideoInfoFromURL(url)
        except Exception as e:
            logging.error(e)
            if self.textChannel:
                await self.textChannel.send(f"❌ Impossibile aggiungere il brano: {e}")
            return
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Brano non trovato.")
            return
        youtubeSong = self._build_song_from_result(result, user, fallback_url=url)
        await self.queueSong(youtubeSong)

    async def handleYoutubePlaylist(self, user, url):
        searcher = VideoSearcher()
        result = await searcher.getPlaylistInfo(url)
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Impossibile trovare la playlist.")
            return
        metadata = result.pop(0)
        thumbnail = metadata.get("thumbnail") or self._bot_avatar_url()
        if self.textChannel:
            embed = discord.Embed(title="📂 Aggiunta Playlist:", color=0xA600FF)
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            embed.add_field(name="Nome Playlist", value=metadata["playlist_name"], inline=False)
            embed.add_field(name="Numero di Brani", value=str(metadata["song_count"]), inline=False)
            await self.textChannel.send(embed=embed)

        for song in result:
            try:
                songInfo = await searcher.getVideoInfoFromURL(song["url"])
                if not songInfo:
                    continue
                youtubeSong = self._build_song_from_result(songInfo, user, fallback_url=song["url"])
                await self.queueSong(youtubeSong)
            except Exception as e:
                logging.error(e)
                continue

    async def handleSpotifyLink(self, user, url):
        try:
            spotifySongInfo = await self.spotify.getSpotifySongInfo(url)
        except Exception as e:
            logging.error(e)
            if self.textChannel:
                await self.textChannel.send(f"❌ Errore lettura link Spotify: {e}")
            return

        artist = spotifySongInfo.get("artist", "")
        query = f"{spotifySongInfo['title']} {artist}".strip()
        searcher = VideoSearcher()
        try:
            result = await searcher.getVideoInfoFromQuery(query)
        except Exception as e:
            logging.error(e)
            if self.textChannel:
                await self.textChannel.send(f"❌ Impossibile aggiungere il brano: {e}")
            return
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Brano non trovato.")
            return
        youtubeSong = self._build_song_from_result(result, user)
        await self.queueSong(youtubeSong)

    async def handleSpotifyPlaylist(self, user, playlist):
        try:
            result = await self.spotify.getSpotifyPlaylistInfo(playlist)
        except Exception as e:
            if self.textChannel:
                await self.textChannel.send(f"❌ Impossibile caricare la playlist Spotify: {e}")
            return
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Playlist o album Spotify non trovato.")
            return
        playlist_info = result.pop(0)
        thumbnail = playlist_info.get("thumbnail") or self._bot_avatar_url()
        if self.textChannel:
            embed = discord.Embed(title="📂 Aggiunta Playlist Spotify:", color=0xA600FF)
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            embed.add_field(name="Nome Playlist", value=playlist_info["title"], inline=False)
            embed.add_field(name="Numero di Brani", value=str(len(result)), inline=False)
            await self.textChannel.send(embed=embed)

        searcher = VideoSearcher()
        for song in result:
            query = f"{song['title']} {song['artist']}".strip()
            try:
                songInfo = await searcher.getVideoInfoFromQuery(query)
                if not songInfo:
                    continue
                youtubeSong = self._build_song_from_result(songInfo, user)
                await self.queueSong(youtubeSong)
            except Exception as e:
                logging.error(e)
                continue

    async def handleSoundCloudLink(self, user, url):
        searcher = VideoSearcher()
        try:
            result = await searcher.getVideoInfoFromURL(url)
        except Exception as e:
            logging.error(e)
            if self.textChannel:
                await self.textChannel.send(f"❌ Impossibile aggiungere il brano: {e}")
            return
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Brano SoundCloud non trovato.")
            return
        soundcloudSong = self._build_song_from_result(result, user, fallback_url=url)
        await self.queueSong(soundcloudSong)

    async def handleSoundCloudPlaylist(self, user, url):
        searcher = VideoSearcher()
        result = await searcher.getPlaylistInfo(url)
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Impossibile trovare la playlist SoundCloud.")
            return
        metadata = result.pop(0)
        thumbnail = metadata.get("thumbnail") or self._bot_avatar_url()
        if self.textChannel:
            embed = discord.Embed(title="📂 Aggiunta Playlist SoundCloud:", color=0xA600FF)
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            embed.add_field(name="Nome Playlist", value=metadata["playlist_name"], inline=False)
            embed.add_field(name="Numero di Brani", value=str(metadata["song_count"]), inline=False)
            await self.textChannel.send(embed=embed)

        for song in result:
            try:
                songInfo = await searcher.getVideoInfoFromURL(song["url"])
                if not songInfo:
                    continue
                soundcloudSong = self._build_song_from_result(songInfo, user, fallback_url=song["url"])
                await self.queueSong(soundcloudSong)
            except Exception as e:
                logging.error(e)
                continue

    async def handleYoutubeSearch(self, user, query):
        searcher = VideoSearcher()
        try:
            result = await searcher.getVideoInfoFromQuery(query)
        except Exception as e:
            logging.error(e)
            if self.textChannel:
                await self.textChannel.send(f"❌ Impossibile cercare il brano: {e}")
            return
        if not result:
            if self.textChannel:
                await self.textChannel.send("❌ Nessun brano trovato.")
            return
        youtubeSong = self._build_song_from_result(result, user)
        await self.queueSong(youtubeSong)

    async def queueSong(self, song: Song):
        if not self.songQueue:
            self.songQueue.append(song)
            await self.playSong()
        else:
            self.songQueue.append(song)
            if self.textChannel:
                embed = discord.Embed(
                    title="➕ Aggiunto alla Coda:",
                    description=song.title,
                    color=0xA600FF,
                )
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                await self.textChannel.send(embed=embed)

    @staticmethod
    def _build_ffmpeg_before_options(song: Song) -> str:
        """
        Costruisce le opzioni FFmpeg:
        - Se il file è già scaricato in cache locale da yt-dlp, non servono opzioni di rete.
        - Se è uno stream HTTP (live radio o file remoto), passa User-Agent e Headers di yt-dlp.
        """
        if song.is_local_cache and Path(song.link).exists():
            return "-loglevel error"

        parts = ["-loglevel error"]
        headers = song.http_headers or {}
        user_agent = headers.get("User-Agent") or headers.get("user-agent")
        if user_agent:
            safe_ua = str(user_agent).replace('"', '\\"')
            parts.append(f'-user_agent "{safe_ua}"')

        parts.append("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
        return " ".join(parts)

    async def playSong(self):
        if not self.songQueue:
            self.current_audio_source = None
            self.start_time = None
            self.pause_duration = 0
            self.pause_start = None
            return

        # Attendi eventuale TTS standalone in corso prima di avviare il player musicale
        if self._tts_lock.locked():
            async with self._tts_lock:
                pass

        if not self.songQueue:
            return

        song = self.songQueue[0]

        if not song.isFile:
            # Se era un file locale che è stato rimosso o uno stream URL scaduto, rigeneralo
            needs_refresh = False
            if song.is_local_cache and not Path(song.link).exists():
                needs_refresh = True
            elif not song.is_local_cache:
                exp = getSongExpiration(song.link)
                if exp is not None and exp <= int(time.time()):
                    needs_refresh = True

            if needs_refresh:
                logging.debug("Rigenerazione risorsa audio per il brano...")
                searcher = VideoSearcher()
                try:
                    result = await searcher.getVideoInfoFromURL(song.url)
                    if result and result.get("link"):
                        song.link = result["link"]
                        song.http_headers = result.get("http_headers") or {}
                        song.is_local_cache = bool(result.get("is_local_cache"))
                except Exception as e:
                    logging.error(f"Errore aggiornamento brano: {e}")
                    if self.textChannel:
                        await self.textChannel.send(f"❌ Impossibile riprodurre il brano: {e}")
                    self.songQueue.pop(0)
                    return await self.playSong()

        ffmpeg_options = {
            "before_options": self._build_ffmpeg_before_options(song),
            "options": f'-vn -loglevel error -filter:a "volume={self.volume:.2f}"',
        }

        raw_player = discord.FFmpegOpusAudio(song.link, **ffmpeg_options)
        interruptible_player = InterruptibleOpusSource(raw_player)
        self.current_audio_source = interruptible_player

        def after_playing(error):
            if error:
                logging.error(f"Errore durante la riproduzione: {error}")
            interruptible_player.cancel_interrupt()
            if self.current_audio_source is interruptible_player:
                self.current_audio_source = None
            if not self.isLooping and self.songQueue:
                finished_song = self.songQueue.pop(0)
                finished_song.cleanup_cache_file()
            fut = asyncio.run_coroutine_threadsafe(self.playSong(), self.client.loop)
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

        voice_client = self._get_voice_client()
        if not voice_client or not self.isConnectedToVC():
            self.current_audio_source = None
            return

        if voice_client.is_playing():
            if hasattr(voice_client, "stop_playing"):
                voice_client.stop_playing()

        voice_client.play(interruptible_player, after=after_playing)

        self.start_time = int(time.time())
        self.pause_duration = 0
        self.pause_start = None

        if self.textChannel:
            embed = discord.Embed(
                title="🎶 In Riproduzione:",
                description=song.title,
                color=0xA600FF,
            )
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            await self.textChannel.send(embed=embed, view=MusicButtons(client=self.client))
