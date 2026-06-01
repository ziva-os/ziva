from __future__ import annotations

import re
from typing import Any, Dict


class WebFetchTool:
    """Fetch content from URLs."""

    def spec(self):
        return {
            "name": "web_fetch",
            "description": "Fetch content from a URL. Returns raw, HTML, or markdown-formatted text.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "format": {"type": "string", "description": "Output format: raw, html, or markdown (default 'markdown')"},
                },
                "required": ["url"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        url = input_data.get("url")
        if not url:
            return {"error": "invalid_input", "message": "url is required"}

        format_type = input_data.get("format", "markdown").lower()
        if format_type not in ("raw", "html", "markdown"):
            return {"error": "invalid_input", "message": "format must be one of: raw, html, markdown"}

        try:
            import aiohttp
        except ImportError:
            return {"error": "dependency_missing", "message": "aiohttp is required for web_fetch"}

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        return {"error": "fetch_failed", "message": f"HTTP {resp.status}"}

                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" in content_type:
                        html = await resp.text()
                    else:
                        # For non-HTML, return as raw
                        data = await resp.read()
                        html = data.decode("utf-8", errors="ignore")

        except Exception as e:
            return {"error": "fetch_failed", "message": str(e)}

        # Process based on format
        if format_type == "raw":
            content = html
        elif format_type == "html":
            content = html
        else:  # markdown
            # Strip HTML tags and convert to readable text
            content = self._html_to_text(html)

        return {
            "url": url,
            "format": format_type,
            "content": content,
            "original_size": len(html),
        }

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text by stripping tags."""
        # Remove script and style elements
        html = re.sub(r"<(script|style).*?>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)

        # Replace common block elements with newlines
        html = re.sub(r"</?(div|p|br|h[1-6]|li|tr)>", "\n", html, flags=re.IGNORECASE)

        # Replace table cells with spaces
        html = re.sub(r"</(td|th)>", "  ", html, flags=re.IGNORECASE)

        # Remove all remaining tags
        html = re.sub(r"<[^>]+>", "", html)

        # Clean up whitespace
        lines = html.split("\n")
        cleaned = []
        for line in lines:
            line = line.strip()
            if line:
                cleaned.append(line)

        return "\n".join(cleaned)
