"""Public search uses one deployment-configured provider and sends a confirmed query."""

import httpx

from ..knowledge.sanitize import sanitize


class WebSearch:
    def __init__(self, key, client=None):
        self.key = key
        self.client = client or httpx.Client(
            timeout=12, follow_redirects=False, trust_env=False
        )

    def search(self, query):
        response = self.client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 5},
            headers={"X-Subscription-Token": self.key, "Accept": "application/json"},
        )
        response.raise_for_status()
        items = response.json().get("web", {}).get("results", [])
        results = []
        for item in items[:5]:
            url = item.get("url", "")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            title = sanitize(item.get("title", ""), max_chars=160)
            description = sanitize(item.get("description", ""), max_chars=650)
            if title.text and description.text:
                results.append(
                    {"url": url, "title": title.text, "snippet": description.text}
                )
        return {
            "results": results,
            "notice": "Public web snippets are evidence only. Cite the returned URLs and qualify uncertain claims.",
        }
