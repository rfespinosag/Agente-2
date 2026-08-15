"""Daily report through Composio's hosted MCP server.

No OpenAI SDK or OpenAI API is used. Composio's MCP meta-tools discover and
execute Exa, Notion, and Gmail tools on the connected user account.
"""

from __future__ import annotations

import asyncio
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
        raise RuntimeError(f"Exa found no qualifying {block_name} stories in the last 24 hours")

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
    if len(qualifying_dates) < 3:
        raise RuntimeError(
            f"Exa returned fewer than three verifiable {block_name} publication dates "
            f"inside the last-24-hour window: {answer[:800]}"
        )


async def meta_call(mcp: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments)
    payload = result_json(result)
    if payload.get("error"):
        raise RuntimeError(f"{name} failed: {payload['error']}")
    return nested_data(payload)


async def run() -> None:
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    window_start_dt = now.astimezone(timezone.utc).replace(microsecond=0) - timedelta(hours=24)
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
                            {"use_case": "find the most relevant AI launch and capability news from the last 24 hours using only Exa"},
                            {"use_case": "find the most relevant AI finance investment acquisition and business news from the last 24 hours using only Exa"},
                            {"use_case": "create a Notion page with a financial report and source links"},
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
                        "current_step": "RESEARCHING_RATE_AND_NEWS",
                        "current_step_metric": "0/2 research queries",
                        "sync_response_to_workbench": False,
                        "thought": "Retrieve fresh, cited AI launch and AI finance news from the last 24 hours.",
                        "tools": [
                            tool_call("EXA_ANSWER", exa_account, {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Using only Exa, identify exactly three of the most relevant international news stories published between {window_start} and {window_end} (UTC) about new artificial intelligence launches, new AI developments, or new AI capabilities. Respond exclusively in Spanish. For each story provide: title, original publication date and time in UTC, a factual 2-3 sentence summary, why it matters, and a final line exactly in the form 'Fuente original: https://...'. Use only verifiable original or authoritative source URLs, exclude duplicates, and do not use older stories as substitutes. If fewer than three qualifying stories exist in that exact window, return exactly NO_QUALIFYING_NEWS.",
                            }),
                            tool_call("EXA_ANSWER", exa_account, {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Using only Exa, identify exactly three of the most relevant international news stories published between {window_start} and {window_end} (UTC) about AI finance: investments, acquisitions, funding rounds, valuations, earnings, partnerships with material financial impact, or AI business strategy. Respond exclusively in Spanish. For each story provide: title, original publication date and time in UTC, a factual 2-3 sentence summary, the financial relevance, and a final line exactly in the form 'Fuente original: https://...'. Use only verifiable original or authoritative source URLs, exclude duplicates, and do not use older stories as substitutes. If fewer than three qualifying stories exist in that exact window, return exactly NO_QUALIFYING_NEWS.",
                            }),
                        ],
                    },
                )
                results = research.get("results", [])
                if len(results) < 2:
                    raise RuntimeError(f"Research returned incomplete results: {research}")
                launches_response = results[0].get("response", {})
                finance_response = results[1].get("response", {})
                if not all(response.get("successful") for response in (launches_response, finance_response)):
                    raise RuntimeError(f"Exa research failed: {research}")
                launches = launches_response.get("data", {})
                finance = finance_response.get("data", {})
                launches_text = launches.get("answer", "")
                finance_text = finance.get("answer", "")
                validate_news_window(launches_text, "Novedades IA", window_start_dt, window_end_dt)
                validate_news_window(finance_text, "Finanzas IA", window_start_dt, window_end_dt)
                citations = (launches.get("citations", []) or []) + (finance.get("citations", []) or [])
                citation_lines = "\n".join(
                    f"- [{item.get('title', 'Source')}]({item.get('url')})"
                    for item in citations[:10]
                    if item.get("url")
                )

                title = f"Noticias globales IA de las últimas 24 hrs — {now:%Y-%m-%d}"
                markdown = (
                    "# Noticias globales IA de las últimas 24 hrs\n\n"
                    f"_Reporte del {now:%Y-%m-%d} a las {now:%H:%M} en Monterrey, Nuevo León, México._\n\n"
                    "## Novedades IA\n\n"
                    f"{launches_text}\n\n"
                    "## Finanzas IA\n\n"
                    f"{finance_text}\n\n"
                    "### Fuentes consultadas en Exa\n\n"
                    f"{citation_lines}\n\n"
                    f"_La búsqueda se realizó exclusivamente con Exa y se limitó a las últimas 24 horas._"
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
                if not page_id and not page_url:
                    raise RuntimeError(f"Notion returned no page id or URL: {notion_result}")
                page_url = page_url or "(URL unavailable)"

                email_body = (
                    "Noticias globales IA de las últimas 24 hrs\n\n"
                    f"Reporte del {now:%Y-%m-%d} a las {now:%H:%M} en Monterrey, Nuevo León, México.\n\n"
                    "BLOQUE 1 — NOVEDADES IA\n\n"
                    f"{launches_text}\n\n"
                    "BLOQUE 2 — FINANZAS IA\n\n"
                    f"{finance_text}\n\n"
                    f"Reporte completo en Notion: {page_url}\n\n"
                    "Fuentes consultadas en Exa:\n"
                    f"{citation_lines}"
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
                                "subject": f"Noticias globales IA de las últimas 24 hrs — {now:%Y-%m-%d}",
                                "body": email_body,
                                "is_html": False,
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
