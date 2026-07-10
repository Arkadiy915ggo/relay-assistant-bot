from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tg_summary_bot.config import Settings
from tg_summary_bot.observability import opik_track, update_opik_llm_usage, update_opik_span_metadata


MEME_SYSTEM_PROMPT = """
Ты генератор мемов для дружеского Telegram-чата.
Смотри на изображение и придумай короткую смешную подпись по-русски.
Юмор должен быть бытовым, ироничным, без травли, хейта, политики, сексуального контента и оскорблений.
Не упоминай людей на фото как реальные личности, если это не очевидный публичный персонаж.
Ответь строго JSON.
""".strip()


MEME_USER_PROMPT = """
Придумай мем для этой картинки.

Верни строго JSON:
{
  "top_text": "короткая верхняя строка или пустая строка",
  "bottom_text": "короткая нижняя строка",
  "alt_text": "одно предложение, почему это смешно"
}

Ограничения:
- top_text и bottom_text максимум по 45 символов;
- можно использовать только русский язык;
- если это мем-шаблон с пустыми белыми полями, обязательно заполни и top_text, и bottom_text;
- если картинка не подходит для мема, сделай мягкую универсальную шутку;
- не добавляй markdown.
""".strip()


DEFAULT_MEME_FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MAX_RENDER_WIDTH = 1280
MAX_TEXT_CHARS = 45


@dataclass(frozen=True)
class MemeCaption:
    top_text: str
    bottom_text: str
    alt_text: str = ""


class MemeGenerator:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.ollama_base_url
        self.model = settings.meme_model or settings.image_recognition_model
        self.timeout_seconds = settings.ollama_timeout_seconds
        self.keep_alive = settings.ollama_keep_alive
        self.unload_after_task = settings.ollama_unload_after_task
        self.num_ctx = settings.image_recognition_num_ctx
        self.num_predict = settings.meme_num_predict
        self.font_path = settings.meme_font_path

    @opik_track(name="meme.generate_caption")
    async def generate_caption(self, image_path: Path) -> MemeCaption:
        image_size = image_path.stat().st_size if image_path.exists() else 0
        update_opik_span_metadata(
            {
                "model": self.model,
                "image_size_bytes": image_size,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            }
        )
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = await self._complete_with_image(image_b64=image_b64, image_size=image_size)
        return parse_meme_caption(response)

    @opik_track(name="meme.vision", type="llm")
    async def _complete_with_image(self, *, image_b64: str, image_size: int) -> str:
        update_opik_span_metadata(
            {
                "provider": "ollama",
                "model": self.model,
                "image_count": 1,
                "image_size_bytes": image_size,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            }
        )
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "think": False,
                    "keep_alive": self.keep_alive,
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": MEME_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": MEME_USER_PROMPT,
                            "images": [image_b64],
                        },
                    ],
                    "options": {
                        "temperature": 0.8,
                        "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict,
                    },
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.text.strip()[:1000] or str(exc)
                raise RuntimeError(f"Ollama meme vision API error: {detail}") from exc

        data = response.json()
        total_duration = (data.get("total_duration") or 0) / 1_000_000_000
        prompt_eval_count = data.get("prompt_eval_count") or 0
        eval_count = data.get("eval_count") or 0
        eval_duration = (data.get("eval_duration") or 0) / 1_000_000_000
        tokens_per_second = eval_count / eval_duration if eval_duration else 0
        content = str(data.get("message", {}).get("content", "")).strip()
        update_opik_llm_usage(
            provider="ollama",
            model=self.model,
            usage={
                "prompt_tokens": int(prompt_eval_count),
                "completion_tokens": int(eval_count),
                "total_tokens": int(prompt_eval_count) + int(eval_count),
            },
            metadata={
                "image_count": 1,
                "image_size_bytes": image_size,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
                "total_duration_s": round(total_duration, 3),
                "eval_duration_s": round(eval_duration, 3),
                "eval_tokens_per_second": round(tokens_per_second, 3),
                "output_chars": len(content),
            },
        )
        logging.info(
            "Ollama meme response model=%s total_s=%.1f prompt_tokens=%s "
            "eval_tokens=%s eval_tps=%.2f",
            self.model,
            total_duration,
            prompt_eval_count,
            eval_count,
            tokens_per_second,
        )
        return content

    def render_meme(self, image_path: Path, caption: MemeCaption, output_path: Path) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise RuntimeError("Pillow is not installed. Run `./run.sh install`.") from exc

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(image_path) as source:
            image = source.convert("RGB")
            if image.width > MAX_RENDER_WIDTH:
                ratio = MAX_RENDER_WIDTH / image.width
                image = image.resize((MAX_RENDER_WIDTH, int(image.height * ratio)), Image.Resampling.LANCZOS)

            draw = ImageDraw.Draw(image)
            max_text_width = int(image.width * 0.92)
            margin = max(12, int(image.width * 0.035))
            stroke_width = max(2, int(image.width / 220))
            font_size = max(22, int(image.width / 11))
            font = self._load_font(font_size)

            top_lines, top_font = self._fit_text(caption.top_text, max_text_width, font_size, draw)
            bottom_lines, bottom_font = self._fit_text(
                caption.bottom_text,
                max_text_width,
                font_size,
                draw,
            )
            if top_lines:
                self._draw_centered_lines(
                    draw,
                    top_lines,
                    top_font,
                    image.width,
                    margin,
                    stroke_width,
                )
            if bottom_lines:
                block_height = self._lines_height(draw, bottom_lines, bottom_font, stroke_width)
                self._draw_centered_lines(
                    draw,
                    bottom_lines,
                    bottom_font,
                    image.width,
                    image.height - margin - block_height,
                    stroke_width,
                )
            image.save(output_path, format="JPEG", quality=92, optimize=True)

    def _fit_text(
        self,
        text: str,
        max_width: int,
        start_font_size: int,
        draw: ImageDraw.ImageDraw,
    ) -> tuple[list[str], Any]:
        normalized = normalize_caption_text(text)
        if not normalized:
            return [], self._load_font(start_font_size)
        font_size = start_font_size
        while font_size >= 18:
            font = self._load_font(font_size)
            lines = wrap_text(normalized, draw, font, max_width)
            if lines and all(text_width(draw, line, font) <= max_width for line in lines):
                return lines, font
            font_size -= 3
        font = self._load_font(18)
        return wrap_text(normalized, draw, font, max_width), font

    def _draw_centered_lines(
        self,
        draw: Any,
        lines: list[str],
        font: Any,
        image_width: int,
        y: int,
        stroke_width: int,
    ) -> None:
        current_y = y
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            x = (image_width - line_width) / 2
            draw.text(
                (x, current_y),
                line,
                font=font,
                fill="white",
                stroke_width=stroke_width,
                stroke_fill="black",
            )
            current_y += line_height + max(4, int(line_height * 0.12))

    def _lines_height(
        self,
        draw: Any,
        lines: list[str],
        font: Any,
        stroke_width: int,
    ) -> int:
        if not lines:
            return 0
        heights = [
            draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)[3]
            - draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)[1]
            for line in lines
        ]
        return sum(heights) + max(4, int(max(heights) * 0.12)) * (len(lines) - 1)

    def _load_font(self, size: int) -> Any:
        from PIL import ImageFont

        candidates = [self.font_path, DEFAULT_MEME_FONT_PATH]
        for path in candidates:
            if path and path.exists():
                try:
                    return ImageFont.truetype(str(path), size=size)
                except OSError:
                    logging.warning("Failed to load meme font: %s", path)
        return ImageFont.load_default(size=size)

    async def unload(self) -> None:
        if not self.unload_after_task or not self.model:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.model, "prompt": "", "keep_alive": 0},
                )
                response.raise_for_status()
            except Exception:  # noqa: BLE001
                logging.exception("Failed to unload Ollama meme model %s", self.model)
                return
        logging.info("Ollama meme model unloaded model=%s", self.model)


