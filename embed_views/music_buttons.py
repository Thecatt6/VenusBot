import discord


class MusicButtons(discord.ui.View):
    def __init__(self, client):
        super().__init__(timeout=None)
        self.client = client
        self.tree = client.tree

    @discord.ui.button(label="⏸", style=discord.ButtonStyle.secondary, row=0)
    async def PauseResume_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("pause").callback(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, row=0)
    async def Skip_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("skip").callback(interaction)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.secondary, row=0)
    async def Stop_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("stop").callback(interaction)

    @discord.ui.button(label="⟳", style=discord.ButtonStyle.secondary, row=0)
    async def Loop_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("loop").callback(interaction)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def Shuffle_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("shuffle").callback(interaction)

    @discord.ui.button(label="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def VolumeDown_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        mc = await self.client.getGuildMusicController(interaction.guild)
        new_vol = mc.setVolume(int(round(mc.volume * 100)) - 15)
        await interaction.response.send_message(
            f"🔉 Volume abbassato al **{new_vol}%**.", ephemeral=True, delete_after=4
        )

    @discord.ui.button(label="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def VolumeUp_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        mc = await self.client.getGuildMusicController(interaction.guild)
        new_vol = mc.setVolume(int(round(mc.volume * 100)) + 15)
        await interaction.response.send_message(
            f"🔊 Volume alzato al **{new_vol}%**.", ephemeral=True, delete_after=4
        )

    @discord.ui.button(label="⌛", style=discord.ButtonStyle.secondary, row=1)
    async def Time_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("time").callback(interaction)

    @discord.ui.button(label="☰", style=discord.ButtonStyle.secondary, row=1)
    async def Queue_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("queue").callback(interaction)

    @discord.ui.button(label="🛑", style=discord.ButtonStyle.danger, row=1)
    async def Kick_Button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.tree.get_command("dc").callback(interaction)
