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
from pathlib import Path
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
EMAIL_MAX_ATTEMPTS = 5
EMAIL_ATTEMPT_TIMEOUT_SECONDS = 180
EMAIL_RETRY_DELAY_SECONDS = 150
NEWS_HISTORY_PATH = Path(os.environ.get("NEWS_HISTORY_PATH", ".cache/news_history.json"))
MAX_NEWS_HISTORY_ENTRIES = 240
MAX_PUBLICATION_FUTURE_SKEW = timedelta(minutes=10)
MCP_MAX_ATTEMPTS = 5
MCP_RETRY_DELAY_SECONDS = 150
EXA_RESEARCH_RETRY_DELAY_SECONDS = 15
MCP_HTTP_TIMEOUT_SECONDS = 180
MCP_CONNECT_TIMEOUT_SECONDS = 30


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
        if not start <= published_dt <= end + MAX_PUBLICATION_FUTURE_SKEW:
            raise RuntimeError(f"Exa returned a {block_name} story outside the last-72-hour window: {published_at}")
        if not re.match(r"^https?://", source_url):
            raise RuntimeError(f"Exa returned an invalid original source URL for {block_name}: {source_url}")
        rendered.append(
            f"{index}. **{title}**\n"
            f"Published: {published_dt:%Y-%m-%d %H:%M UTC}\n\n"
            f"{summary}\n\n"
            f"{relevance}\n\n"
            f"Original source: {source_url}"
        )
    return "\n\n".join(rendered)


def extract_news_entries(answer: str) -> list[dict[str, str]]:
    """Extract stable title/URL keys used to prevent repeat stories."""
    candidate = answer.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE | re.DOTALL).strip()
    payload = json.loads(candidate)
    stories = payload.get("stories", []) if isinstance(payload, dict) else []
    return [
        {
            "title": str(story.get("title", "")).strip(),
            "source_url": str(story.get("source_url", "")).strip(),
        }
        for story in stories
        if isinstance(story, dict)
    ]


def normalize_news_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def select_new_news_answer(answer: str, history: list[dict[str, str]], block_name: str) -> str:
    """Keep the first three newest candidates, de-duplicated within this report."""
    candidate = answer.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE | re.DOTALL).strip()
    payload = json.loads(candidate)
    stories = payload.get("stories", []) if isinstance(payload, dict) else []
    # Historical repetition is intentional: a story may remain among the newest
    # qualifying items on a later run. Only duplicates in the current response
    # (and across the two sections) should be removed.
    selected: list[dict[str, Any]] = []
    selected_urls: set[str] = set()
    selected_titles: set[str] = set()
    for story in stories:
        if not isinstance(story, dict):
            continue
        url_key = normalize_news_key(str(story.get("source_url", "")))
        title_key = normalize_news_key(str(story.get("title", "")))
        if not url_key or not title_key:
            continue
        if url_key in selected_urls or title_key in selected_titles:
            continue
        selected.append(story)
        selected_urls.add(url_key)
        selected_titles.add(title_key)
        if len(selected) == 3:
            break
    if len(selected) < 3:
        raise RuntimeError(f"Exa returned fewer than three distinct {block_name} stories")
    return json.dumps({"stories": selected}, ensure_ascii=False)


def prepare_research_payload(
    research: dict[str, Any],
    history: list[dict[str, str]],
    window_start_dt: datetime,
    window_end_dt: datetime,
) -> tuple[dict[str, Any], dict[str, Any], str, str, list[dict[str, str]], list[dict[str, str]]]:
    results = research.get("results", [])
    if len(results) < 2:
        raise RuntimeError(f"Research returned incomplete results: {research}")
    launches_response = results[0].get("response", {})
    finance_response = results[1].get("response", {})
    if not all(response.get("successful") for response in (launches_response, finance_response)):
        raise RuntimeError(f"Exa research failed: {research}")
    launches = launches_response.get("data", {})
    finance = finance_response.get("data", {})
    launches_answer = select_new_news_answer(launches.get("answer", ""), history, "AI Developments")
    launch_entries = extract_news_entries(launches_answer)
    finance_answer = select_new_news_answer(finance.get("answer", ""), history + launch_entries, "AI Finance")
    finance_entries = extract_news_entries(finance_answer)
    return (
        launches,
        finance,
        parse_news_json(launches_answer, "AI Developments", window_start_dt, window_end_dt),
        parse_news_json(finance_answer, "AI Finance", window_start_dt, window_end_dt),
        launch_entries,
        finance_entries,
    )


