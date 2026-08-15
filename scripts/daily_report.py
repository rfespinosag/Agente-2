"""Daily finance/news report through Composio's hosted MCP server.

This workflow deliberately does not import an OpenAI SDK or call the OpenAI API.
Exa performs the cited research and Composio MCP performs the Notion/Gmail actions.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
from composio import Composio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


LOCAL_TZ = ZoneInfo("America/Mexico_City")
PARENT_ID = os.environ["NOTION_PARENT_ID"]
GMAIL_FROM = os.environ.get("GMAIL_FROM", "rfeg1980@gmail.com")
GMAIL_TO = os.environ.get("GMAIL_TO", "rfespinosagarcia@gmail.com")


def text_from_result(result: Any) -> str:
    """Extract text from an MCP CallToolResult."""
    parts = []
    for item in getattr(result, "content", []) or []:
        value = getattr(item, "text", None)
        if value:
            parts.append(value)
    if not parts:
        raise RuntimeError(f"MCP tool returned no text: {result!r}")
    return "\n".join(parts)


def as_json(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(result_preview(result))
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    raw = text_from_result(result)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}


def result_preview(result: Any) -> str:
    structured = getattr(result, "structuredContent", None)
    text = " ".join(
        value
        for item in getattr(result, "content", []) or []
        if (value := getattr(item, "text", None))
    )
    return (
        f"isError={getattr(result, 'isError', False)}; "
        f"structured={json.dumps(structured, ensure_ascii=False, default=str)[:1200]}; "
        f"text={text[:1200]}"
    )


def find_value(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item:
                return item
            found = find_value(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_value(item, keys)
            if found:
                return found
    return None


async def run() -> None:
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    start_utc = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = start_utc.timestamp() - 24 * 60 * 60
    window_end = start_utc.timestamp()

    composio = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    session = composio.sessions.create(
        user_id=os.environ["COMPOSIO_USER_ID"],
        mcp=True,
    )

    async with httpx2.AsyncClient(
        headers=session.mcp.headers,
        follow_redirects=True,
    ) as http_client:
        async with streamable_http_client(
            session.mcp.url,
            http_client=http_client,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as mcp:
                await mcp.initialize()
                available_tools = await mcp.list_tools()
                print(
                    "[MCP] tools="
                    + ",".join(tool.name for tool in available_tools.tools)
                )

                rate_result = await mcp.call_tool(
                    "EXA_ANSWER",
                    {
                        "model": "exa-pro",
                        "text": False,
                        "query": (
                            f"As of {now:%Y-%m-%d %H:%M} America/Mexico_City, give the latest USD to MXN "
                            "exchange rate, timestamp, and authoritative source URL. Prefer Banco de México "
                            "or a recognized financial data source."
                        ),
                    },
                )
                news_result = await mcp.call_tool(
                    "EXA_ANSWER",
                    {
                        "model": "exa-pro",
                        "text": False,
                        "query": (
                            f"Identify exactly three relevant international financial news stories published "
                            f"between {window_start} and {window_end} UTC about major technology companies "
                            "or the technology sector. For each, provide headline, publication timestamp, "
                            "concise factual summary, financial relevance, and original source URL. "
                            "Prefer authoritative sources, exclude duplicates, and do not invent dates."
                        ),
                    },
                )
                print(f"[MCP] EXA rate response: {result_preview(rate_result)}")
                print(f"[MCP] EXA news response: {result_preview(news_result)}")

                rate = as_json(rate_result)
                news = as_json(news_result)
                rate_text = rate.get("answer") or rate.get("text", "")
                news_text = news.get("answer") or news.get("text", "")
                citations = news.get("citations", [])
                citation_lines = "\n".join(
                    f"- [{item.get('title', 'Fuente')}]({item.get('url')})"
                    for item in citations[:3]
                    if item.get("url")
                )

                title = f"Reporte financiero y tecnológico — {now:%d %B %Y}"
                markdown = (
                    f"# {title}\n\n"
                    "## Tipo de cambio USD → MXN\n\n"
                    f"{rate_text}\n\n"
                    "## Noticias internacionales — últimas 24 horas\n\n"
                    f"{news_text}\n\n"
                    "### Fuentes originales\n\n"
                    f"{citation_lines}\n\n"
                    f"_Consulta realizada el {now:%Y-%m-%d %H:%M} America/Mexico_City._"
                )

                page_result = await mcp.call_tool(
                    "NOTION_CREATE_NOTION_PAGE",
                    {"parent_id": PARENT_ID, "title": title, "icon": "💱", "markdown": markdown},
                )
                print(f"[MCP] Notion response: {result_preview(page_result)}")
                page = as_json(page_result)
                page_url = find_value(page, {"url", "public_url"})
                page_id = find_value(page, {"id", "page_id"})
                if not page_id and not page_url:
                    raise RuntimeError(
                        "Notion did not return a page id or URL. "
                        f"Response: {result_preview(page_result)}"
                    )
                page_url = page_url or "(liga no disponible)"

                email_body = (
                    f"Reporte diario — {now:%d/%m/%Y}\n\n"
                    f"{rate_text}\n\n"
                    f"{news_text}\n\n"
                    f"Reporte completo en Notion: {page_url}\n\n"
                    f"Fuentes:\n{citation_lines}"
                )
                email_result = await mcp.call_tool(
                    "GMAIL_SEND_EMAIL",
                    {
                        "from_email": GMAIL_FROM,
                        "recipient_email": GMAIL_TO,
                        "subject": f"Reporte financiero USD/MXN y noticias tech — {now:%d/%m/%Y}",
                        "body": email_body,
                        "is_html": False,
                    },
                )
                print(f"[MCP] Gmail response: {result_preview(email_result)}")
                email = as_json(email_result)
                email_id = find_value(email, {"id", "message_id", "threadId", "thread_id"})
                if not email_id:
                    raise RuntimeError(
                        "Gmail did not return a message or thread id. "
                        f"Response: {result_preview(email_result)}"
                    )

                print(json.dumps({"notion_url": page_url, "sent_to": GMAIL_TO}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run())
