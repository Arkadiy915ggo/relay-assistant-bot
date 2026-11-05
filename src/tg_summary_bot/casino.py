from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


CASINO_RULES_VERSION = "telegram_slots_base4_v1"
CASINO_STAKE = 10
CASINO_VARIABLE_RULES_VERSION = "telegram_slots_base4_stakes_v2"
SQLITE_MAX_INTEGER = 2**63 - 1
CASINO_MAX_STAKE = SQLITE_MAX_INTEGER // 25
CasinoStake = int | Literal["all"]
CASINO_OBJECT_RE = re.compile(r"\b(?:казино[^\W\d_]*|казик[^\W\d_]*|слот[^\W\d_]*)(?=\W|[0-9]|$)", re.IGNORECASE)
CASINO_SYMBOLS = ("bar", "grape", "lemon", "seven")

CasinoCategory = Literal["none", "pair", "triple", "jackpot"]
CasinoTriggerKind = Literal["user_request", "bot_request", "bot_automatic"]


@dataclass(frozen=True)
class CasinoTrigger:
    kind: CasinoTriggerKind
    request_message_id: int | None
    domain_id: int

    @property
    def key(self) -> str:
        if self.kind == "user_request":
            return f"user-message:{self.domain_id}"
        if self.kind == "bot_request":
            return f"bot-request:{self.domain_id}"
        return f"joke-job:{self.domain_id}"


def user_casino_trigger(message_id: int) -> CasinoTrigger:
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("request message id must be positive")
    return CasinoTrigger("user_request", message_id, message_id)


def bot_request_casino_trigger(message_id: int) -> CasinoTrigger:
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("request message id must be positive")
    return CasinoTrigger("bot_request", message_id, message_id)


def automatic_casino_trigger(job_id: int) -> CasinoTrigger:
    if type(job_id) is not int or job_id <= 0:
        raise ValueError("job id must be positive")
    return CasinoTrigger("bot_automatic", None, job_id)


@dataclass(frozen=True)
class SlotResult:
    value: int
    symbols: tuple[str, str, str]
    category: CasinoCategory
    payout: int


def validate_casino_stake(stake: int) -> None:
    if type(stake) is not int or not 1 <= stake <= CASINO_MAX_STAKE:
        raise ValueError("stake must be a positive bounded integer")


def parse_casino_stake(text: str, *, addressed: bool = False) -> CasinoStake:
    """Parse only literal user input; model output cannot select a stake."""
    text = text.lower().replace("ё", "е").strip()
    if addressed:
        match = CASINO_OBJECT_RE.search(text)
        if not match:
            raise ValueError("missing casino object")
        if re.search(r"\d|\b(?:все|all|на)\b", text[:match.start()]):
            raise ValueError("stake must follow casino object")
        text = text[match.end():]
        if text and text[0].isdigit():
            raise ValueError("stake must be separated from casino object")
        text = re.sub(r"\b(?:себе|сам|сама|пожалуйста)\b", "", text)
    # Leading decimal separators are part of malformed amounts, not decoration.
    text = text.strip().rstrip(".!?:;").strip()
    if not text:
        return CASINO_STAKE
    if re.fullmatch(r"(?:на\s+)?(?:все(?:\s+(?:бабки|очки|деньги|баланс))?|весь\s+баланс|all(?:\s+in)?)", text):
        return "all"
    match = re.fullmatch(r"(?:на\s+)?([0-9]{1,18})(?:\s+(?:очк(?:о|а|ов|и)|балл(?:а|ов)?))?", text)
    if not match:
        raise ValueError("invalid casino stake")
    stake = int(match.group(1))
    validate_casino_stake(stake)
    return stake


def slot_result(
    value: int, *, rules_version: str = CASINO_RULES_VERSION, stake: int = CASINO_STAKE,
) -> SlotResult:
    """Decode a Telegram slot Dice value using the saved rules version and stake."""
    if rules_version not in {CASINO_RULES_VERSION, CASINO_VARIABLE_RULES_VERSION}:
        raise ValueError("unsupported casino rules version")
    validate_casino_stake(stake)
    if rules_version == CASINO_RULES_VERSION and stake != CASINO_STAKE:
        raise ValueError("legacy rules require the original stake")
    if type(value) is not int or not 1 <= value <= 64:
        raise ValueError("dice value must be an int from 1 through 64")
    number = value - 1
    symbols = (
        CASINO_SYMBOLS[number % 4],
        CASINO_SYMBOLS[(number // 4) % 4],
        CASINO_SYMBOLS[(number // 16) % 4],
    )
    if symbols == ("seven", "seven", "seven"):
        category: CasinoCategory = "jackpot"
    elif len(set(symbols)) == 1:
        category = "triple"
    elif len(set(symbols)) == 2:
        category = "pair"
    else:
        category = "none"
    payout = {"none": 0, "pair": stake // 2, "triple": stake * 5, "jackpot": stake * 25}[category]
    return SlotResult(value=value, symbols=symbols, category=category, payout=payout)