def parse_meme_caption(raw: str) -> MemeCaption:
    payload = raw.strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", payload, flags=re.DOTALL)
    if match:
        payload = match.group(0)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        text = normalize_caption_text(raw) or "Когда хотел мем, а получилась жизнь"
        return MemeCaption(top_text="", bottom_text=clip_caption_text(text), alt_text="Fallback caption")
    if not isinstance(data, dict):
        text = normalize_caption_text(raw) or "Когда всё пошло не по плану"
        return MemeCaption(top_text="", bottom_text=clip_caption_text(text), alt_text="Fallback caption")

    top_text = clip_caption_text(str(data.get("top_text", "")))
    bottom_text = clip_caption_text(str(data.get("bottom_text", "")))
    alt_text = normalize_caption_text(str(data.get("alt_text", "")))
    if not top_text and not bottom_text:
        top_text = "Когда всё пошло не по плану"
        bottom_text = "Но ты уже сделал вид, что так и надо"
    elif not top_text:
        top_text = "Ожидание"
    elif not bottom_text:
        bottom_text = "Реальность"
    return MemeCaption(top_text=top_text, bottom_text=bottom_text, alt_text=alt_text)


def normalize_caption_text(text: str) -> str:
    return " ".join(text.replace("\n", " ").split()).strip()


def clip_caption_text(text: str) -> str:
    normalized = normalize_caption_text(text)
    if len(normalized) <= MAX_TEXT_CHARS:
        return normalized
    return normalized[:MAX_TEXT_CHARS].rstrip(" .,;:!?")


def wrap_text(
    text: str,
    draw: Any,
    font: Any,
    max_width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def text_width(
    draw: Any,
    text: str,
    font: Any,
) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]
