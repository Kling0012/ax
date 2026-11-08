"""B7 graph binding. B16b assembly supplies the environment/worker/gate ports."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import re
from zoneinfo import ZoneInfo

from axcommon import timeutil
from runtime.subloop.nodeapi import error, ok

from .contracts import Target


def prepare_manifest(manifest, effective_knobs):
    """Apply restart-effective JST time to B7's UTC cron before building Executor."""
    result = deepcopy(manifest)
    if result["id"] == "x-posts-draft":
        value = effective_knobs.get("u_x_draft_time", "07:00")
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("invalid JST draft time")
        hour, minute = map(int, value.split(":"))
        result["schedule"]["cron"] = f"{minute} {(hour - 9) % 24} * * *"
        result["knobs"]["u_x_draft_time"]["default"] = value
    return result


class RuntimeBinding:
    def __init__(self, engine, *, accounts: dict[str, str], queries: tuple[str, ...] = ()):
        self.engine, self.accounts, self.queries = engine, dict(accounts), queries

    def target(self, data):
        destination = data["destination"]
        return Target("x-posts", data["project"], data["repo"], destination, self.accounts[destination])

    async def collect(self, ctx, *, phase="queue"):
        if phase == "draft":
            expected = prepare_manifest(ctx.manifest, ctx.knobs)["schedule"]
            if expected != ctx.manifest["schedule"]:
                return error("draft_schedule_not_applied", "投稿案の時刻を反映して再起動する必要がある")
        targets = await ctx.call(self.engine.inventory)
        run = ctx.store.get_run(ctx.run_id)
        scheduled_at = run.get("planned_at") or run.get("started_at")
        if not scheduled_at:
            return error("planned_time_missing", "予定時刻を取得できない")
        planned = timeutil.parse_ts(scheduled_at).astimezone(ZoneInfo("Asia/Tokyo"))
        if timeutil.parse_ts(scheduled_at) < timeutil.parse_ts(self.engine.started_at):
            return ok({"items": [], "reason": "schedule_missed"})
        collect_due = phase == "draft"
        items = []
        for index, data in enumerate(targets):
            if "投稿" not in data["operations"] or data["destination"] not in self.accounts:
                continue
            target = self.target(data)
            observation_id = None
            if collect_due:
                result = await ctx.call(self.engine.collect, target, planned.date().isoformat(),
                                        scheduled_at=scheduled_at, queries=self.queries, generate=False)
                if result["result"] == "collected" and result["input_ids"]:
                    observation_id = result["input_ids"][0]
            # collect_only may observe candidates and inventory but does not create runnable items.
            if ctx.mode == "run" and self.engine._mode(target) == "run":
                items.append({"item_id": f"{ctx.run_id}-x-{index}", "project": target.project,
                              "repo": target.repo, "destination": target.destination,
                              "scheduled_at": scheduled_at, "observation_id": observation_id})
        return ok({"items": items})

    async def draft(self, ctx):
        data = ctx.item.get("data", ctx.item)
        observation = data.get("observation_id")
        if not observation:
            return ok({"result": "skipped", "reason": "not_draft_schedule"})
        result = await ctx.call(self.engine.draft, self.target(data), observation,
                                scheduled_at=data["scheduled_at"])
        # A failed proposal does not block delivery of other, already checked candidates.
        return ok(result)

    async def send(self, ctx):
        target = self.target(ctx.item.get("data", ctx.item))
        loop = asyncio.get_running_loop()

        def mark_launching(candidate_id):
            future = asyncio.run_coroutine_threadsafe(
                ctx.launching("x-post:" + candidate_id, {"candidate_id": candidate_id}), loop)
            future.result()

        result = await ctx.call(self.engine.send_one, target, before_post=mark_launching)
        if result.get("state") == "unknown":
            return error("x_send_unknown", "送信結果の照合が必要", data=result)
        return ok(result)

    async def reconcile(self, ctx, key):
        if not key.startswith("x-post:"):
            return None
        target = self.target(ctx.item.get("data", ctx.item))
        await ctx.call(self.engine.recover)
        result = await ctx.call(self.engine.reconcile, target, key.split(":", 1)[1])
        if result.get("state") == "sent":
            return {"found": True, "out": ok(result)}
        if result.get("state") == "failed":
            return {"found": True, "out": error("x_not_sent", "未送信を確認", expected=True, data=result)}
        return None
