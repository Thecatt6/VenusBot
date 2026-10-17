import json
from pathlib import Path
import logging
import aiofiles


class WordCounter:
    def __init__(self):
        root_dir = Path(__file__).resolve().parent
        self.word_counters_file = root_dir / "word_counters.json"

        if not self.word_counters_file.exists():
            default_data = [
                {"unreal": "unreal", "unruh": "unreal"},
                {"unreal": 0},
            ]
            self.word_counters_file.write_text(
                json.dumps(default_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    async def loadWordCounters(self):
        try:
            async with aiofiles.open(self.word_counters_file, "r", encoding="utf-8") as f:
                content = await f.read()
                data = json.loads(content) if content.strip() else [{}, {}]
                if not isinstance(data, list) or len(data) < 2:
                    return [{}, {}]
                return data
        except Exception as e:
            logging.warning(f"Errore lettura word_counters.json: {e}")
            return [{}, {}]

    async def saveWordCounters(self, data):
        async with aiofiles.open(self.word_counters_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, indent=2, ensure_ascii=False))

    async def addKeyword(self, keyword: str, counter_name: str):
        kw = keyword.strip().lower()
        cn = counter_name.strip().lower()
        if not kw or not cn:
            return
        data = await self.loadWordCounters()
        mapping = data[0]
        mapping[kw] = cn
        if cn not in data[1]:
            data[1][cn] = 0
        await self.saveWordCounters(data)

    async def removeKeyword(self, keyword: str):
        kw = keyword.strip().lower()
        data = await self.loadWordCounters()
        mapping = data[0]
        if kw in mapping:
            del mapping[kw]
            await self.saveWordCounters(data)

    async def removeCounter(self, counter_name: str):
        cn = counter_name.strip().lower()
        data = await self.loadWordCounters()
        mapping = data[0]
        counters = data[1]
        mapping = {k: v for k, v in mapping.items() if v.lower() != cn}
        counters = {k: v for k, v in counters.items() if k.lower() != cn}
        await self.saveWordCounters([mapping, counters])

    async def incrementCounterForKeyword(self, keyword: str):
        kw = keyword.strip().lower()
        data = await self.loadWordCounters()
        mapping = data[0]
        counters = data[1]

        if kw in mapping:
            counter_name = mapping[kw]
            counters[counter_name] = counters.get(counter_name, 0) + 1
            await self.saveWordCounters(data)

    async def getCount(self, counter_name: str) -> int:
        data = await self.loadWordCounters()
        return data[1].get(counter_name, 0)

    async def getAllCounts(self):
        data = await self.loadWordCounters()
        return data[1]
