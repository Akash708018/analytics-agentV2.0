"""B2 measurement: does the MCP server emit notifications/tools/list_changed when a domain is
confirmed, and does a client that re-lists then see the domain tool?

Server side only. Whether Claude DESKTOP re-lists on the notification cannot be measured in a
headless container; see O-B2-1 in docs/decisions.md.
Run: uv run python bench/mcp_list_changed.py
"""
import asyncio

import mcp.types as mt
from fastmcp import Client, Context, FastMCP

server = FastMCP("gating-probe")


@server.tool(tags={"marketing"})
def marketing_channel_efficiency() -> str:
    return "ok"


@server.tool
async def confirm_domain(domain: str, ctx: Context) -> str:
    await ctx.enable_components(tags={domain})
    return f"{domain} confirmed"


server.disable(tags={"marketing"})
seen: list[str] = []


async def handler(msg) -> None:
    if isinstance(msg, mt.ServerNotification):
        seen.append(type(msg.root).__name__)


async def main() -> None:
    async with Client(server, message_handler=handler) as c:
        before = sorted(t.name for t in await c.list_tools())
        await c.call_tool("confirm_domain", {"domain": "marketing"})
        await asyncio.sleep(0.1)
        after = sorted(t.name for t in await c.list_tools())
    async with Client(server) as other:
        other_session = sorted(t.name for t in await other.list_tools())
    print("before       :", before)
    print("notifications:", seen)
    print("after        :", after)
    print("other session:", other_session)


asyncio.run(main())
