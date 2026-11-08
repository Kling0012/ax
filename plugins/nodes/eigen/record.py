from runtime.subloop.nodeapi import eject

schema = {"type": "object", "additionalProperties": False}
META = {"statuses": ["ok", "skip", "error"]}


async def run(ctx, inp):
    return eject("scheduled_publisher_unbound", todo={"title": "Bind scheduled publisher", "state": "parked", "owner": "system", "reason": "unbound"})