def load_news_history() -> list[dict[str, str]]:
    try:
        payload = json.loads(NEWS_HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    return entries[-MAX_NEWS_HISTORY_ENTRIES:] if isinstance(entries, list) else []


def news_history_instruction(history: list[dict[str, str]]) -> str:
    return "Previously published stories are allowed when they remain among the newest qualifying stories. Do not exclude a story only because it appeared in an earlier report."


def validate_new_news(entries: list[dict[str, str]], history: list[dict[str, str]], block_name: str) -> None:
    """Kept for compatibility; historical repeats are intentionally allowed."""
    return None


def save_news_history(history: list[dict[str, str]], new_entries: list[dict[str, str]]) -> None:
    combined = history + new_entries
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in combined:
        key = normalize_news_key(entry.get("source_url", "")) or normalize_news_key(entry.get("title", ""))
        if key and key not in seen:
            seen.add(key)
            unique.append({"title": entry.get("title", ""), "source_url": entry.get("source_url", "")})
    NEWS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    NEWS_HISTORY_PATH.write_text(
        json.dumps({"entries": unique[-MAX_NEWS_HISTORY_ENTRIES:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def validate_stock_table(answer: str) -> None:
    missing = [ticker for _, ticker in TOP_AI_STOCKS if ticker not in answer]
    if missing:
        raise RuntimeError(f"Exa stock table is missing tickers: {', '.join(missing)}")
    if not re.search(r"20\d{2}-\d{2}-\d{2}", answer):
        raise RuntimeError("Exa stock table has no explicit market date")


def html_fragment(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
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


def stock_table_markdown(rows: list[list[str]]) -> str:
    """Render the verified stock snapshot as a Notion-compatible Markdown table."""
    header, *body = rows
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in body)
    return "\n".join(lines)


async def fetch_finnhub_stock_rows(api_key: str) -> list[list[str]]:
    """Fetch the latest price and daily change for the configured AI leaders."""
    rows = [["Company", "Ticker", "Yesterday price", "Today's price", "Change %", "Market time"]]
    async with httpx2.AsyncClient(timeout=20.0) as client:
        for company, ticker in TOP_AI_STOCKS:
            response = await client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": api_key},
            )
            if response.status_code != 200:
                raise RuntimeError(f"Finnhub quote request failed for {ticker}: HTTP {response.status_code}")
            quote = response.json()
            required = (quote.get("c"), quote.get("d"), quote.get("dp"), quote.get("t"))
            if any(value is None for value in required):
                raise RuntimeError(f"Finnhub returned incomplete quote data for {ticker}: missing price or daily change")
            today_price = float(quote["c"])
            yesterday_price = today_price - float(quote["d"])
            updated = datetime.fromtimestamp(int(quote["t"]), timezone.utc)
            rows.append([
                company,
                ticker,
                f"${yesterday_price:,.2f}",
                f"${today_price:,.2f}",
                f"{float(quote['dp']):+,.2f}%",
                f"{updated:%Y-%m-%d %H:%M UTC}",
            ])
    return rows


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


async def meta_call(
    mcp: ClientSession,
    name: str,
    arguments: dict[str, Any],
    retry_transient: bool = False,
) -> dict[str, Any]:
    for attempt in range(1, MCP_MAX_ATTEMPTS + 1):
        try:
            result = await mcp.call_tool(name, arguments)
            payload = result_json(result)
            break
        except (httpx2.TimeoutException, asyncio.TimeoutError) as exc:
            if not retry_transient or attempt == MCP_MAX_ATTEMPTS:
                raise
            print(f"[MCP] {name} attempt {attempt}/{MCP_MAX_ATTEMPTS} timed out: {type(exc).__name__}")
            print(f"[MCP] retrying in {MCP_RETRY_DELAY_SECONDS} seconds")
            await asyncio.sleep(MCP_RETRY_DELAY_SECONDS)
    if payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            details = " | ".join(
                str(error[key]) for key in ("message", "detail", "code", "status")
                if error.get(key) is not None
            )
        else:
            details = str(error)
        if name == "COMPOSIO_MULTI_EXECUTE_TOOL":
            raw_details = result_text(result)
            raw_details = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", raw_details)
            raw_details = re.sub(r"https?://\S+", "[url]", raw_details)
            if raw_details and raw_details not in details:
                details = f"{details} | {raw_details[:1400]}"
        raise RuntimeError(f"{name} failed: {details[:2000]}")
    return nested_data(payload)


async def send_email_with_retries(
    mcp: ClientSession,
    composio_session_id: str,
    gmail_account: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Retry transient/timeout failures without recreating the Notion report."""
    last_error: Exception | None = None
    for attempt in range(1, EMAIL_MAX_ATTEMPTS + 1):
        try:
            print(f"[Gmail] send attempt {attempt}/{EMAIL_MAX_ATTEMPTS}")
            return await asyncio.wait_for(
                meta_call(
                    mcp,
                    "COMPOSIO_MULTI_EXECUTE_TOOL",
                    {
                        "session_id": composio_session_id,
                        "current_step": "SENDING_REPORT_EMAIL",
                        "current_step_metric": f"{attempt - 1}/{EMAIL_MAX_ATTEMPTS} emails",
                        "sync_response_to_workbench": False,
                        "thought": "Send the report from the configured Gmail sender to the configured recipient.",
                        "tools": [tool_call("GMAIL_SEND_EMAIL", gmail_account, arguments)],
                    },
                ),
                timeout=EMAIL_ATTEMPT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            last_error = exc
            print(f"[Gmail] attempt {attempt} failed: {type(exc).__name__}: {exc}")
            if attempt < EMAIL_MAX_ATTEMPTS:
                print(f"[Gmail] retrying in {EMAIL_RETRY_DELAY_SECONDS} seconds")
                await asyncio.sleep(EMAIL_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"Gmail send failed after {EMAIL_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error


async def run() -> None:
    now = datetime.now(LOCAL_TZ).replace(microsecond=0)
    window_start_dt = (now - timedelta(hours=72)).astimezone(timezone.utc)
    window_end_dt = now.astimezone(timezone.utc)
    window_start = window_start_dt.isoformat().replace("+00:00", "Z")
    window_end = window_end_dt.isoformat().replace("+00:00", "Z")

    composio = Composio(api_key=os.environ["COMPOSIO_API_KEY"])
    finnhub_api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not finnhub_api_key:
        raise RuntimeError("FINNHUB_API_KEY is not configured in GitHub Secrets")
    session = composio.sessions.create(
        user_id=os.environ["COMPOSIO_USER_ID"],
        mcp=True,
    )

    async with httpx2.AsyncClient(
        headers=session.mcp.headers,
        follow_redirects=True,
        timeout=httpx2.Timeout(
            MCP_HTTP_TIMEOUT_SECONDS,
            connect=MCP_CONNECT_TIMEOUT_SECONDS,
        ),
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
                    retry_transient=True,
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
                news_history = load_news_history()
                history_instruction = news_history_instruction(news_history)

                research_arguments = {
                        "session_id": composio_session_id,
                        "current_step": "RESEARCHING_AI_NEWS",
                        "current_step_metric": "0/2 research queries",
                        "sync_response_to_workbench": False,
                        "thought": "Retrieve fresh AI news using only Exa; stock prices are fetched separately from Finnhub.",
                        "tools": [
                            tool_call("EXA_ANSWER", exa_account, {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Using only Exa, identify up to eight of the newest qualifying international AI news stories. Focus on reporting published by authoritative United States sources, while covering major AI events anywhere in the world. Prioritize stories published today in America/Mexico_City, sorted newest to oldest by verified publication timestamp; if fewer than three qualifying stories are available today, use the newest qualifying stories from the fallback window between {window_start} and {window_end} UTC. The current time is {window_end} UTC ({now:%Y-%m-%d %H:%M} America/Mexico_City); do not return future publication timestamps, except up to 10 minutes for clock skew. Use original English-language headlines and direct original article URLs. {history_instruction} Return ONLY valid JSON, with no Markdown fences or commentary, in exactly this shape: {{\"stories\":[{{\"title\":\"...\",\"published_at\":\"YYYY-MM-DDTHH:MM:SSZ\",\"summary\":\"2-3 factual sentences in English\",\"relevance\":\"why it matters in English\",\"source_url\":\"https://original-source...\"}}]}}. Every published_at must be the verified publication timestamp of that specific article, every source_url must be the direct original English-language article URL, and every story must be inside the fallback 72-hour window. Exclude duplicates within this response. If fewer than three qualifying stories exist, return {{\"stories\":[]}}.",
                            }),
                            tool_call("EXA_ANSWER", exa_account, {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Using only Exa, identify up to eight of the newest qualifying international AI finance and business stories: investments, acquisitions, funding rounds, valuations, earnings, material partnerships, or AI strategy. Focus on reporting published by authoritative United States sources, while covering major AI companies and events anywhere in the world. Prioritize stories published today in America/Mexico_City, sorted newest to oldest by verified publication timestamp; if fewer than three qualifying stories are available today, use the newest qualifying stories from the fallback window between {window_start} and {window_end} UTC. The current time is {window_end} UTC ({now:%Y-%m-%d %H:%M} America/Mexico_City); do not return future publication timestamps, except up to 10 minutes for clock skew. Use original English-language headlines and direct original article URLs. {history_instruction} Return ONLY valid JSON, with no Markdown fences or commentary, in exactly this shape: {{\"stories\":[{{\"title\":\"...\",\"published_at\":\"YYYY-MM-DDTHH:MM:SSZ\",\"summary\":\"2-3 factual sentences in English\",\"relevance\":\"financial relevance in English\",\"source_url\":\"https://original-source...\"}}]}}. Every published_at must be the verified publication timestamp of that specific article, every source_url must be the direct original English-language article URL, and every story must be inside the fallback 72-hour window. Exclude duplicates within this response. If fewer than three qualifying stories exist, return {{\"stories\":[]}}.",
                            }),
                        ],
                    }
                research_queries = [
                    tool["arguments"]["query"]
                    for tool in research_arguments["tools"]
                ]
                research_focuses = [
                    "Prioritize different companies and stories than the first attempt, especially recent product launches and model releases.",
                    "Prioritize developer tools, open-source models, safety, robotics, chips, and enterprise AI from authoritative sources.",
                    "Use a different set of authoritative United States sources while keeping direct original article URLs and verified timestamps.",
                    "Broaden the United States source mix while keeping the newest qualifying stories and verified timestamps.",
                    "Return the freshest qualifying stories available today from authoritative United States sources; previous-report repetition is allowed.",
                ]
                for research_attempt in range(1, MCP_MAX_ATTEMPTS + 1):
                    retry_focus = research_focuses[research_attempt - 1]
                    for tool, base_query in zip(research_arguments["tools"], research_queries):
                        tool["arguments"]["query"] = f"{base_query} {retry_focus} This is search attempt {research_attempt} of {MCP_MAX_ATTEMPTS}."
                    research = await meta_call(
                        mcp,
                        "COMPOSIO_MULTI_EXECUTE_TOOL",
                        research_arguments,
                        retry_transient=True,
                    )
                    try:
                        (
                            launches,
                            finance,
                            launches_text,
                            finance_text,
                            launch_entries,
                            finance_entries,
                        ) = prepare_research_payload(
                            research,
                            news_history,
                            window_start_dt,
                            window_end_dt,
                        )
                        break
                    except RuntimeError as exc:
                        if "fewer than three distinct" not in str(exc) or research_attempt == MCP_MAX_ATTEMPTS:
                            raise
                        print(f"[Exa] research attempt {research_attempt}/{MCP_MAX_ATTEMPTS} lacked three distinct stories")
                        print(f"[Exa] retrying in {EXA_RESEARCH_RETRY_DELAY_SECONDS} seconds")
                        await asyncio.sleep(EXA_RESEARCH_RETRY_DELAY_SECONDS)
                stock_rows = await fetch_finnhub_stock_rows(finnhub_api_key)
                stock_html = stock_table_html(stock_rows)
                stock_markdown = stock_table_markdown(stock_rows)
                citations = (launches.get("citations", []) or []) + (finance.get("citations", []) or [])
                citation_lines = "\n".join(
                    f"- [{item.get('title', 'Source')}]({item.get('url')})"
                    for item in citations[:10]
                    if item.get("url")
                )

                title = f"IA daily report for Rogelio Espinosa — {now:%Y-%m-%d}"
                markdown = (
                    f"# IA daily report for Rogelio Espinosa — {now:%Y-%m-%d} {{color=\"blue\"}}\n\n"
                    "<callout icon=\"🔷\" color=\"blue_bg\">\n**AI DAILY BRIEF**\nMarket intelligence, AI developments and financial signals\n</callout>\n\n"
                    f"_Report generated on {now:%Y-%m-%d} at {now:%H:%M} in Monterrey, Nuevo León, Mexico._\n\n"
                    "---\n\n"
                    "## Section 1 — AI developments {color=\"blue_bg\"}\n\n"
                    "<callout icon=\"🔷\" color=\"blue_bg\">\n**Top signal**\nThe most relevant AI product, model or capability developments from the last 72 hours.\n</callout>\n\n"
                    f"{launches_text}\n\n"
                    "---\n\n"
                    "## Section 2 — AI finance {color=\"blue_bg\"}\n\n"
                    "<callout icon=\"🔷\" color=\"blue_bg\">\n**Top financial signal**\nThe most relevant AI investments, acquisitions, earnings and business strategy signals.\n</callout>\n\n"
                    f"{finance_text}\n\n"
                    "---\n\n"
                    "## Section 3 — AI leaders stock prices (USD) {color=\"blue_bg\"}\n\n"
                    "<callout icon=\"🔷\" color=\"blue_bg\">\n**Market snapshot**\nDaily movement for the selected AI leaders, with prices shown in USD.\n</callout>\n\n"
                    f"{stock_markdown}\n\n"
                    "Source: [Finnhub Quote API](https://finnhub.io/docs/api/quote)\n\n"
                    "### Sources searched through Exa\n\n"
                    f"{citation_lines}\n\n"
                    f"_Search performed exclusively with Exa, prioritizing today’s newest stories from authoritative United States sources and using the last 72 hours as fallback._"
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
                    retry_transient=True,
                )
                notion_response = notion_result.get("results", [{}])[0].get("response", {})
                if not notion_response.get("successful"):
                    raise RuntimeError(f"Notion create failed: {notion_result}")
                page = notion_response.get("data", {})
                page_url = find_value(page, {"url", "public_url"})
                page_url = page_url or "(URL unavailable)"

                email_body = (
                    f"IA daily report for Rogelio Espinosa — {now:%Y-%m-%d}\n\n"
                    "🟣 AI DAILY BRIEF\n"
                    f"Report generated on {now:%Y-%m-%d} at {now:%H:%M} in Monterrey, Nuevo León, Mexico.\n\n"
                    f"Open full report in Notion: {page_url}\n\n"
                    "SECTION 1 — AI DEVELOPMENTS\n\n"
                    f"{launches_text}\n\n"
                    "SECTION 2 — AI FINANCE\n\n"
                    f"{finance_text}\n\n"
                    "SECTION 3 — AI LEADERS STOCK PRICES (USD)\n\n"
                    "See the formatted table below.\n\n"
                    "Sources searched through Exa:\n"
                    f"{citation_lines}"
                )
                email_body_html = (
                    '<div style="border-left:6px solid #1F4E79;padding:18px 22px;background:#1F4E79;margin-bottom:20px">'
                    f"<h1 style=\"color:#FFFFFF;margin:0 0 8px\">IA daily report for Rogelio Espinosa — {now:%Y-%m-%d}</h1>"
                    '<p style="margin:0;color:#FFFFFF;font-weight:bold;letter-spacing:.08em">AI DAILY BRIEF</p>'
                    "</div>"
                    f"<p>Report generated on {now:%Y-%m-%d} at {now:%H:%M} in Monterrey, Nuevo León, Mexico.</p>"
                    f'<p><strong>🔗 <a href="{html.escape(page_url)}">Open full report in Notion</a></strong></p>'
                    '<div style="border-left:6px solid #1F4E79;padding:10px 16px;background:#1F4E79;margin-top:20px"><h2 style="color:#FFFFFF;margin:0">Section 1 — AI developments</h2><p style="margin:6px 0 0;color:#FFFFFF"><strong>Top signal</strong></p></div>'
                    f"<p>{html_fragment(launches_text)}</p>"
                    '<div style="border-left:6px solid #1F4E79;padding:10px 16px;background:#1F4E79;margin-top:20px"><h2 style="color:#FFFFFF;margin:0">Section 2 — AI finance</h2><p style="margin:6px 0 0;color:#FFFFFF"><strong>Top financial signal</strong></p></div>'
                    f"<p>{html_fragment(finance_text)}</p>"
                    '<div style="border-left:6px solid #1F4E79;padding:10px 16px;background:#1F4E79;margin-top:20px"><h2 style="color:#FFFFFF;margin:0">Section 3 — AI leaders stock prices (USD)</h2><p style="margin:6px 0 0;color:#FFFFFF"><strong>Market snapshot</strong></p></div>'
                    f"{stock_html}"
                    '<p>Source: <a href="https://finnhub.io/docs/api/quote">Finnhub Quote API</a></p>'
                    "<p><strong>Sources searched through Exa:</strong><br>"
                    f"{html_fragment(citation_lines)}</p>"
                )
                email_result = await send_email_with_retries(
                    mcp,
                    composio_session_id,
                    gmail_account,
                    {
                        "from_email": GMAIL_FROM,
                        "recipient_email": GMAIL_TO,
                        "subject": f"AI Daily Brief — {now:%Y-%m-%d}",
                        "body": email_body_html,
                        "is_html": True,
                    },
                )
                email_response = email_result.get("results", [{}])[0].get("response", {})
                if not email_response.get("successful"):
                    raise RuntimeError(f"Gmail send failed: {email_result}")
                email_id = find_value(email_response.get("data", {}), {"id", "message_id", "threadId", "thread_id"})
                if not email_id:
                    raise RuntimeError(f"Gmail returned no message id: {email_result}")

                save_news_history(news_history, launch_entries + finance_entries)

                print(json.dumps({"notion_url": page_url, "email_id": email_id, "sent_to": GMAIL_TO}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run())
