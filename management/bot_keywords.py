import difflib
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import aiofiles


def _strip_accents(text: str) -> str:
    """Rimuove gli accenti mantenendo le lettere base (es. 'è' -> 'e')."""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _italian_phonetic_key(word: str) -> str:
    """
    Normalizza una parola secondo la fonetica italiana comune dei motori STT.
    Utile quando parole come 'venus', 'jarvis', 'alexa' vengono trascritte
    con grafie italiane o inglesi leggermente diverse.
    """
    w = _strip_accents(word.lower().strip())
    w = re.sub(r"[^a-z0-9]", "", w)
    if not w:
        return ""

    # Sostituzioni fonetiche tipiche italiano/inglese nello speech-to-text
    replacements = [
        ("gi", "j"),
        ("ge", "je"),
        ("ch", "k"),
        ("ck", "k"),
        ("c", "k"),
        ("qu", "kw"),
        ("ph", "f"),
        ("w", "v"),
        ("y", "i"),
        ("ee", "i"),
        ("oo", "u"),
        ("sh", "sc"),
    ]
    for old, new in replacements:
        w = w.replace(old, new)

    # Riduci doppie consonanti (es. "vennus" -> "venus")
    w = re.sub(r"(.)\1+", r"\1", w)
    return w


# Prefissi colloquiali italiani che spesso precedono la wake word
ITALIAN_GREETING_PREFIXES = {
    "ehi",
    "hey",
    "ei",
    "ok",
    "okay",
    "okei",
    "oh",
    "o",
    "ciao",
    "scusa",
    "senti",
    "ascolta",
    "allora",
    "bella",
}


