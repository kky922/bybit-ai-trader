from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

import config
from infra.state import append_news_snapshot


@dataclass
class NewsItem:
    title: str
    body: str
    source: str
    url: str
    published_at: str
    coins_mentioned: list[str]


class NewsCollector:
    def _extract_symbols(self, text: str) -> list[str]:
        majors = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "FET"]
        upper = text.upper()
        return [m for m in majors if m in upper]

    def collect(self, lookback_hours: int = 6) -> list[NewsItem]:
        now = datetime.now(timezone.utc)
        earliest = now - timedelta(hours=lookback_hours)
        seen: set[str] = set()
        out: list[NewsItem] = []

        for source in config.NEWS_SOURCES:
            feed = feedparser.parse(source)
            for entry in feed.entries[:50]:
                url = entry.get("link", "").strip()
                if not url:
                    continue
                key = hashlib.sha256(url.encode("utf-8")).hexdigest()
                if key in seen:
                    continue
                seen.add(key)

                pub = entry.get("published_parsed")
                published_at = (
                    datetime(*pub[:6], tzinfo=timezone.utc).isoformat()
                    if pub
                    else now.isoformat()
                )
                pub_dt = datetime.fromisoformat(published_at)
                if pub_dt < earliest:
                    continue
                title = entry.get("title", "").strip()
                body = entry.get("summary", title).strip()
                out.append(
                    NewsItem(
                        title=title,
                        body=body,
                        source=source,
                        url=url,
                        published_at=published_at,
                        coins_mentioned=self._extract_symbols(f"{title} {body}"),
                    )
                )

        append_news_snapshot(
            {"ts": now.isoformat(), "type": "raw_news", "count": len(out), "items": [asdict(x) for x in out]}
        )
        return out
