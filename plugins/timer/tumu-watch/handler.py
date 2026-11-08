"""Execute Tumu monitoring through the configured timer client."""
import asyncio


async def run(ctx):
    return await asyncio.to_thread(ctx.clients['tumu_watch'].run, ctx)
