import discord
import math
from management.vip_users import VIPUsers


class QueueView(discord.ui.View):
    def __init__(self, queue, bot, author, timeout=120):
        super().__init__(timeout=timeout)
        self.queue = queue
        self.bot = bot
        self.author = author
        self.page = 0
        self.max_pages = max(1, math.ceil((len(self.queue) - 1) / 25))
        self.selected_song_index = None
        self.origin_interaction: discord.Interaction | None = None

    async def send_page(self, interaction: discord.Interaction, first_response=False):
        self.max_pages = max(1, math.ceil((len(self.queue) - 1) / 25))
        if self.page >= self.max_pages:
            self.page = max(0, self.max_pages - 1)

        if len(self.queue) <= 1:
            embed = discord.Embed(
                title="🎶 Coda di Riproduzione",
                description="Nessun brano in coda.",
                color=0xA600FF,
            )
            if self.bot.user:
                embed.set_thumbnail(url=self.bot.user.display_avatar.url)
            if first_response:
                self.origin_interaction = interaction
                await interaction.response.send_message(embed=embed, ephemeral=True)
            elif self.origin_interaction:
                try:
                    await self.origin_interaction.edit_original_response(embed=embed, view=None)
                except Exception:
                    pass
            return

        start = self.page * 25 + 1
        end = start + 25
        songs = self.queue[start:end]

        embed = discord.Embed(
            title=f"🎶 Coda di Riproduzione (Pagina {self.page + 1}/{self.max_pages})",
            color=0xA600FF,
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        if self.queue:
            embed.description = f"▶️ **In riproduzione:** {self.queue[0].title}"

        for i, song in enumerate(songs, start=start):
            embed.add_field(
                name=f"#{i}",
                value=f"{song.title} *(richiesto da {song.user.display_name})*",
                inline=False,
            )

        if first_response:
            self.origin_interaction = interaction
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
        elif self.origin_interaction:
            try:
                await self.origin_interaction.edit_original_response(embed=embed, view=self)
            except Exception:
                pass

    @discord.ui.button(label="Precedente", style=discord.ButtonStyle.secondary, emoji="⬅️", row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Usa `/queue` per aprire la tua vista personale della coda.", ephemeral=True
            )
            return
        if self.page > 0:
            self.page -= 1
            await interaction.response.defer()
            await self.send_page(interaction)
        else:
            await interaction.response.send_message("Sei già alla prima pagina.", ephemeral=True)

    @discord.ui.button(label="Rimuovi Brano", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_from_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Usa `/queue` per controllare la coda.", ephemeral=True
            )
            return

        start = self.page * 25 + 1
        end = start + 25
        options = [
            discord.SelectOption(label=f"{i}. {song.title}"[:100], value=str(i))
            for i, song in enumerate(self.queue[start:end], start=start)
        ]
        if not options:
            await interaction.response.send_message("Nessun brano da rimuovere.", ephemeral=True)
            return

        class RemoveDropdown(discord.ui.Select):
            def __init__(self, parent_view: "QueueView"):
                self.parent_view = parent_view
                super().__init__(placeholder="Seleziona un brano da rimuovere...", options=options)

            async def callback(self, select_interaction: discord.Interaction):
                index = int(self.values[0])
                if index >= len(self.parent_view.queue):
                    await select_interaction.response.edit_message(
                        content="⚠️ Brano non più presente in coda.", view=None
                    )
                    return
                song_user = self.parent_view.queue[index].user

                is_admin = await VIPUsers().isAdmin(select_interaction)
                if is_admin or select_interaction.user.id == song_user.id:
                    removed_song = self.parent_view.queue.pop(index)
                    await select_interaction.response.edit_message(
                        content=f"🗑️ Rimosso dalla coda: **{removed_song.title}**", view=None
                    )
                    await self.parent_view.send_page(select_interaction)
                else:
                    await select_interaction.response.send_message(
                        f"⛔ Non puoi rimuovere un brano aggiunto da **{song_user.display_name}**.",
                        ephemeral=True,
                    )

        view = discord.ui.View(timeout=60)
        view.add_item(RemoveDropdown(self))
        await interaction.response.send_message(
            "Scegli il brano da rimuovere:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Sposta Brano", style=discord.ButtonStyle.primary, emoji="↕️", row=0)
    async def move_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Usa `/queue` per controllare la coda.", ephemeral=True
            )
            return

        start = self.page * 25 + 1
        end = start + 25
        current_songs = self.queue[start:end]

        options = [
            discord.SelectOption(label=f"{i}. {song.title}"[:100], value=str(i))
            for i, song in enumerate(current_songs, start=start)
        ]
        if not options:
            await interaction.response.send_message("Nessun brano da spostare.", ephemeral=True)
            return

        class SelectSongToMove(discord.ui.Select):
            def __init__(self, parent_view: "QueueView"):
                self.parent_view = parent_view
                super().__init__(placeholder="Seleziona il brano da spostare...", options=options)

            async def callback(self, select_interaction: discord.Interaction):
                self.parent_view.selected_song_index = int(self.values[0])

                class MoveToModal(discord.ui.Modal, title="Sposta Brano in Posizione"):
                    def __init__(self, parent_view: "QueueView"):
                        super().__init__()
                        self.parent_view = parent_view
                        self.position_input = discord.ui.TextInput(
                            label="Nuova posizione in coda (da 1 in su):",
                            style=discord.TextStyle.short,
                            required=True,
                            max_length=3,
                        )
                        self.add_item(self.position_input)

                    async def on_submit(self, modal_interaction: discord.Interaction):
                        try:
                            new_index = int(self.position_input.value)
                            if new_index < 1 or new_index >= len(self.parent_view.queue):
                                raise ValueError(
                                    f"Posizione non valida (scegli tra 1 e {len(self.parent_view.queue) - 1})."
                                )
                            from_index = self.parent_view.selected_song_index
                            song_user = self.parent_view.queue[from_index].user

                            is_admin = await VIPUsers().isAdmin(modal_interaction)
                            if is_admin or modal_interaction.user.id == song_user.id:
                                song = self.parent_view.queue.pop(from_index)
                                self.parent_view.queue.insert(new_index, song)
                                await modal_interaction.response.send_message(
                                    f"✅ Brano **{song.title}** spostato in posizione #{new_index}.",
                                    ephemeral=True,
                                )
                                await self.parent_view.send_page(modal_interaction)
                            else:
                                await modal_interaction.response.send_message(
                                    f"⛔ Non puoi spostare un brano aggiunto da **{song_user.display_name}**.",
                                    ephemeral=True,
                                )
                        except Exception as e:
                            await modal_interaction.response.send_message(
                                f"❌ Errore: {e}", ephemeral=True
                            )

                await select_interaction.response.send_modal(MoveToModal(self.parent_view))

        view = discord.ui.View(timeout=60)
        view.add_item(SelectSongToMove(self))
        await interaction.response.send_message(
            "Seleziona il brano da spostare:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Successiva", style=discord.ButtonStyle.secondary, emoji="➡️", row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Usa `/queue` per aprire la tua vista della coda.", ephemeral=True
            )
            return
        if self.page < self.max_pages - 1:
            self.page += 1
            await interaction.response.defer()
            await self.send_page(interaction)
        else:
            await interaction.response.send_message("Sei già all'ultima pagina.", ephemeral=True)
