import discord
from management.vip_users import VIPUsers
from management.word_counter import WordCounter


class CountersView(discord.ui.View):
    def __init__(self, bot, timeout=120):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.origin_interaction: discord.Interaction | None = None
        self.counters = None

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔢 Contatori Parole (Word Counters)",
            color=0xA600FF,
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        self.counters = await WordCounter().loadWordCounters()

        if len(self.counters[1]) <= 0:
            embed.description = "Non ci sono contatori attivi. Aggiungine uno con il pulsante qui sotto."
        else:
            for counter_name, count in self.counters[1].items():
                keywords = [f"• `{k}`" for k, v in self.counters[0].items() if v == counter_name]
                embed.add_field(
                    name=f"{counter_name.capitalize()}: {count}",
                    value="\n".join(keywords) if keywords else "Nessuna parola associata.",
                    inline=False,
                )
        return embed

    async def send_page(self, interaction: discord.Interaction, first_response=False):
        embed = await self.build_embed()
        if first_response:
            self.origin_interaction = interaction
            await interaction.response.send_message(embed=embed, view=self, ephemeral=False)
        else:
            target = self.origin_interaction or interaction
            try:
                await target.edit_original_response(embed=embed, view=self)
            except Exception:
                pass

    @discord.ui.button(label="Aggiungi Parola", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_keyword(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await VIPUsers().isAdmin(interaction):
            await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
            return

        class KeywordModal(discord.ui.Modal, title="Aggiungi Parola e Contatore"):
            def __init__(self, parent_view: "CountersView"):
                super().__init__()
                self.parent_view = parent_view

                self.keyword_input = discord.ui.TextInput(
                    label="Parola da rilevare a voce:",
                    placeholder="es. assurdo",
                    style=discord.TextStyle.short,
                    required=True,
                )
                self.counter_input = discord.ui.TextInput(
                    label="Nome del Contatore:",
                    placeholder="es. assurdo",
                    style=discord.TextStyle.short,
                    required=True,
                )

                self.add_item(self.keyword_input)
                self.add_item(self.counter_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                keyword = self.keyword_input.value.strip().lower()
                counter = self.counter_input.value.strip().lower()

                if not keyword or not counter:
                    await modal_interaction.response.send_message(
                        "❌ Parola e contatore non possono essere vuoti.", ephemeral=True
                    )
                    return

                await WordCounter().addKeyword(keyword, counter)
                await modal_interaction.response.send_message(
                    f"✅ Aggiunta parola `{keyword}` al contatore `{counter}`.", ephemeral=True
                )
                await self.parent_view.send_page(modal_interaction, first_response=False)

        await interaction.response.send_modal(KeywordModal(self))

    @discord.ui.button(label="Rimuovi Parola", style=discord.ButtonStyle.secondary, emoji="✂️", row=0)
    async def remove_keyword(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await VIPUsers().isAdmin(interaction):
            await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
            return

        self.counters = await WordCounter().loadWordCounters()
        if len(self.counters[1]) <= 0:
            await interaction.response.send_message(
                "ℹ️ Nessun contatore disponibile.", ephemeral=True
            )
            return

        options = [
            discord.SelectOption(label=counter[:100], value=counter[:100])
            for counter in list(self.counters[1].keys())[:25]
        ]

        class SelectCounterDropdown(discord.ui.Select):
            def __init__(self, parent_view: "CountersView"):
                self.parent_view = parent_view
                super().__init__(placeholder="Seleziona un Contatore...", options=options)

            async def callback(self, select_interaction: discord.Interaction):
                counter_name = self.values[0]
                data = await WordCounter().loadWordCounters()
                keywords = [k for k, v in data[0].items() if v == counter_name]

                if not keywords:
                    await select_interaction.response.edit_message(
                        content=f"ℹ️ Nessuna parola associata a `{counter_name}`.",
                        view=None,
                    )
                    return

                keyword_options = [
                    discord.SelectOption(label=kw[:100], value=kw[:100]) for kw in keywords[:25]
                ]

                class RemoveKeywordDropdown(discord.ui.Select):
                    def __init__(self, parent_view: "CountersView"):
                        self.parent_view = parent_view
                        super().__init__(
                            placeholder="Seleziona la parola da rimuovere...",
                            options=keyword_options,
                        )

                    async def callback(self, keyword_interaction: discord.Interaction):
                        keyword_to_remove = self.values[0]
                        await WordCounter().removeKeyword(keyword_to_remove)
                        await keyword_interaction.response.edit_message(
                            content=f"🗑️ Rimossa parola `{keyword_to_remove}` da `{counter_name}`.",
                            view=None,
                        )
                        await self.parent_view.send_page(keyword_interaction, first_response=False)

                keyword_view = discord.ui.View(timeout=60)
                keyword_view.add_item(RemoveKeywordDropdown(self.parent_view))
                await select_interaction.response.edit_message(
                    content=f"Seleziona la parola da rimuovere da `{counter_name}`:",
                    view=keyword_view,
                )

        view = discord.ui.View(timeout=60)
        view.add_item(SelectCounterDropdown(self))
        await interaction.response.send_message(
            "Seleziona il contatore da cui rimuovere una parola:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Elimina Contatore", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await VIPUsers().isAdmin(interaction):
            await interaction.response.send_message("⛔ Comando riservato agli Admin.", ephemeral=True)
            return

        self.counters = await WordCounter().loadWordCounters()
        if len(self.counters[1]) <= 0:
            await interaction.response.send_message(
                "ℹ️ Nessun contatore da rimuovere.", ephemeral=True
            )
            return

        options = [
            discord.SelectOption(label=counter[:100], value=counter[:100])
            for counter in list(self.counters[1].keys())[:25]
        ]

        class RemoveCounterDropdown(discord.ui.Select):
            def __init__(self, parent_view: "CountersView"):
                self.parent_view = parent_view
                super().__init__(placeholder="Seleziona il Contatore da eliminare...", options=options)

            async def callback(self, select_interaction: discord.Interaction):
                counter_name = self.values[0]
                await WordCounter().removeCounter(counter_name)
                await select_interaction.response.edit_message(
                    content=f"🗑️ Eliminato contatore: `{counter_name}`",
                    view=None,
                )
                await self.parent_view.send_page(select_interaction, first_response=False)

        view = discord.ui.View(timeout=60)
        view.add_item(RemoveCounterDropdown(self))
        await interaction.response.send_message(
            "Scegli il contatore da eliminare:", view=view, ephemeral=True
        )
