import json
import os
from pathlib import Path
import logging
import aiofiles
from dotenv import load_dotenv


class VIPUsers:
    def __init__(self):
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        raw_owner = (os.getenv("OWNER") or "0").strip().strip("'\"")
        try:
            self.owner_id = int(raw_owner) if raw_owner else 0
        except ValueError:
            self.owner_id = 0

        root_dir = Path(__file__).resolve().parent
        self.VIP_USERS_FILE = root_dir / "vip_users_list.json"
        if not self.VIP_USERS_FILE.exists():
            self.VIP_USERS_FILE.write_text(json.dumps([]), encoding="utf-8")

    async def loadVIPUserIDs(self) -> list[int]:
        try:
            async with aiofiles.open(self.VIP_USERS_FILE, "r", encoding="utf-8") as f:
                content = await f.read()
                user_ids = json.loads(content) if content.strip() else []
        except Exception as e:
            logging.warning(f"Errore lettura vip_users_list.json: {e}")
            user_ids = []

        if self.owner_id and self.owner_id not in user_ids:
            user_ids.append(self.owner_id)

        return user_ids

    async def isAdmin(self, user_or_interaction) -> bool:
        """
        Verifica se l'utente è un Admin del bot:
        - ID presente in VIPUsers / OWNER nel .env
        - Oppure ha i permessi di Amministratore nel server Discord
        """
        vip_ids = await self.loadVIPUserIDs()

        # Se viene passata una discord.Interaction
        user = getattr(user_or_interaction, "user", user_or_interaction)
        user_id = getattr(user, "id", None)
        if user_id in vip_ids:
            return True

        # Controlla anche permessi di amministratore nel server Discord
        guild_perms = getattr(user, "guild_permissions", None)
        if guild_perms and getattr(guild_perms, "administrator", False):
            return True

        guild = getattr(user_or_interaction, "guild", None)
        if guild and getattr(guild, "owner_id", None) == user_id:
            return True

        return False

    async def saveVIPUserIDs(self, user_ids: list[int]):
        async with aiofiles.open(self.VIP_USERS_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(user_ids, indent=2))

    async def addVIPUserID(self, user_id: int):
        user_ids = await self.loadVIPUserIDs()
        if user_id not in user_ids:
            user_ids.append(user_id)
            await self.saveVIPUserIDs(user_ids)

    async def removeVIPUserID(self, user_id: int):
        user_ids = await self.loadVIPUserIDs()
        if user_id in user_ids:
            user_ids.remove(user_id)
            await self.saveVIPUserIDs(user_ids)
