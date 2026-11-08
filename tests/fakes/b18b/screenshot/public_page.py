"""In-memory public-page fixtures: no socket, subprocess or real browser."""
import base64
import hashlib
import json
import struct
import zlib


def png_bytes(width=1280, height=800):
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress((b"\0" + b"\xfa\xfb\xfc" * width) * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class PublicPageDriver:
    def __init__(self, clock, image=None):
        self.clock = clock
        self.image = png_bytes() if image is None else image
        self.calls = []
        self.delay = 0
        self.no_image = False
        self.error = False
        self.tabs = {"user-tab"}

    def call_tool(self, name, args, timeout_s):
        self.calls.append((name, args, timeout_s))
        if name == "browser_navigate":
            self.tabs.add("owned-tab")
            return {"content": [], "owned_tab": "owned-tab"}
        if self.delay:
            self.clock.now += min(timeout_s, self.delay)
            if self.delay >= timeout_s:
                raise TimeoutError("fake delay")
        if self.error:
            return {"isError": True, "content": []}
        if self.no_image:
            return {"content": []}
        return {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(self.image).decode()}]}

    def close_owned_tab(self, response, timeout_s):
        self.calls.append(("close_owned_tab", {"tab": response["owned_tab"]}, timeout_s))
        self.tabs.remove(response["owned_tab"])


class FakeRunner:
    def __init__(self, image=None):
        image = png_bytes() if image is None else image
        self.payloads = {"out/page.png": image, "out/meta.json": json.dumps({
            "final_url": "https://example.com/", "title": "Public fixture", "http_status": 200,
            "width": 1280, "height": 800, "captured_at": "2026-11-07T00:00:00Z",
            "mode": "fullpage", "ignored_args": []}).encode()}
        self.calls = []
        self.on_start = lambda: None
        self.short_read = False
        self.bad_hash = False

    def run_entry(self, args, timeout_s):
        self.calls.append(("shell:start", args, timeout_s))
        self.on_start()
        meta = json.loads(self.payloads["out/meta.json"])
        meta["mode"] = "fullpage" if args["full_page"] else "viewport"
        self.payloads["out/meta.json"] = json.dumps(meta).encode()
        return {name: {"size": len(data), "sha256": "0" * 64 if self.bad_hash else hashlib.sha256(data).hexdigest()}
                for name, data in self.payloads.items()}

    def fetch(self, name, offset, size, timeout_s):
        self.calls.append(("shell:fetch", name, offset, size, timeout_s))
        result = self.payloads[name][offset:offset + size]
        return result[:-1] if self.short_read else result


class Policy:
    def __init__(self):
        self.tools = []
        self.outbound = []
        self.artifacts = []
        self.deny = None
        self.secret_marker = "fixture-credential-marker"

    def check_tool(self, name, args):
        self.tools.append((name, args))
        if name not in {"browser_navigate", "screenshot"} or name == self.deny:
            from core.resources.screenshot.entry_expand import ScreenshotError
            raise ScreenshotError("tool_denied")

    def scan_outbound(self, value):
        self.outbound.append(value)
        if self.secret_marker in json.dumps(value):
            from core.resources.screenshot.entry_expand import ScreenshotError
            raise ScreenshotError("secret_detected")

    def scan_artifact(self, data):
        self.artifacts.append(data)
        if self.secret_marker.encode() in data:
            from core.resources.screenshot.entry_expand import ScreenshotError
            raise ScreenshotError("secret_detected")
