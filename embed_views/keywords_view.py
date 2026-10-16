import discord
from management.vip_users import VIPUsers
from management.bot_keywords import BotKeywords


class KeywordsView(discord.ui.View):
    def __init__(self, bot, timeout=120):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.origin_interaction: discord.Interaction | None = None

    async def build_embed(self) -> discord.Embed:
        kw_manager = BotKeywords()
        config = await kw_manager.loadConfig()
        primary = config["primary"]
        aliases = config["aliases"]

        embed = discord.Embed(
            title="🎙️ Parola di Attivazione (Wake Word) & Alias",
            description=(
                "Pronuncia la parola di attivazione nel canale vocale seguita da un comando "
                "musicale o da qualsiasi domanda (stile Google Assistant)."
            ),
            color=0xA600FF,
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        embed.add_field(
            name="⭐ Parola di Attivazione Principale",
            value=f"**`{primary}`**",
            inline=False,
        )

        alias_text = (
            ", ".join(f"`{a}`" for a in aliases)
            if aliases
            else "*Nessun alias secondario configurato*"
        )
        embed.add_field(
            name="🔄 Alias Aggiuntivi",
            value=alias_text,
            inline=False,
        )

        embed.add_field(
            name="💡 Esempi di utilizzo a voce",
            value=(
                f"• *\"{primary.capitalize()}, metti una canzone di Vasco Rossi\"*\n"
                f"• *\"{primary.capitalize()}, salta questa canzone\"*\n"
                f"• *\"{primary.capitalize()}, che ore sono?\"*\n"
                f"• *\"{primary.capitalize()}, spiegami perché il cielo è blu\"*"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Usa i pulsanti qui sotto o il comando /wakeword (Solo Admin) per modificare."
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

    @discord.ui.button(label="Imposta Principale", style=discord.ButtonStyle.success, emoji="⭐", row=0)
    async def set_primary_keyword(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await VIPUsers().isAdmin(interaction):
            await interaction.response.send_message(
                "⛔ Solo gli amministratori possono modificare la parola di attivazione.",
                ephemeral=True,
            )
            return

        class SetPrimaryModal(discord.ui.Modal, title="Imposta Parola di Attivazione"):
            def __init__(self, parent_view: "KeywordsView"):
                super().__init__()
                self.parent_view = parent_view
                self.keyword_input = discord.ui.TextInput(
                    label="Nuova parola di attivazione principale:",
                    placeholder="es. venus, jarvis, assistente, computer...",
                    style=discord.TextStyle.short,
                    required=True,
                    max_length=32,
                )
                self.add_item(self.keyword_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    new_kw = await BotKeywords().setPrimaryKeyword(self.keyword_input.value)
                    await modal_interaction.response.send_message(
                        f"✅ Parola di attivazione principale impostata su: **`{new_kw}`**",
                        ephemeral=True,
                    )
                    await self.parent_view.send_page(modal_interaction, first_response=False)
                except Exception as e:
                    await modal_interaction.response.send_message(f"❌ Errore: {e}", ephemeral=True)

        await interaction.response.send_modal(SetPrimaryModal(self))

    @discord.ui.button(label="Aggiungi Alias", style=discord.ButtonStyle.primary, emoji="➕", row=0)
    async def add_to_keywords(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await VIPUsers().isAdmin(interaction):
            await interaction.response.send_message(
                "⛔ Solo gli amministratori possono aggiungere alias.",
                ephemeral=True,
            )
            return

        class AddAliasModal(discord.ui.Modal, title="Aggiungi Alias di Attivazione"):
            def __init__(self, parent_view: "KeywordsView"):
                super().__init__()
                self.parent_view = parent_view
                self.keyword_input = discord.ui.TextInput(
                    label="Nuovo alias di attivazione:",
                    placeholder="es. venere, ehi venus...",
                    style=discord.TextStyle.short,
                    required=True,
                    max_length=32,
                )
                self.add_item(self.keyword_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    added = await BotKeywords().addBotKeyword(self.keyword_input.value)
                    await modal_interaction.response.send_message(
                        f"✅ Aggiunto alias di attivazione: **`{added}`**",
                        ephemeral=True,
                    )
                    await self.parent_view.send_page(modal_interaction, first_response=False)
                except Exception as e:
                    await modal_interaction.response.send_message(f"❌ Errore: {e}", ephemeral=True)

        await interaction.response.send_modal(AddAliasModal(self))

    @discord.ui.button(label="Rimuovi Alias", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def remove_from_keywords(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await VIPUsers().isAdmin(interaction):
            await interaction.response.send_message(
                "⛔ Solo gli amministratori possono rimuovere alias.",
                ephemeral=True,
            )
            return

        aliases = await BotKeywords().getAliases()
        if not aliases:
            await interaction.response.send_message(
                "ℹ️ Non ci sono alias secondari da rimuovere.",
                ephemeral=True,
            )
            return

        options = [discord.SelectOption(label=alias[:100], value=alias[:100]) for alias in aliases[:25]]

        class RemoveDropdown(discord.ui.Select):
            def __init__(self, parent_view: "KeywordsView"):
                self.parent_view = parent_view
                super().__init__(placeholder="Seleziona un alias da rimuovere...", options=options)

            async def callback(self, select_interaction: discord.Interaction):
                removed_keyword = self.values[0]
                await BotKeywords().removeBotKeyword(removed_keyword)
                await select_interaction.response.edit_message(
                    content=f"🗑️ Alias rimosso: **`{removed_keyword}`**",
                    view=None,
                )
                await self.parent_view.send_page(select_interaction, first_response=False)

        view = discord.ui.View(timeout=60)
        view.add_item(RemoveDropdown(self))
        await interaction.response.send_message(
            "Seleziona l'alias da rimuovere:", view=view, ephemeral=True
        )
