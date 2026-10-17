import discord


class SearchView(discord.ui.View):
    def __init__(self, songs, musicController, bot):
        super().__init__(timeout=120)
        self.songs = songs[:10]
        self.musicController = musicController
        self.bot = bot

    async def send_page(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔍 Risultati della Ricerca:",
            color=0xA600FF,
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        for i, song in enumerate(self.songs, start=1):
            embed.add_field(name=f"{i}.", value=song["title"][:256], inline=False)

        class SongDropdown(discord.ui.Select):
            def __init__(self, parent_view: "SearchView"):
                self.parent_view = parent_view
                options = [
                    discord.SelectOption(
                        label=f"{i + 1}. {s['title']}"[:100],
                        value=str(i),
                    )
                    for i, s in enumerate(parent_view.songs)
                ]
                super().__init__(
                    placeholder="Seleziona un brano da riprodurre/mettere in coda...",
                    options=options,
                )

            async def callback(self, select_interaction: discord.Interaction):
                index = int(self.values[0])
                song = self.parent_view.songs[index]

                if not self.parent_view.musicController.isConnectedToVC():
                    if not select_interaction.user.voice or not select_interaction.user.voice.channel:
                        await select_interaction.response.send_message(
                            "⚠️ Devi trovarti in un canale vocale per riprodurre un brano.",
                            ephemeral=True,
                        )
                        return
                    else:
                        await self.parent_view.musicController.two_four_seven(
                            select_interaction.user.voice.channel,
                            select_interaction.channel,
                        )

                await select_interaction.response.send_message(
                    f"🎶 Aggiungo **{song['title']}**...", delete_after=5
                )
                await self.parent_view.musicController.determineSongSource(
                    select_interaction.user, song["link"]
                )

        dropdown_view = discord.ui.View(timeout=120)
        dropdown_view.add_item(SongDropdown(self))

        await interaction.channel.send(embed=embed, view=dropdown_view)