class BotKeywords:
    """
    Gestisce la parola di attivazione (wake word) principale e gli eventuali alias,
    con supporto a:
    - Impostazione rapida della parola di attivazione principale tramite comando admin
    - Alias aggiuntivi opzionali
    - Tolleranza fonetica e fuzzy matching per il riconoscimento vocale in italiano
    """

    def __init__(self):
        root_dir = Path(__file__).resolve().parent
        self.bot_keywords_file = root_dir / "bot_keywords.json"
        if not self.bot_keywords_file.exists():
            default_data = {
                "primary": "venus",
                "aliases": ["venere"],
                "fuzzy_threshold": 0.78,
            }
            self.bot_keywords_file.write_text(
                json.dumps(default_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _normalize_schema(self, raw_data) -> Dict:
        """Converte il vecchio formato lista ['venus'] nel nuovo schema strutturato."""
        if isinstance(raw_data, list):
            cleaned = [str(k).strip().lower() for k in raw_data if str(k).strip()]
            primary = cleaned[0] if cleaned else "venus"
            aliases = [k for k in cleaned[1:] if k != primary]
            return {
                "primary": primary,
                "aliases": aliases,
                "fuzzy_threshold": 0.78,
            }
        elif isinstance(raw_data, dict):
            primary = str(raw_data.get("primary") or "venus").strip().lower()
            aliases_raw = raw_data.get("aliases", [])
            aliases = []
            for a in aliases_raw:
                a_clean = str(a).strip().lower()
                if a_clean and a_clean != primary and a_clean not in aliases:
                    aliases.append(a_clean)
            threshold = float(raw_data.get("fuzzy_threshold", 0.78))
            return {
                "primary": primary,
                "aliases": aliases,
                "fuzzy_threshold": max(0.60, min(0.95, threshold)),
            }
        return {
            "primary": "venus",
            "aliases": ["venere"],
            "fuzzy_threshold": 0.78,
        }

    async def loadConfig(self) -> Dict:
        try:
            async with aiofiles.open(self.bot_keywords_file, "r", encoding="utf-8") as f:
                content = await f.read()
                raw = json.loads(content) if content.strip() else {}
        except Exception as e:
            logging.warning(f"Errore lettura bot_keywords.json, ripristino default: {e}")
            raw = {}
        config = self._normalize_schema(raw)
        return config

    async def saveConfig(self, config: Dict):
        normalized = self._normalize_schema(config)
        async with aiofiles.open(self.bot_keywords_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(normalized, indent=2, ensure_ascii=False))

    async def getPrimaryKeyword(self) -> str:
        config = await self.loadConfig()
        return config["primary"]

    async def setPrimaryKeyword(self, keyword: str, keep_old_as_alias: bool = False) -> str:
        """Imposta la parola di attivazione principale."""
        clean_kw = keyword.strip().lower()
        clean_kw = re.sub(r"[^\w\sàèéìòù]", "", clean_kw).strip()
        if not clean_kw:
            raise ValueError("La parola di attivazione non può essere vuota.")

        config = await self.loadConfig()
        old_primary = config["primary"]
        config["primary"] = clean_kw

        if clean_kw in config["aliases"]:
            config["aliases"].remove(clean_kw)

        if keep_old_as_alias and old_primary and old_primary != clean_kw:
            if old_primary not in config["aliases"]:
                config["aliases"].insert(0, old_primary)

        await self.saveConfig(config)
        return clean_kw

    async def getAliases(self) -> List[str]:
        config = await self.loadConfig()
        return config["aliases"]

    async def loadBotKeywords(self) -> List[str]:
        """Restituisce tutte le parole di attivazione attive: [primary, *aliases]."""
        config = await self.loadConfig()
        return [config["primary"]] + config["aliases"]

    async def saveBotKeywords(self, keywords: List[str]):
        config = self._normalize_schema(keywords)
        await self.saveConfig(config)

    async def addBotKeyword(self, keyword: str) -> str:
        clean_kw = keyword.strip().lower()
        clean_kw = re.sub(r"[^\w\sàèéìòù]", "", clean_kw).strip()
        if not clean_kw:
            raise ValueError("La parola non può essere vuota.")

        config = await self.loadConfig()
        if clean_kw != config["primary"] and clean_kw not in config["aliases"]:
            config["aliases"].append(clean_kw)
            await self.saveConfig(config)
        return clean_kw

    async def removeBotKeyword(self, keyword: str) -> bool:
        clean_kw = keyword.strip().lower()
        config = await self.loadConfig()
        if clean_kw in config["aliases"]:
            config["aliases"].remove(clean_kw)
            await self.saveConfig(config)
            return True
        if clean_kw == config["primary"] and config["aliases"]:
            # Promuove il primo alias a parola principale se si rimuove la principale
            config["primary"] = config["aliases"].pop(0)
            await self.saveConfig(config)
            return True
        return False

    async def resetKeywords(self, default_primary: str = "venus") -> Dict:
        config = {
            "primary": default_primary.strip().lower(),
            "aliases": ["venere"] if default_primary.strip().lower() == "venus" else [],
            "fuzzy_threshold": 0.78,
        }
        await self.saveConfig(config)
        return config

    async def extractCommandAfterWakeWord(self, text: str) -> Tuple[bool, Optional[str], str]:
        """
        Analizza il testo trascritto in italiano per verificare se contiene la parola di attivazione.
        Supporta:
        - Punteggiatura dopo la wake word (es. "Venus, che ore sono?")
        - Prefissi colloquiali ("Ehi Venus...", "Ok Venus...", "Ciao Venus...")
        - Wake word multi-parola (es. "ok google", "ehi assistente")
        - Tolleranza fonetica e fuzzy matching sulle prime parole della frase

        Restituisce:
            (attivato: bool, wake_word_rilevata: Optional[str], comando_residuo: str)
        """
        if not text or not text.strip():
            return False, None, ""

        # Estrai eventuale [EN_HINT: ...] per conservarlo nel comando residuo
        en_hint_suffix = ""
        en_hint_content = ""
        m_hint = re.search(r"(\s*\[EN_HINT:\s*(.+?)\])$", text.strip(), flags=re.IGNORECASE)
        if m_hint:
            en_hint_suffix = m_hint.group(1)
            en_hint_content = m_hint.group(2).strip()
            cleaned_text = text[: m_hint.start()].strip()
        else:
            cleaned_text = text.strip()

        config = await self.loadConfig()
        keywords = [config["primary"]] + config["aliases"]
        # Aggiungi varianti fonetiche tipiche di "venus" nei motori STT italiani/inglesi
        if "venus" in keywords:
            for v_alias in ("venere", "vinus", "venos", "benus", "fenus", "menus"):
                if v_alias not in keywords:
                    keywords.append(v_alias)
        threshold = config.get("fuzzy_threshold", 0.75)

        # 1. Controllo regex diretto (esatto o senza accenti) ovunque nella frase iniziale
        sorted_keywords = sorted(keywords, key=len, reverse=True)
        for kw in sorted_keywords:
            pattern = rf"^(?:(?:{'|'.join(ITALIAN_GREETING_PREFIXES)})[\s,.:;!?]+)*({re.escape(kw)})\b[\s,.:;!?]*(.*)$"
            m = re.match(pattern, cleaned_text, flags=re.IGNORECASE)
            if m:
                rem = m.group(2).strip()
                return True, kw, f"{rem}{en_hint_suffix}".strip() if rem else ""

            # Controllo anche se la wake word è preceduta da massimo 1-2 parole nella frase
            pattern_anywhere = rf"\b({re.escape(kw)})\b[\s,.:;!?]*(.*)$"
            m2 = re.search(pattern_anywhere, cleaned_text, flags=re.IGNORECASE)
            if m2 and m2.start() <= 24:
                rem = m2.group(2).strip()
                return True, kw, f"{rem}{en_hint_suffix}".strip() if rem else ""

        # 2. Controllo fonetico e fuzzy sulle prime parole della frase
        tokens = list(re.finditer(r"[\wàèéìòùÀÈÉÌÒÙ]+", cleaned_text))
        if not tokens:
            return False, None, ""

        start_idx = 0
        while (
            start_idx < len(tokens) - 1
            and _strip_accents(tokens[start_idx].group(0).lower()) in ITALIAN_GREETING_PREFIXES
        ):
            start_idx += 1

        candidate_starts = [start_idx] if start_idx == 0 else [0, start_idx]

        for s_idx in candidate_starts:
            for kw in sorted_keywords:
                kw_words = kw.split()
                num_words = len(kw_words)
                if s_idx + num_words > len(tokens):
                    continue

                span_tokens = tokens[s_idx : s_idx + num_words]
                spoken_phrase = " ".join(t.group(0).lower() for t in span_tokens)

                norm_spoken = _strip_accents(spoken_phrase)
                norm_kw = _strip_accents(kw)
                ratio_direct = difflib.SequenceMatcher(None, norm_spoken, norm_kw).ratio()

                phon_spoken = " ".join(_italian_phonetic_key(t.group(0)) for t in span_tokens)
                phon_kw = " ".join(_italian_phonetic_key(w) for w in kw_words)
                ratio_phonetic = (
                    difflib.SequenceMatcher(None, phon_spoken, phon_kw).ratio()
                    if phon_spoken and phon_kw
                    else 0.0
                )

                best_ratio = max(ratio_direct, ratio_phonetic)
                effective_threshold = max(threshold, 0.88) if len(norm_kw) <= 3 else threshold

                if best_ratio >= effective_threshold:
                    end_char_pos = span_tokens[-1].end()
                    remainder = cleaned_text[end_char_pos:]
                    remainder = re.sub(r"^[\s,.:;!?-]+", "", remainder).strip()
                    logging.debug(
                        f"Wake word '{kw}' rilevata tramite match fonetico/fuzzy "
                        f"('{spoken_phrase}', score={best_ratio:.2f})"
                    )
                    return True, kw, f"{remainder}{en_hint_suffix}".strip() if remainder else ""

        # 3. Se la prima ipotesi non conteneva la wake word ma una delle ipotesi alternative in EN_HINT sì, usala!
        if en_hint_content:
            for alt_candidate in en_hint_content.split("|"):
                alt_c = alt_candidate.strip()
                if not alt_c:
                    continue
                for kw in sorted_keywords:
                    m_alt = re.match(
                        rf"^(?:(?:{'|'.join(ITALIAN_GREETING_PREFIXES)})[\s,.:;!?]+)*({re.escape(kw)})\b[\s,.:;!?]*(.*)$",
                        alt_c,
                        flags=re.IGNORECASE,
                    )
                    if m_alt:
                        rem = m_alt.group(2).strip()
                        return True, kw, f"{rem}{en_hint_suffix}".strip() if rem else ""

        return False, None, ""
