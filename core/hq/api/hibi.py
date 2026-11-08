"""S-22.6/F-25: read-only, allowlisted Hibi views from the core ledger port."""
from __future__ import annotations

from axcommon import timeutil
from .service import JST, day

STATES = frozenset({"away", "play", "focus", "break", "shallow"})
CATEGORIES = frozenset({"work", "research", "communication", "media", "play", "other"})


class HibiViews:
    def __init__(self, api):
        self.api = api

    def _fresh(self, value) -> bool:
        try:
            age = (self.api.now() - timeutil.parse_ts(value["as_of"])).total_seconds()
            maximum = 3 * self.api.defaults.get("u_hibi_interval_s")
            future = 60 * self.api.defaults.get("hibi_future_skew_min")
            return -future <= age <= maximum
        except Exception:
            return False

    def _get(self, path):
        try:
            data = self.api.ledger.get(path)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _day_data(self, target_date):
        data = self._get(f"/v1/daily-state/{day(target_date)}")
        if data is None or not self._fresh(data) or data.get("date") != target_date or data.get("tz") != "Asia/Tokyo":
            return None
        segments = data.get("segments")
        if not isinstance(segments, list) or len(segments) > self.api.defaults.get("hibi_segments_max"):
            return None
        safe = []
        try:
            for segment in segments:
                start, end = timeutil.parse_ts(segment["start"]), timeutil.parse_ts(segment["end"])
                if end < start or segment["state"] not in STATES or segment["category"] not in CATEGORIES:
                    return None
                title = segment.get("session_title")
                if title is not None and (not isinstance(title, str) or len(title) > 40):
                    return None
                safe.append({"start": segment["start"], "end": segment["end"], "state": segment["state"],
                             "category": segment["category"], "session_title": title})
            return self.api.clean({"date": target_date, "tz": "Asia/Tokyo", "as_of": data["as_of"], "segments": safe})
        except Exception:
            return None

    @staticmethod
    def unknown_now():
        return {"state": "unknown", "since": None, "category": None, "session_title": None, "as_of": None}

    def now(self):
        focus = self._get("/v1/focus-state")
        if focus is None or not self._fresh(focus) or focus.get("state") not in STATES:
            return self.unknown_now()
        try:
            timeutil.parse_ts(focus["since"])
            result = {"state": focus["state"], "since": focus["since"], "category": None,
                      "session_title": None, "as_of": focus["as_of"]}
            daily = self._day_data(self.api.now().astimezone(JST).date().isoformat())
            if daily:
                bands = [s for s in daily["segments"] if timeutil.parse_ts(s["start"]) <= self.api.now() <= timeutil.parse_ts(s["end"])
                         and s["state"] == focus["state"]]
                if bands:
                    latest = max(bands, key=lambda s: timeutil.parse_ts(s["start"]))
                    result.update(category=latest["category"], session_title=latest["session_title"])
            return self.api.clean(result)
        except Exception:
            return self.unknown_now()

    def daily(self, target_date):
        target_date = day(target_date)
        data = self._day_data(target_date)
        if data is not None:
            return data
        # Unknown is an explicit failure state, not a successful empty day.
        return {"date": target_date, "tz": "Asia/Tokyo", "as_of": None, "segments": [], "state": "unknown"}
