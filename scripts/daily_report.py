"""Daily report through Composio's hosted MCP server.

No OpenAI SDK or OpenAI API is used. Composio's MCP meta-tools discover and
execute Exa, Notion, and Gmail tools on the connected user account.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
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
SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
TOP_AI_STOCKS = [
    ("NVIDIA", "NVDA"),
    ("Microsoft", "MSFT"),
    ("Alphabet", "GOOGL"),
    ("Amazon", "AMZN"),
    ("Meta Platforms", "META"),
    ("Broadcom", "AVGO"),
    ("AMD", "AMD"),
    ("Oracle", "ORCL"),
    ("Palantir", "PLTR"),
    ("TSMC", "TSM"),
]


def result_text(result: Any) -> str:
    return "\n".join(
        value
        for item in getattr(result, "content", []) or []
        if (value := getattr(item, "text", None))
    )


def result_json(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(result_text(result))
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    raw = result_text(result)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP returned non-JSON data: {raw[:1200]}") from exc


def nested_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


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


def active_account(statuses: list[dict[str, Any]], toolkit: str) -> str:
    for item in statuses:
        if item.get("toolkit", "").lower() == toolkit.lower():
            for account in item.get("accounts", []) or []:
                if str(account.get("status", "")).upper() == "ACTIVE" and account.get("id"):
                    return account["id"]
            if item.get("has_active_connection"):
                return ""
    raise RuntimeError(f"No active Composio account found for {toolkit}")


def tool_call(tool_slug: str, account: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call = {"tool_slug": tool_slug, "arguments": arguments}
    if account:
        call["account"] = account
    return call


def validate_news_window(answer: str, block_name: str, start: datetime, end: datetime) -> None:
    """Reject stale or fabricated Exa results before publishing them."""
    if "NO_QUALIFYING_NEWS" in answer:
        raise RuntimeError(f"Exa found no qualifying {block_name} stories in the last 72 hours")

    matches = re.findall(
        r"(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\s+de\s+(\d{4})(?:\s+a\s+las\s+(\d{1,2}):(\d{2}))?",
        answer.lower(),
    )
    qualifying_dates = []
    for day, month_name, year, hour, minute in matches:
        candidate = datetime(
            int(year),
            SPANISH_MONTHS[month_name],
            int(day),
            int(hour or 0),
            int(minute or 0),
            tzinfo=timezone.utc,
        )
        if start <= candidate <= end:
            qualifying_dates.append(candidate)
    for year, month, day, hour, minute in re.findall(
        r"(20\d{2})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?",
        answer,
    ):
        candidate = datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=timezone.utc
        )
        if start <= candidate <= end:
            qualifying_dates.append(candidate)
    if len(qualifying_dates) < 3:
        raise RuntimeError(
            f"Exa returned fewer than three verifiable {block_name} publication dates "
            f"inside the last-72-hour window: {answer[:800]}"
        )


def parse_news_json(answer: str, block_name: str, start: datetime, end: datetime) -> str:
    """Validate Exa's structured news response and render it for the report."""
    candidate = answer.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Exa did not return valid structured JSON for {block_name}: {answer[:800]}") from exc

    stories = payload.get("stories") if isinstance(payload, dict) else None
    if not isinstance(stories, list) or len(stories) < 3:
        raise RuntimeError(f"Exa returned fewer than three structured {block_name} stories: {answer[:800]}")

    rendered: list[str] = []
    for index, story in enumerate(stories[:5], start=1):
        if not isinstance(story, dict):
            raise RuntimeError(f"Exa returned an invalid {block_name} story at position {index}")
        title = str(story.get("title", "")).strip()
        published_at = str(story.get("published_at", "")).strip()
        summary = str(story.get("summary", "")).strip()
        relevance = str(story.get("relevance", "")).strip()
        source_url = str(story.get("source_url", "")).strip()
        if not all((title, published_at, summary, relevance, source_url)):
            raise RuntimeError(f"Exa returned incomplete structured {block_name} story at position {index}")
        try:
            published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(f"Exa returned an invalid published_at for {block_name}: {published_at}") from exc
        if published_dt.tzinfo is None:
            published_dt = published_dt.replace(tzinfo=timezone.utc)
        published_dt = published_dt.astimezone(timezone.utc)
        if not start <= published_dt <= end:
            raise RuntimeError(f"Exa returned a {block_name} story outside the last-72-hour window: {published_at}")
        if not re.match(r"^https?://", source_url):
            raise RuntimeError(f"Exa returned an invalid original source URL for {block_name}: {source_url}")
        rendered.append(
            f"### {index}. {title}\n"
            f"Published: {published_dt:%Y-%m-%d %H:%M UTC}\n\n"
            f"{summary}\n\n"
            f"{relevance}\n\n"
            f"Original source: {source_url}"
        )
    return "\n\n".join(rendered)


