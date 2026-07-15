from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from tg_summary_bot.config import Settings


@dataclass(frozen=True)
class WikiSearchResult:
    title: str
    extract: str
    url: str


class WikipediaSearchClient:
    def __init__(self, settings: Settings) -> None:
        self.language = settings.wiki_language
        self.timeout_seconds = settings.wiki_timeout_seconds
        self.max_results = settings.wiki_max_results
        self.user_agent = settings.wiki_user_agent
        self.base_url = f"https://{self.language}.wikipedia.org"

    async def search(self, query: str) -> list[WikiSearchResult]:
        query = " ".join(query.split())
        if not query:
            return []

        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
            search_response = await client.get(
                f"{self.base_url}/w/rest.php/v1/search/page",
                params={"q": query, "limit": self.max_results},
                headers={"User-Agent": self.user_agent},
            )
            try:
                search_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = search_response.text.strip()[:1000] or str(exc)
                raise RuntimeError(f"Wikipedia search API error: {detail}") from exc

            pages = search_response.json().get("pages") or []
            results: list[WikiSearchResult] = []
            for page in pages[: self.max_results]:
                title = str(page.get("title") or "").strip()
                key = str(page.get("key") or title).strip()
                if not title:
                    continue
                summary = await self._summary(client, key)
                if summary:
                    results.append(summary)
                    continue
                excerpt = clean_wiki_excerpt(str(page.get("excerpt") or "")).strip()
                url = f"{self.base_url}/wiki/{quote(key.replace(' ', '_'))}"
                results.append(WikiSearchResult(title=title, extract=excerpt, url=url))
            return results

    async def _summary(self, client: httpx.AsyncClient, key: str) -> WikiSearchResult | None:
        response = await client.get(
            f"{self.base_url}/api/rest_v1/page/summary/{quote(key, safe='')}",
            params={"redirect": "true"},
            headers={"User-Agent": self.user_agent},
        )
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            return None
        data = response.json()
        title = str(data.get("title") or key).strip()
        extract = str(data.get("extract") or "").strip()
        content_urls = data.get("content_urls") or {}
        desktop = content_urls.get("desktop") or {}
        url = str(desktop.get("page") or "").strip()
        if not url:
            url = f"{self.base_url}/wiki/{quote(key.replace(' ', '_'))}"
        return WikiSearchResult(title=title, extract=extract, url=url)


def clean_wiki_excerpt(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(value.split())


def format_wiki_results(query: str, results: list[WikiSearchResult]) -> str:
    if not results:
        return f"По запросу `{query}` в Wikipedia ничего не нашлось."

    lines = [f"**Wikipedia: {query}**"]
    for index, result in enumerate(results, start=1):
        extract = result.extract or "Краткое описание недоступно."
        lines.append(f"\n{index}. **{result.title}**\n{extract}\nИсточник: {result.url}")
    return "\n".join(lines)
