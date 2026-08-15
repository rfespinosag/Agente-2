"""Daily report through Composio's hosted MCP server.

No OpenAI SDK or OpenAI API is used. Composio's MCP meta-tools discover and
execute Exa, Notion, and Gmail tools on the connected user account.
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
    raise RuntimeError(f"No active Composio account found for {toolkit}")


async def meta_call(mcp: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments)
    payload = result_json(result)
    if payload.get("error"):
        raise RuntimeError(f"{name} failed: {payload['error']}")
    return nested_data(payload)


async def run() -> None:
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    window_start = (now.astimezone(timezone.utc).timestamp() - 24 * 60 * 60)
    window_end = now.astimezone(timezone.utc).timestamp()

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
                            {"use_case": "search Exa for the current USD MXN exchange rate"},
                            {"use_case": "find exactly three international financial technology news stories from the last 24 hours using Exa"},
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
                        "thought": "Retrieve a current USD MXN rate and fresh cited technology finance news.",
                        "tools": [
                            {
                                "tool_slug": "EXA_ANSWER",
                                "account": exa_account,
                                "arguments": {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"As of {now:%Y-%m-%d %H:%M} America/Mexico_City, give the latest USD to MXN exchange rate, timestamp, and authoritative source URL.",
                                },
                            },
                            {
                                "tool_slug": "EXA_ANSWER",
                                "account": exa_account,
                                "arguments": {
                                    "model": "exa-pro",
                                    "text": False,
                                    "query": f"Identify exactly three relevant international financial news stories published between {window_start} and {window_end} UTC about major technology companies. For each, provide headline, publication timestamp, concise factual summary, financial relevance, and original source URL. Prefer authoritative sources, exclude duplicates, and do not invent dates.",
                                },
                            },
                        ],
                    },
                )
                results = research.get("results", [])
                if len(results) < 2:
                    raise RuntimeError(f"Research returned incomplete results: {research}")
                rate_response = results[0].get("response", {})
                news_response = results[1].get("response", {})
                if not rate_response.get("successful") or not news_response.get("successful"):
                    raise RuntimeError(f"Exa research failed: {research}")
                rate = rate_response.get("data", {})
                news = news_response.get("data", {})
                rate_text = rate.get("answer", "")
                news_text = news.get("answer", "")
                citations = news.get("citations", [])
                citation_lines = "\n".join(
                    f"- [{item.get('title', 'Source')}]({item.get('url')})"
                    for item in citations[:3]
                    if item.get("url")
                )

                title = f"Daily financial and technology report - {now:%Y-%m-%d}"
                markdown = (
                    f"# {title}\n\n"
                    "## USD to MXN exchange rate\n\n"
                    f"{rate_text}\n\n"
                    "## International news - last 24 hours\n\n"
                    f"{news_text}\n\n"
                    "### Original sources\n\n"
                    f"{citation_lines}\n\n"
                    f"_Checked {now:%Y-%m-%d %H:%M} America/Mexico_City._"
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
                        "tools": [{
                            "tool_slug": "NOTION_CREATE_NOTION_PAGE",
                            "account": notion_account,
                            "arguments": {"parent_id": PARENT_ID, "title": title, "icon": "💱", "markdown": markdown},
                        }],
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
                    f"Daily report - {now:%Y-%m-%d}\n\n{rate_text}\n\n{news_text}\n\n"
                    f"Full Notion report: {page_url}\n\nSources:\n{citation_lines}"
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
                        "tools": [{
                            "tool_slug": "GMAIL_SEND_EMAIL",
                            "account": gmail_account,
                            "arguments": {
                                "from_email": GMAIL_FROM,
                                "recipient_email": GMAIL_TO,
                                "subject": f"Financial report USD/MXN and tech news - {now:%Y-%m-%d}",
                                "body": email_body,
                                "is_html": False,
                            },
                        }],
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
