"""Fake-only binding for tab identity pending F-21.2 measurement.

The field names in this fixture are test data, never a production driver map.
"""
from tests.fakes.box_fake import BoxFake
from tests.fakes.b18b.screenshot.public_page import Policy


class ScreenshotBoxFake(BoxFake):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tabs = {"user-tab"}

    def _tool(self, name, args):
        if name == "browser_navigate":
            self.tabs.add("owned-tab")
            return {"content": [], "owned_tab": "owned-tab"}
        if name == "browser_tabs" and args == {"fixture_close": "owned-tab"}:
            self.tabs.remove("owned-tab")
            return {"content": []}
        return super()._tool(name, args)


class BoxPolicy(Policy):
    def read_key(self, provider, purpose):
        assert (provider, purpose) == ("box", "ax")
        return "fixture-only-not-a-real-key"

    def check_tool_args(self, name, args):
        assert name in {"browser_navigate", "screenshot", "browser_tabs"}

    def scan_inbound(self, data):
        return self.scan_artifact(data)
