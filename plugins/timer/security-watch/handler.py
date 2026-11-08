"""Security audit timer; notification delivery remains independently tracked."""
import asyncio


async def run(ctx):
    return await asyncio.to_thread(ctx.clients['security_watch'].run, ctx)
