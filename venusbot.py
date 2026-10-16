import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

from embed_views.counters_view import CountersView
from embed_views.keywords_view import KeywordsView
from embed_views.queue_view import QueueView
from embed_views.search_view import SearchView
from management.banned_users import BannedUsers
from management.bot_keywords import BotKeywords
from management.vip_users import VIPUsers
from music_controller import MusicController
from scripts.voice_engine import ITALIAN_TTS_VOICES

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(
            f"{Path(__file__).resolve().parent / 'bot_log.log'}",
            mode="w",
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

logging.getLogger("discord").setLevel(logging.WARNING)

# Caricamento automatico di libopus su Linux / Docker
if not discord.opus.is_loaded():
    for _opus_candidate in ("libopus.so.0", "libopus.so", "opus"):
        try:
            discord.opus.load_opus(_opus_candidate)
            if discord.opus.is_loaded():
                logging.info(f"Libreria audio Opus caricata con successo ({_opus_candidate}).")
                break
        except Exception:
            continue



class VenusBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        load_dotenv()
        self.token = (os.getenv("DISCORD_TOKEN") or "").strip().strip("'\"")
        self.musicControllers: dict[int, MusicController] = {}

    async def getGuildMusicController(self, guild: discord.Guild) -> MusicController:
        if guild.id not in self.musicControllers:
            self.musicControllers[guild.id] = MusicController(client=self, guild=guild)
        return self.musicControllers[guild.id]

    async def popGuildMusicController(self, guild: discord.Guild):
        if guild.id in self.musicControllers:
            self.musicControllers.pop(guild.id)

    def restart_bot(self):
        os.execv(sys.executable, [sys.executable] + sys.argv)

    async def on_ready(self):
        logging.info(f"{self.user} è online e pronto!")
        await self.tree.sync()
        logging.info("Slash commands sincronizzati con successo.")

    async def on_connect(self):
        logging.info(f"{self.user} si è connesso a Discord.")

    async def start_bot(self):
        if not self.token:
            logging.error(
                "DISCORD_TOKEN mancante! Inserisci il token del tuo bot nel file .env."
            )
            return
        await self.start(self.token)


bot = VenusBot()


async def _check_banned(interaction: discord.Interaction) -> bool:
    banned_ids = await BannedUsers().loadBannedUserIDs()
    if interaction.user.id in banned_ids:
        await interaction.response.send_message(
            f"⛔ L'utente **{interaction.user.display_name}** è bannato dall'utilizzo del bot.",
            ephemeral=True,
        )
        return True
    return False


# Gestione automatica 24/7 Smart Connect e pulizia stato vocale del bot
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    musicController = await bot.getGuildMusicController(member.guild)

    # Se è il bot stesso ad aver cambiato stato vocale (es. disconnesso/espulso o spostato)
    if member == bot.user:
        if before.channel is not None and after.channel is None:
            logging.info(
                f"Il bot è stato disconnesso dal canale vocale in {member.guild.name}. Pulizia sessione..."
            )
            await musicController.handleBotDisconnectedFromVC()
        elif after.channel is not None and before.channel != after.channel:
            musicController.voiceChannel = after.channel
            await musicController.startVoiceRecording()
        return

    voiceChannel, textChannel = musicController.getVideoAndTextChannel()
    if not voiceChannel or not textChannel:
        return

    # Utente entra in un canale vocale
    if before.channel is None and after.channel is not None:
        human_members = [m for m in after.channel.members if not m.bot]
        if len(human_members) == 1 and after.channel == voiceChannel:
            await musicController.two_four_seven(voiceChannel, textChannel)

    # Utente esce da un canale vocale
    elif before.channel is not None and after.channel is None:
        human_members = [m for m in before.channel.members if not m.bot]
        if len(human_members) == 0 and before.channel == voiceChannel:
            await musicController.softDisconnect()

    # Utente cambia canale vocale
    elif before.channel != after.channel:
        if before.channel == voiceChannel:
            human_before = [m for m in before.channel.members if not m.bot]
            if len(human_before) == 0:
                await musicController.softDisconnect()

        if after.channel == voiceChannel:
            human_after = [m for m in after.channel.members if not m.bot]
            if len(human_after) == 1:
                await musicController.two_four_seven(voiceChannel, textChannel)


# =====================================================================
# COMANDI ADMIN PER PAROLA DI ATTIVAZIONE (WAKE WORD) E VOCE ASSISTENTE
# =====================================================================

@bot.tree.command(
    name="setwakeword",
    description="Admin - Imposta rapidamente la parola di attivazione vocale principale (wake word).",
)
@app_commands.describe(
    parola="La nuova parola di attivazione (es. venus, jarvis, assistente, computer)",
    mantieni_alias="Se Vero, mantiene la vecchia parola principale come alias secondario",
)
async def setWakeWord(
    interaction: discord.Interaction,
    parola: str,
    mantieni_alias: bool = False,
):
    logging.info(f"{interaction.user.name} ha eseguito /setwakeword con '{parola}'")

    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message(
            "⛔ Comando riservato agli amministratori del bot.", ephemeral=True
        )
        return

    kw_manager = BotKeywords()
    try:
        new_primary = await kw_manager.setPrimaryKeyword(
            parola, keep_old_as_alias=mantieni_alias
        )
        config = await kw_manager.loadConfig()
        aliases = config["aliases"]
        alias_str = ", ".join(f"`{a}`" for a in aliases) if aliases else "*Nessuno*"

        embed = discord.Embed(
            title="✅ Parola di Attivazione Aggiornata!",
            description=(
                f"Ora puoi attivare l'assistente dicendo **`{new_primary.capitalize()}`** "
                "seguito da una domanda o da un comando musicale."
            ),
            color=0xA600FF,
        )
        embed.add_field(
            name="⭐ Parola Principale", value=f"**`{new_primary}`**", inline=True
        )
        embed.add_field(name="🔄 Alias Attivi", value=alias_str, inline=True)
        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Errore: {e}", ephemeral=True)


@bot.tree.command(
    name="wakeword",
    description="Admin - Gestisci la parola di attivazione principale e gli alias secondari.",
)
@app_commands.describe(
    azione="Scegli l'operazione da effettuare sulla parola di attivazione",
    parola="Parola o frase da impostare, aggiungere o rimuovere",
)
async def wakeword(
    interaction: discord.Interaction,
    azione: Literal["imposta", "aggiungi_alias", "rimuovi_alias", "lista", "reset"],
    parola: Optional[str] = None,
):
    logging.info(f"{interaction.user.name} ha eseguito /wakeword ({azione}, {parola})")

    kw_manager = BotKeywords()

    if azione == "lista":
        if await _check_banned(interaction):
            return
        view = KeywordsView(bot)
        await view.send_page(interaction, first_response=True)
        return

    # Tutte le operazioni di modifica richiedono privilegi Admin
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message(
            "⛔ Comando riservato agli amministratori del bot.", ephemeral=True
        )
        return

    try:
        if azione == "imposta":
            if not parola or not parola.strip():
                await interaction.response.send_message(
                    "⚠️ Specifica la nuova parola di attivazione nel parametro `parola`.",
                    ephemeral=True,
                )
                return
            new_kw = await kw_manager.setPrimaryKeyword(parola)
            await interaction.response.send_message(
                f"✅ Parola di attivazione principale impostata su: **`{new_kw}`**"
            )

        elif azione == "aggiungi_alias":
            if not parola or not parola.strip():
                await interaction.response.send_message(
                    "⚠️ Specifica l'alias da aggiungere nel parametro `parola`.",
                    ephemeral=True,
                )
                return
            added = await kw_manager.addBotKeyword(parola)
            await interaction.response.send_message(
                f"✅ Alias **`{added}`** aggiunto alle parole di attivazione."
            )

        elif azione == "rimuovi_alias":
            if not parola or not parola.strip():
                await interaction.response.send_message(
                    "⚠️ Specifica l'alias da rimuovere nel parametro `parola`.",
                    ephemeral=True,
                )
                return
            removed = await kw_manager.removeBotKeyword(parola)
            if removed:
                await interaction.response.send_message(
                    f"🗑️ Parola **`{parola.strip().lower()}`** rimossa con successo."
                )
            else:
                await interaction.response.send_message(
                    f"ℹ️ La parola **`{parola}`** non era presente tra gli alias (oppure è l'unica parola principale attiva).",
                    ephemeral=True,
                )

        elif azione == "reset":
            config = await kw_manager.resetKeywords("venus")
            await interaction.response.send_message(
                f"🔄 Parole di attivazione ripristinate ai valori predefiniti: principale **`{config['primary']}`**, alias `{', '.join(config['aliases'])}`."
            )
    except Exception as e:
        await interaction.response.send_message(f"❌ Errore: {e}", ephemeral=True)


@bot.tree.command(
    name="keywords",
    description="Mostra e gestisci la parola di attivazione vocale e gli alias.",
)
async def keywords(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return
    view = KeywordsView(bot)
    await view.send_page(interaction, first_response=True)


@bot.tree.command(
    name="voce",
    description="Admin - Cambia la voce italiana dell'assistente vocale.",
)
@app_commands.describe(voce="Scegli il timbro vocale italiano per le risposte TTS")
async def changeVoice(
    interaction: discord.Interaction,
    voce: Literal["elsa", "isabella", "diego", "giuseppe"],
):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message(
            "⛔ Comando riservato agli amministratori.", ephemeral=True
        )
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    selected = musicController.setTTSVoice(voce)
    await interaction.response.send_message(
        f"🗣️ Voce dell'assistente impostata su **{voce.capitalize()}** (`{selected}`)."
    )
    if musicController.isConnectedToVC():
        await musicController.speakInVC(
            f"Ciao! Da adesso risponderò con la voce di {voce.capitalize()}."
        )


@bot.tree.command(
    name="chiedi",
    description="Fai una domanda all'assistente intelligente (risponde a voce se connesso al canale vocale).",
)
@app_commands.describe(domanda="La domanda o richiesta per l'assistente")
async def chiedi(interaction: discord.Interaction, domanda: str):
    if await _check_banned(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    musicController = await bot.getGuildMusicController(guild=interaction.guild)

    current_song = (
        musicController.songQueue[0].title if musicController.songQueue else None
    )
    queue_count = max(0, len(musicController.songQueue) - 1)

    resp = await musicController.ai_assistant.process_request(
        guild_id=interaction.guild.id,
        user_name=interaction.user.display_name,
        user_text=domanda,
        current_song=current_song,
        queue_count=queue_count,
    )

    if musicController.isConnectedToVC():
        await interaction.followup.send(
            "🔊 Rispondo a voce nel canale vocale...", ephemeral=True
        )
        await musicController.executeAssistantResponse(interaction.user, resp)
    else:
        await interaction.followup.send(
            f"🎙️ **{resp.voice_reply}**", ephemeral=True
        )


# =====================================================================
# COMANDI MUSICALI E DI CONTROLLO
# =====================================================================

@bot.tree.command(
    name="transcribe",
    description="Trascrive la chat vocale in un canale di testo. Se non selezioni un canale, la disattiva.",
)
async def transcribe(
    interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None
):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    response = await musicController.setTranscriptionChannel(channel)
    if response:
        await interaction.response.send_message(
            f"📝 Trascrizione vocale attivata nel canale **{response.name}**."
        )
    else:
        await interaction.response.send_message("📝 Trascrizione vocale disattivata.")


@bot.tree.command(
    name="counters",
    description="Mostra tutti i contatori di parole pronunciate in vocale.",
)
async def counters(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return
    view = CountersView(bot)
    await view.send_page(interaction, first_response=True)


@bot.tree.command(
    name="play",
    description="Riproduci un brano o playlist da YouTube, Spotify o SoundCloud.",
)
async def play(interaction: discord.Interaction, query: str):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    if not musicController.isConnectedToVC():
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "⚠️ Devi essere in un canale vocale per usare questo comando.",
                ephemeral=True,
            )
            return

    # Rispondi subito all'interazione per evitare il timeout di 3 secondi di Discord
    await interaction.response.send_message(
        f"🔍 Ricerca di **{query}** in corso...", delete_after=6
    )

    if not musicController.isConnectedToVC():
        try:
            await musicController.two_four_seven(
                interaction.user.voice.channel, interaction.channel
            )
        except Exception as e:
            logging.error(f"Errore connessione vocale in /play: {e}")
            await interaction.channel.send(
                f"❌ Impossibile connettersi al canale vocale: {e}"
            )
            return

    await musicController.determineSongSource(interaction.user, query)


@bot.tree.command(name="playfile", description="Riproduce un file audio caricato.")
async def playfile(interaction: discord.Interaction, file: discord.Attachment):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    if not musicController.isConnectedToVC():
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "⚠️ Devi essere in un canale vocale per usare questo comando.",
                ephemeral=True,
            )
            return

    await interaction.response.send_message(
        f"📥 Caricamento file audio: **{file.filename}**", delete_after=4
    )

    if not musicController.isConnectedToVC():
        await musicController.two_four_seven(
            interaction.user.voice.channel, interaction.channel
        )

    await musicController.handleFile(interaction.user, file)


@bot.tree.command(
    name="247",
    description="Collega il bot a un canale vocale in modalità 24/7 con ascolto vocale attivo.",
)
async def two_four_seven(
    interaction: discord.Interaction, channel: discord.VoiceChannel
):
    if await _check_banned(interaction):
        return

    await interaction.response.defer()
    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    try:
        response = await musicController.two_four_seven(channel, interaction.channel)
        primary_kw = await BotKeywords().getPrimaryKeyword()
        if response:
            await interaction.followup.send(
                f"🎧 Il bot è già attivo e in ascolto in **{response.channel.name}** (Parola di attivazione: **`{primary_kw}`**)."
            )
        else:
            await interaction.followup.send(
                f"✅ Modalità 24/7 e assistente vocale attivati in **{channel.name}** (Parola di attivazione: **`{primary_kw}`**)."
            )
    except Exception as e:
        logging.error(f"Errore comando /247: {e}")
        await interaction.followup.send(
            f"❌ Errore durante la connessione al canale vocale **{channel.name}**: {e}"
        )


@bot.tree.command(name="lofi", description="Riproduce una radio Lofi 24/7.")
async def lofi(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    if not musicController.isConnectedToVC():
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "⚠️ Devi essere in un canale vocale.", ephemeral=True
            )
            return

    await interaction.response.send_message("📻 Avvio Radio Lofi...", delete_after=5)
    if not musicController.isConnectedToVC():
        await musicController.two_four_seven(
            interaction.user.voice.channel, interaction.channel
        )
    await musicController.determineSongSource(
        interaction.user, "https://www.youtube.com/watch?v=jfKfPfyJRdk"
    )


@bot.tree.command(name="lofijazz", description="Riproduce una radio Lofi Jazz 24/7.")
async def lofijazz(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    if not musicController.isConnectedToVC():
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "⚠️ Devi essere in un canale vocale.", ephemeral=True
            )
            return

    await interaction.response.send_message("🎷 Avvio Radio Lofi Jazz...", delete_after=5)
    if not musicController.isConnectedToVC():
        await musicController.two_four_seven(
            interaction.user.voice.channel, interaction.channel
        )
    await musicController.determineSongSource(
        interaction.user, "https://www.youtube.com/watch?v=HuFYqnbVbzY"
    )


@bot.tree.command(name="synthwave", description="Riproduce una radio SynthWave 24/7.")
async def synthwave(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    if not musicController.isConnectedToVC():
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "⚠️ Devi essere in un canale vocale.", ephemeral=True
            )
            return

    await interaction.response.send_message("🌆 Avvio Radio SynthWave...", delete_after=5)
    if not musicController.isConnectedToVC():
        await musicController.two_four_seven(
            interaction.user.voice.channel, interaction.channel
        )
    await musicController.determineSongSource(
        interaction.user, "https://www.youtube.com/watch?v=4xDzrJKXOOY"
    )


@bot.tree.command(
    name="pause",
    description="Mette in pausa il brano corrente (o lo riprende se già in pausa).",
)
async def pause(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    paused = await musicController.pauseSong()
    if paused:
        await interaction.response.send_message("⏸️ Brano messo in pausa.")
    else:
        await interaction.response.send_message("▶️ Riproduzione ripresa.")


@bot.tree.command(name="resume", description="Riprende la riproduzione del brano in pausa.")
async def resume(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    await musicController.resumeSong()
    await interaction.response.send_message("▶️ Riproduzione ripresa.")


@bot.tree.command(name="volume", description="Imposta il volume della musica (5-150%).")
@app_commands.describe(livello="Livello del volume in percentuale (es. 70)")
async def volume(interaction: discord.Interaction, livello: int):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    new_vol = musicController.setVolume(livello)
    await interaction.response.send_message(f"🔊 Volume impostato al **{new_vol}%**.")


@bot.tree.command(name="shuffle", description="Mescola i brani presenti nella coda.")
async def shuffle(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    await musicController.shuffleQueue()
    await interaction.response.send_message("🔀 La coda è stata mescolata.")


@bot.tree.command(name="time", description="Mostra il tempo trascorso e la durata del brano corrente.")
async def time_cmd(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    response = await musicController.getCurrentDuration()
    await interaction.response.send_message(f"⏱️ {response}", ephemeral=True, delete_after=8)


@bot.tree.command(name="stop", description="Ferma la riproduzione e svuota la coda.")
async def stop(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    if not musicController.getIsMajorityVote():
        await interaction.response.send_message("⏹️ Riproduzione fermata e coda svuotata.")
        await musicController.stopAllSongs()
    else:
        await interaction.response.send_message(
            "⏹️ Fermare il brano e svuotare la coda? ***Richiede il voto di tutti***"
        )
        msg = await interaction.original_response()
        await musicController.stopAllSongs(message=msg)


@bot.tree.command(
    name="dc",
    description="Disconnette il bot dal canale vocale e disattiva la modalità 24/7.",
)
async def kick(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    await musicController.hardDisconnect()
    await bot.popGuildMusicController(guild=interaction.guild)
    await interaction.response.send_message(
        "👋 Bot disconnesso dal canale vocale e modalità 24/7 disattivata."
    )


@bot.tree.command(name="skip", description="Salta il brano attualmente in riproduzione.")
async def skip(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    if not musicController.getIsMajorityVote():
        await interaction.response.send_message("⏭️ Brano saltato.")
        await musicController.skipSong()
    else:
        await interaction.response.send_message("⏭️ Saltare il brano corrente?")
        msg = await interaction.original_response()
        await musicController.skipSong(message=msg)


@bot.tree.command(name="loop", description="Attiva/Disattiva la ripetizione del brano corrente.")
async def loop(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    response = await musicController.setLooping()
    if response:
        await interaction.response.send_message("🔁 Ripetizione (Loop) attivata.")
    else:
        await interaction.response.send_message("🔁 Ripetizione (Loop) disattivata.")


@bot.tree.command(
    name="autoplay",
    description="Attiva/Disattiva l'Autoplay (riproduce automaticamente brani simili da Spotify a fine coda).",
)
async def autoplay(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    enabled = await musicController.setAutoplay()
    if enabled:
        await interaction.response.send_message(
            "✨ **Autoplay attivato!** Quando la coda terminerà, riprodurrò automaticamente brani correlati da Spotify."
        )
    else:
        await interaction.response.send_message("✨ **Autoplay disattivato.**")


@bot.tree.command(
    name="replay",
    description="Riavvia il brano attualmente in riproduzione dall'inizio (0:00).",
)
async def replay(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    ok = await musicController.replaySong()
    if ok:
        await interaction.response.send_message("⏮️ Brano riavviato dall'inizio.")
    else:
        await interaction.response.send_message(
            "⚠️ Nessun brano attualmente in riproduzione.", ephemeral=True
        )


@bot.tree.command(name="queue", description="Mostra e gestisce la coda di riproduzione.")
async def queue(interaction: discord.Interaction):
    if await _check_banned(interaction):
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    song_queue = musicController.getSongQueue()
    if len(song_queue) <= 1:
        embed = discord.Embed(
            title="🎶 Coda di Riproduzione",
            description="Non ci sono altri brani in coda.",
            color=0xA600FF,
        )
        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    view = QueueView(song_queue, bot, interaction.user)
    await view.send_page(interaction, first_response=True)


@bot.tree.command(name="search", description="Cerca i primi 10 risultati su YouTube e scegli quale riprodurre.")
async def search(interaction: discord.Interaction, query: str):
    if await _check_banned(interaction):
        return

    await interaction.response.send_message(
        f"🔍 Ricerca su YouTube: **{query}**", delete_after=4
    )
    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    result = await musicController.searchSongs(query)
    if not result:
        return
    view = SearchView(result, musicController, bot)
    await view.send_page(interaction)


# =====================================================================
# COMANDI ADMIN DI GESTIONE UTENTI E SISTEMA
# =====================================================================

@bot.tree.command(
    name="majorvote",
    description="Admin - Attiva/Disattiva il voto di maggioranza per /skip e /stop.",
)
async def majorVote(interaction: discord.Interaction):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
        return

    musicController = await bot.getGuildMusicController(guild=interaction.guild)
    response = await musicController.setMajorityVote()
    if response:
        await interaction.response.send_message("🗳️ Voto di maggioranza **attivato**.")
    else:
        await interaction.response.send_message("🗳️ Voto di maggioranza **disattivato**.")


@bot.tree.command(name="ban", description="Admin - Banna un utente dall'utilizzo del bot.")
async def ban(interaction: discord.Interaction, user: discord.User):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
        return

    await BannedUsers().addBannedUserID(user.id)
    await interaction.response.send_message(f"🔨 **{user.display_name}** è stato bannato dal bot.")


@bot.tree.command(name="unban", description="Admin - Rimuove il ban a un utente.")
async def unban(interaction: discord.Interaction, user: discord.User):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
        return

    await BannedUsers().removeBannedUserID(user.id)
    await interaction.response.send_message(f"✅ Il ban di **{user.display_name}** è stato rimosso.")


@bot.tree.command(name="addadmin", description="Admin - Aggiunge un utente come amministratore del bot.")
async def addAdmin(interaction: discord.Interaction, user: discord.User):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
        return

    await VIPUsers().addVIPUserID(user.id)
    await interaction.response.send_message(
        f"👑 **{user.display_name}** è stato aggiunto come amministratore del bot."
    )


@bot.tree.command(name="removeadmin", description="Admin - Rimuove un utente dagli amministratori del bot.")
async def removeAdmin(interaction: discord.Interaction, user: discord.User):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
        return

    await VIPUsers().removeVIPUserID(user.id)
    await interaction.response.send_message(
        f"🗑️ **{user.display_name}** è stato rimosso dagli amministratori del bot."
    )


@bot.tree.command(name="sync", description="Admin - Sincronizza l'albero dei comandi slash.")
async def sync(interaction: discord.Interaction):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
        return

    await bot.tree.sync()
    await interaction.response.send_message("✅ Comandi sincronizzati.", delete_after=5)


@bot.tree.command(name="restart", description="Admin - Riavvia completamente il bot.")
async def restart(interaction: discord.Interaction):
    if not await VIPUsers().isAdmin(interaction):
        await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
        return

    await interaction.response.send_message("🔄 Riavvio del bot in corso...")
    bot.restart_bot()


@bot.tree.command(name="help", description="Mostra la lista completa dei comandi e delle funzioni vocali.")
async def help_cmd(interaction: discord.Interaction):
    kw_config = await BotKeywords().loadConfig()
    primary_kw = kw_config["primary"]
    aliases = kw_config["aliases"]
    alias_text = f" (alias: {', '.join(aliases)})" if aliases else ""

    embed = discord.Embed(
        title="🤖 Venus Bot — Assistente Vocale & Musica 24/7",
        description=(
            f"**Parola di attivazione attuale:** **`{primary_kw}`**{alias_text}\n"
            "Parla nel canale vocale pronunciando la parola di attivazione seguita da "
            "**qualsiasi domanda** (stile Google Assistant) o da un **comando musicale**!"
        ),
        color=0xA600FF,
    )
    embed.add_field(
        name="🗣️ Esempi Comandi Vocali & Domande",
        value=(
            f"• *\"{primary_kw.capitalize()}, metti [nome canzone / artista]\"*\n"
            f"• *\"{primary_kw.capitalize()}, metti una canzone rock italiana\"*\n"
            f"• *\"{primary_kw.capitalize()}, pausa / riprendi / salta / stop / ripeti / mescola\"*\n"
            f"• *\"{primary_kw.capitalize()}, alza il volume / volume a 50\"*\n"
            f"• *\"{primary_kw.capitalize()}, che canzone sta suonando?\"*\n"
            f"• *\"{primary_kw.capitalize()}, che ore sono? / raccontami una curiosità\"*"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎵 Comandi Musicali",
        value=(
            "`/play` • `/playfile` • `/search` • `/queue` • `/pause` • `/resume`\n"
            "`/skip` • `/stop` • `/replay` • `/loop` • `/autoplay` • `/shuffle` • `/volume` • `/time`\n"
            "`/247` • `/dc` • `/lofi` • `/lofijazz` • `/synthwave`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🧠 Assistente & Voce",
        value=(
            "`/setwakeword` *(Admin: cambia subito la parola di attivazione)*\n"
            "`/wakeword` *(Admin: gestisci parola principale e alias)*\n"
            "`/keywords` *(Pannello interattivo wake word)*\n"
            "`/voce` *(Admin: cambia la voce italiana dell'assistente)*\n"
            "`/chiedi` • `/transcribe` • `/counters`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Amministrazione",
        value="`/addadmin` • `/removeadmin` • `/ban` • `/unban` • `/majorvote` • `/sync` • `/restart`",
        inline=False,
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    asyncio.run(bot.start_bot())
