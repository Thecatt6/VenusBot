# 🎶 Venus Bot - A Voice Activated 24/7 Discord Music Bot

Venus Bot is a simple, yet powerful and customizable Discord music bot built with `discord.py`, capable of playing songs or playlists from YouTube, Spotify, SoundCloud, and direct file uploads. Featuring multi-user voice recognition, customizable voice commands, 24/7 connection, audio transcription, keyword counters, an advanced queue system, and much more — Venus Bot will change the way you use voice channels in Discord forever!

---

## ✨ Features

- 🎧 **Play Songs/Playlists** from YouTube, Spotify, SoundCloud, or uploaded files
- 📜 **Smart Queue System** with paging, reordering, shuffle, and prioritization
- 🔁 **Vote-Based Skipping** to democratically control what’s playing
- 🎶 **Search and Select** songs directly from Discord using dropdowns
- 🔍 **Dynamic Word Counters** with real-time tracking and keyword mapping
- 🛠️ **Custom Voice Commands** — define how the bot listens and responds
- 🧠 **Voice Recognition** to detect and attribute user commands in voice chat
- 🗣️ **Audio Transcription** — turn voice into text with AI-based recognition
- 🔐 **VIP/Admin System** for elevated permissions and command access
- 💾 **Keyword Counters** to keep track of words said by your group
- 🛠️ **Smart Connect** — allows the bot to join the channel when you join, or leaves when you leave
---

## 🚀 Getting Started

-Note: If all of the features of the bot are working besides actually playing music, just update your yt-dlp package.

> **Before You Start:**
>
> - ✅ You must have **[FFmpeg](https://ffmpeg.org/download.html)** installed and available in your system’s `PATH`
> - ✅ You must use **Python 3.11 or later** and create a virtual environment in the root directory
> - ✅ You need to configure the `.env` file before running the bot
> - ✅ You need to extract your cookies.txt and place it in the root folder.
> - ✅ For voice channels, install the requirements in a **new** virtual environment. Discord requires the DAVE end-to-end-encryption protocol for non-Stage voice channels; an older `discord.py` or `discord-ext-voice-recv` installation will be rejected (often with voice gateway code `4017`).

---

### 🔧 Step-by-Step Setup

```bash
# Clone the repository
git clone https://github.com/Thecatt6/VenusBot.git
cd VenusBot

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

The requirements pin `discord.py` 2.7.1 (which implements DAVE), its `davey` binding, and a reviewed temporary fork of the receive extension that decrypts incoming DAVE audio. The Python import remains `discord.ext.voice_recv`, so no command code needs to change.

You can verify the installed stack before starting the bot:

```bash
python -m discord --version
python -c "import davey; from discord.ext import voice_recv; print('DAVE voice stack ready')"
```
### 🧪 .env Setup
```
DISCORD_TOKEN='YOUR_BOT_TOKEN_HERE'
SPOTIFY_CLIENT_ID='YOUR_SPOTIFY_CLIENT_ID'
SPOTIFY_CLIENT_SECRET='YOUR_SPOTIFY_CLIENT_SECRET'
SPOTIFY_CLIENT_REFRESH_TOKEN='YOUR_SPOTIFY_REFRESH_TOKEN'
OWNER=000000000000000000
```
- Replace `YOUR_BOT_TOKEN_HERE` with your Discord Bot Token
- Replace Spotify credentials with ones from the **[Spotify Developer Dashboard](https://developer.spotify.com/dashboard)**
- OWNER should be your Discord user ID.
> - To get it:
> - Go to Discord Settings → Advanced → Enable Developer Mode
> - Right-click your username → Click "Copy User ID"

### 🤖 Run the Bot
```bash
# in a virtual environment
python venusbot.py
```

### 🐳 Run with Docker

Keep `.env` and `cookies.txt` beside `docker-compose.yml`; they are mounted at runtime and are not copied into the image. The supplied image includes FFmpeg and Deno, which yt-dlp uses to solve YouTube's JavaScript challenges.

```bash
docker compose up -d --build
docker compose logs -f venusbot
```

---

## 🗣️ Voice Related Commands and Info
- This bot is able to hear all of the users in a voice channel. So in order to play or control the music, you don't have to use the slash commands, and instead you can your voice as a command.
By default, "venus" is the keyword to activate this, and these are the available voice commands:
```
'venus play', 'venus skip', 'venus next', 'venus loop', 'venus pause', 'venus stop'
```
While "venus" is the default keyword to activate these commands, you can actually change this word to something else, or even to multiple keywords.
### /keywords
- This command will show all of the available keywords you can use as a voice command, from here you can also add or remove certain keywords.
### Example Usage:

![voiceplay](https://github.com/user-attachments/assets/1d329973-20cb-49b2-810d-21a483379741)

- Since this bot can listen to the users in a voice channel, I also added the ability for it to keep track of certain words being said.
### /counters
With this command, you will be able to see all of the keywords and word counters. You can also add or remove keywords/counters from this menu. 
- Counters are words that are being tracked.
- Keywords are words that will track towards a counter. You can have multiple keywords point to a counter.
### Example Usage:

![unreal](https://github.com/user-attachments/assets/cfa44704-85d5-4cdf-aad8-31039724c2c7)

### /transcribe
This command allows you to transcribe the audio from all users in a voice channel to a specified text channel. If no channel is specified, it will turn off transcribing.

![transcribe](https://github.com/user-attachments/assets/deb8b668-2875-4092-bd6e-7850f8905923)

### Smart Connect
- While the bot is in 24/7 mode, the bot will join the channel when you join, or it will leave when it is the last one left in the channel.
---

## ✨ Basic Command Usage and Examples
### /247
Connect the bot to a voice channel and set it to 24/7 mode. The bot will leave when it's the last one in the call, and it'll join when someone enters the call. all other commands that play music also activate 24/7 mode if the user is in a voice channel.

### /dc
Kicks the bot from the voice channel, and turns off 24/7.

### /play
Play a song/playlist from Youtube, Spotify, or SoundCloud. 
- Examples
```
/play Not Allowed by TV Girl 
```
```
/play https://www.youtube.com/watch?v=9wiEM0s4aCQ
```
```
/play https://open.spotify.com/album/6trNtQUgC8cgbWcqoMYkOR
```
```
/play https://soundcloud.com/uiceheidd/scared-of-love?in=jason-the_god/sets/juice-wrld-playlist&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing
```

### /playfile
Plays the given attachment.

### /lofi
Plays a 24/7 lofi radio.

### /lofijazz
Plays a 24/7 lofi jazz radio.

### /synthwave
Plays a 24/7 synthwave radio.

### /loop
Enables/Disables Looping on the current song.

### /time
Get the duration of the current song.

### /skip
Skips the current song.

### /stop
Stops playing the current song, and removes all songs from queue.

### /pause
Pauses the current song. If already paused, will resume the song.

### /resume
Resumes the current song from where it left off.

### /queue
Shows the queue of songs, can move or remove songs from queue here.

![queue](https://github.com/user-attachments/assets/34a33851-8bdc-486d-aca7-82a7883f1974)

### /shuffle
Shuffles the queue.

### /search
Searches for top 10 results of a song. Use this in case you can't find what you want.

---

## 🛠️ Management Commands - Admin Only
### /ban
Bans a user from using the bot.

### /unban
Unbans a user from the bot.

### /majorvote
Enables/Disables Majority Vote on Skipping or Stopping a Song.

### /restart
Restarts the bot.

### /addadmin
Makes the specified user an admin.

### /removeadmin
Removes admin from the specified user.