def validate_stock_table(answer: str) -> None:
    missing = [ticker for _, ticker in TOP_AI_STOCKS if ticker not in answer]
    if missing:
        raise RuntimeError(f"Exa stock table is missing tickers: {', '.join(missing)}")
    if not re.search(r"20\d{2}-\d{2}-\d{2}", answer):
        raise RuntimeError("Exa stock table has no explicit market date")


def html_fragment(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', escaped)
    return escaped.replace("\n", "<br>\n")


def parse_stock_table(text: str) -> list[list[str]]:
    """Parse Exa's Markdown table into rows for HTML and Notion blocks."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "|" not in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if cells:
            rows.append(cells)
    if len(rows) < 2:
        raise RuntimeError(f"Exa stock answer was not a readable Markdown table: {text[:800]}")
    return rows


def stock_table_html(rows: list[list[str]]) -> str:
    header, *body = rows
    output = [
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px">',
        "<thead><tr style=\"background:#eef2f7\">",
    ]
    output.extend(f"<th align=\"left\">{html.escape(cell)}</th>" for cell in header)
    output.append("</tr></thead><tbody>")
    for row in body:
        output.append("<tr>")
        output.extend(f"<td>{html_fragment(cell)}</td>" for cell in row)
        output.append("</tr>")
    output.append("</tbody></table>")
    return "".join(output)


def notion_table_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Build Notion API table_row blocks from parsed cells."""
    return [
        {
            "object": "block",
            "type": "table_row",
            "table_row": {
                "cells": [
                    [{"type": "text", "text": {"content": cell[:2000]}}]
                    for cell in row
                ]
            },
        }
        for row in rows
    ]


async def meta_call(mcp: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments)
    payload = result_json(result)
    if payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            details = " | ".join(
                str(error[key]) for key in ("message", "detail", "code", "status")
                if error.get(key) is not None
            )
        else:
            details = str(error)
        raise RuntimeError(f"{name} failed: {details[:2000]}")
    return nested_data(payload)


async def run() -> None:
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    window_start_dt = now.astimezone(timezone.utc).replace(microsecond=0) - timedelta(hours=72)
    window_end_dt = now.astimezone(timezone.utc).replace(microsecond=0)
    window_start = window_start_dt.isoformat().replace("+00:00", "Z")
    window_end = window_end_dt.isoformat().replace("+00:00", "Z")

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
                available = await mcp.list_tools()
                print("[MCP] tools=" + ",".join(tool.name for tool in available.tools))

                search = await meta_call(
                    mcp,
                    "COMPOSIO_SEARCH_TOOLS",
                    {
                        "session": {"generate_id": True},
                        "queries": [
                            {"use_case": "find the most relevant AI launch and capability news from the last 72 hours using only Exa"},
                            {"use_case": "find the most relevant AI finance investment acquisition and business news from the last 72 hours using only Exa"},
                            {"use_case": "find current prices and daily changes for the top ten AI-related public companies using only Exa"},
                            {"use_case": "create a Notion page with a financial report and source links"},
                            {"use_case": "append a native table and table rows to a Notion page"},
                            {"use_case": "send the report by Gmail from one address to another"},
                        ],
                    },
                )
                composio_session_id = search.get("session", {}).get("id")
                if not composio_session_id:
                    raise RuntimeError(f"Composio search returned no session id: {search}")
                statuses = search.get("toolkit_connection_statuses", [])
                print(
                    "[MCP] connections="
                    + json.dumps(
                        [
                            {
                                "toolkit": item.get("toolkit"),
                                "has_active_connection": item.get("has_active_connection"),
                                "accounts": [
                                    {"id": account.get("id"), "status": account.get("status")}
                                    for account in item.get("accounts", []) or []
                                ],
                            }
                            for item in statuses
                        ],
                        ensure_ascii=False,
                    )
                )
                exa_account = active_account(statuses, "exa")
                notion_account = active_account(statuses, "notion")
                gmail_account = active_account(statuses, "gmail")

                research = await meta_call(
                    mcp,
                    "COMPOSIO_MULTI_EXECUTE_TOOL",
                    {
                        "session_id": composio_session_id,
                        "current_step": "RESEARCHING_AI_NEWS_AND_STOCKS",
                        "current_step_metric": "0/3 research queries",
                        "sync_response_to_workbench": False,
                        "thought": "Retrieve fresh AI news and current stock prices using only Exa.",
                        "tools": [
                            tool_call("EXA_ANSWER", exa_account, {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Using only Exa, identify exactly three of the most relevant international news stories published between {window_start} and {window_end} UTC about new artificial intelligence launches, new AI developments, or new AI capabilities. Use original English-language headlines and prioritize original English-language American or other authoritative sources. Return ONLY valid JSON, with no Markdown fences or commentary, in exactly this shape: {{\"stories\":[{{\"title\":\"...\",\"published_at\":\"YYYY-MM-DDTHH:MM:SSZ\",\"summary\":\"2-3 factual sentences in English\",\"relevance\":\"why it matters in English\",\"source_url\":\"https://original-source...\"}}]}}. Every published_at must be the verified publication timestamp of that specific article, every source_url must be the direct original English-language article URL, and every story must be inside this exact 72-hour window. Exclude duplicates. If fewer than three qualifying stories exist, return {{\"stories\":[]}}.",
                            }),
                            tool_call("EXA_ANSWER", exa_account, {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Using only Exa, identify exactly three of the most relevant international news stories published between {window_start} and {window_end} UTC about AI finance: investments, acquisitions, funding rounds, valuations, earnings, partnerships with material financial impact, or AI business strategy. Use original English-language headlines and prioritize original English-language American or other authoritative sources. Return ONLY valid JSON, with no Markdown fences or commentary, in exactly this shape: {{\"stories\":[{{\"title\":\"...\",\"published_at\":\"YYYY-MM-DDTHH:MM:SSZ\",\"summary\":\"2-3 factual sentences in English\",\"relevance\":\"financial relevance in English\",\"source_url\":\"https://original-source...\"}}]}}. Every published_at must be the verified publication timestamp of that specific article, every source_url must be the direct original English-language article URL, and every story must be inside this exact 72-hour window. Exclude duplicates. If fewer than three qualifying stories exist, return {{\"stories\":[]}}.",
                            }),
                            tool_call("EXA_ANSWER", exa_account, {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Using only Exa, provide a current market snapshot as of {window_end} UTC for exactly these ten AI-related public companies: {', '.join(f'{name} ({ticker})' for name, ticker in TOP_AI_STOCKS)}. Return only a Markdown table with columns Company, Ticker, Current price, Daily change, Currency, Market date/time, and Original source. Use the latest verifiable market price available for each ticker, do not invent values, include a direct English-language source URL in every row, and include the market date in ISO format YYYY-MM-DD. If a price is unavailable, write unavailable instead of guessing.",
                            }),
                        ],
                    },
                )
                results = research.get("results", [])
                if len(results) < 3:
                    raise RuntimeError(f"Research returned incomplete results: {research}")
                launches_response = results[0].get("response", {})
                finance_response = results[1].get("response", {})
                stocks_response = results[2].get("response", {})
                if not all(response.get("successful") for response in (launches_response, finance_response, stocks_response)):
                    raise RuntimeError(f"Exa research failed: {research}")
                launches = launches_response.get("data", {})
                finance = finance_response.get("data", {})
                stocks = stocks_response.get("data", {})
                launches_answer = launches.get("answer", "")
                finance_answer = finance.get("answer", "")
                stocks_text = stocks.get("answer", "")
                launches_text = parse_news_json(launches_answer, "AI Developments", window_start_dt, window_end_dt)
                finance_text = parse_news_json(finance_answer, "AI Finance", window_start_dt, window_end_dt)
                validate_stock_table(stocks_text)
                stock_rows = parse_stock_table(stocks_text)
                stock_html = stock_table_html(stock_rows)
                citations = (launches.get("citations", []) or []) + (finance.get("citations", []) or []) + (stocks.get("citations", []) or [])
                citation_lines = "\n".join(
                    f"- [{item.get('title', 'Source')}]({item.get('url')})"
                    for item in citations[:10]
                    if item.get("url")
                )

                title = f"Most relevant IA NEWS — {now:%Y-%m-%d}"
                markdown = (
                    "# Most relevant IA NEWS\n\n"
                    f"_Report generated on {now:%Y-%m-%d} at {now:%H:%M} in Monterrey, Nuevo León, Mexico._\n\n"
                    "## **AI Developments**\n\n"
                    f"{launches_text}\n\n"
                    "## **AI Finance**\n\n"
                    f"{finance_text}\n\n"
                    "## **AI Leaders Stock Prices**\n\n"
                    "The stock snapshot is inserted as a native Notion table below.\n\n"
                    "### Sources searched through Exa\n\n"
                    f"{citation_lines}\n\n"
                    f"_Search performed exclusively with Exa and restricted to the exact last-72-hour window._"
                )

                notion_result = await meta_call(
                    mcp,
                    "COMPOSIO_MULTI_EXECUTE_TOOL",
                    {
                        "session_id": composio_session_id,
                        "current_step": "CREATING_NOTION_REPORT",
                        "current_step_metric": "0/1 pages",
                        "sync_response_to_workbench": False,
                        "thought": "Create the daily report in the configured Notion parent page.",
                        "tools": [tool_call(
                            "NOTION_CREATE_NOTION_PAGE",
                            notion_account,
                            {"parent_id": PARENT_ID, "title": title, "icon": "📰", "markdown": markdown},
                        )],
                    },
                )
                notion_response = notion_result.get("results", [{}])[0].get("response", {})
                if not notion_response.get("successful"):
                    raise RuntimeError(f"Notion create failed: {notion_result}")
                page = notion_response.get("data", {})
                page_url = find_value(page, {"url", "public_url"})
                page_id = find_value(page, {"id", "page_id"})
                if not page_id:
                    raise RuntimeError(f"Notion returned no page id needed for the native stock table: {notion_result}")
                page_url = page_url or "(URL unavailable)"

                notion_table_result = await meta_call(
                    mcp,
                    "COMPOSIO_MULTI_EXECUTE_TOOL",
                    {
                        "session_id": composio_session_id,
                        "current_step": "ADDING_NOTION_STOCK_TABLE",
                        "current_step_metric": "0/1 tables",
                        "sync_response_to_workbench": False,
                        "thought": "Add the stock snapshot as a native Notion table for readability.",
                        "tools": [tool_call(
                            "NOTION_APPEND_BLOCK_CHILDREN",
                            notion_account,
                            {"block_id": page_id, "children": [
                                {
                                    "object": "block",
                                    "type": "table",
                                    "table": {
                                        "table_width": len(stock_rows[0]),
                                        "has_column_header": True,
                                        "has_row_header": False,
                                    },
                                }
                            ]},
                        )],
                    },
                )
                table_response = notion_table_result.get("results", [{}])[0].get("response", {})
                if not table_response.get("successful"):
                    raise RuntimeError(f"Notion table creation failed: {notion_table_result}")
                table_block_id = find_value(table_response.get("data", {}), {"id", "block_id"})
                if not table_block_id:
                    raise RuntimeError(f"Notion table creation returned no block id: {notion_table_result}")

                notion_rows_result = await meta_call(
                    mcp,
                    "COMPOSIO_MULTI_EXECUTE_TOOL",
                    {
                        "session_id": composio_session_id,
                        "current_step": "FILLING_NOTION_STOCK_TABLE",
                        "current_step_metric": f"0/{len(stock_rows)} rows",
                        "sync_response_to_workbench": False,
                        "thought": "Fill the native Notion stock table with the verified Exa values.",
                        "tools": [tool_call(
                            "NOTION_APPEND_BLOCK_CHILDREN",
                            notion_account,
                            {"block_id": table_block_id, "children": notion_table_rows(stock_rows)},
                        )],
                    },
                )
                rows_response = notion_rows_result.get("results", [{}])[0].get("response", {})
                if not rows_response.get("successful"):
                    raise RuntimeError(f"Notion table rows failed: {notion_rows_result}")

                email_body = (
                    "Most relevant IA NEWS\n\n"
                    f"Report generated on {now:%Y-%m-%d} at {now:%H:%M} in Monterrey, Nuevo León, Mexico.\n\n"
                    f"Full report in Notion: {page_url}\n\n"
                    "**BLOCK 1 — AI DEVELOPMENTS**\n\n"
                    f"{launches_text}\n\n"
                    "**BLOCK 2 — AI FINANCE**\n\n"
                    f"{finance_text}\n\n"
                    "**BLOCK 3 — AI LEADERS STOCK PRICES**\n\n"
                    "See the formatted table below.\n\n"
                    "Sources searched through Exa:\n"
                    f"{citation_lines}"
                )
                email_body_html = (
                    "<h1>Most relevant IA NEWS</h1>"
                    f"<p>Report generated on {now:%Y-%m-%d} at {now:%H:%M} in Monterrey, Nuevo León, Mexico.</p>"
                    f'<p><strong><a href="{html.escape(page_url)}">Full report in Notion</a></strong></p>'
                    "<h2><strong>BLOCK 1 — AI DEVELOPMENTS</strong></h2>"
                    f"<p>{html_fragment(launches_text)}</p>"
                    "<h2><strong>BLOCK 2 — AI FINANCE</strong></h2>"
                    f"<p>{html_fragment(finance_text)}</p>"
                    "<h2><strong>BLOCK 3 — AI LEADERS STOCK PRICES</strong></h2>"
                    f"{stock_html}"
                    "<p><strong>Sources searched through Exa:</strong><br>"
                    f"{html_fragment(citation_lines)}</p>"
                )
                email_result = await meta_call(
                    mcp,
                    "COMPOSIO_MULTI_EXECUTE_TOOL",
                    {
                        "session_id": composio_session_id,
                        "current_step": "SENDING_REPORT_EMAIL",
                        "current_step_metric": "0/1 emails",
                        "sync_response_to_workbench": False,
                        "thought": "Send the report from the configured Gmail sender to the configured recipient.",
                        "tools": [tool_call("GMAIL_SEND_EMAIL", gmail_account, {
                                "from_email": GMAIL_FROM,
                                "recipient_email": GMAIL_TO,
                                "subject": f"Most relevant IA NEWS — {now:%Y-%m-%d}",
                                "body": email_body_html,
                                "is_html": True,
                        })],
                    },
                )
                email_response = email_result.get("results", [{}])[0].get("response", {})
                if not email_response.get("successful"):
                    raise RuntimeError(f"Gmail send failed: {email_result}")
                email_id = find_value(email_response.get("data", {}), {"id", "message_id", "threadId", "thread_id"})
                if not email_id:
                    raise RuntimeError(f"Gmail returned no message id: {email_result}")

                print(json.dumps({"notion_url": page_url, "email_id": email_id, "sent_to": GMAIL_TO}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run())
