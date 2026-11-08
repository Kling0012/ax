"""The independent x-user-posts env.run entry (ENV-MODULE §14.1)."""
from __future__ import annotations

from uuid import uuid4


class UserPostsSource:
    """runner.run(request, artifacts) stages the existing env.run contract.

    decoder converts the actual xactions posts.json artifact to validated Posts.
    The raw xactions JSON shape is deliberately not guessed here.
    """

    def __init__(self, runner, *, decoder=None):
        self.runner, self.decoder = runner, decoder

    def fetch(self, target):
        if self.decoder is None:
            raise NotImplementedError("FACTS F-11.8: xactions posts.json decoding requires a verified fixture")
        job_id = "ax-discord-" + uuid4().hex
        request = {
            "job_id": job_id, "requires": ["x-session"], "sensitivity": "public",
            "entry": {"kind": "entrypoint", "name": "x-user-posts", "args": {
                "user": target.x_user, "limit": 20, "retweets": target.include_retweets}},
            "outputs": ["posts.json"], "secrets": [], "budget_min": 5,
            "stall_min": 5, "irreversible": False, "scope": "one",
            "idempotent": False, "idempotency_key": job_id,
        }
        result = self.runner.run(request, {})
        if result.state != "succeeded":
            raise RuntimeError("x_user_posts_unavailable")
        return self.decoder(result.output["posts.json"])
