from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CASINO_RULES_VERSION = "telegram_slots_base4_v1"
CASINO_STAKE = 10
CASINO_SYMBOLS = ("bar", "grape", "lemon", "seven")

CasinoCategory = Literal["none", "pair", "triple", "jackpot"]


@dataclass(frozen=True)
class SlotResult:
    value: int
    symbols: tuple[str, str, str]
    category: CasinoCategory
    payout: int


def slot_result(value: int, *, rules_version: str = CASINO_RULES_VERSION) -> SlotResult:
    """Decode a Telegram slot Dice value under the fixed v1 community mapping."""
    if rules_version != CASINO_RULES_VERSION:
        raise ValueError("unsupported casino rules version")
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
    payout = {"none": 0, "pair": 5, "triple": 50, "jackpot": 250}[category]
    return SlotResult(value=value, symbols=symbols, category=category, payout=payout)
