import json
from pathlib import Path
import logging
import aiofiles


class BannedUsers:
    def __init__(self):
        root_dir = Path(__file__).resolve().parent
        self.BANNED_USERS_FILE = root_dir / "banned_users_list.json"
        if not self.BANNED_USERS_FILE.exists():
            self.BANNED_USERS_FILE.write_text(json.dumps([]), encoding="utf-8")

    async def loadBannedUserIDs(self) -> list[int]:
        try:
            async with aiofiles.open(self.BANNED_USERS_FILE, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content) if content.strip() else []
        except Exception as e:
            logging.warning(f"Errore lettura banned_users_list.json: {e}")
            return []

    async def saveBannedUserIDs(self, user_ids: list[int]):
        async with aiofiles.open(self.BANNED_USERS_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(user_ids, indent=2))

    async def addBannedUserID(self, user_id: int):
        user_ids = await self.loadBannedUserIDs()
        if user_id not in user_ids:
            user_ids.append(user_id)
            await self.saveBannedUserIDs(user_ids)

    async def removeBannedUserID(self, user_id: int):
        user_ids = await self.loadBannedUserIDs()
        if user_id in user_ids:
            user_ids.remove(user_id)
            await self.saveBannedUserIDs(user_ids)
