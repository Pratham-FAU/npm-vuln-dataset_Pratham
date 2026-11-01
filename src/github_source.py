"""GitHub issue and pull request activity, reconstructed per month.

Data used (REST API, needs a token in GITHUB_TOKEN for realistic rate limits):
  * /repos/{repo}/issues?state=all&since=S      every issue/PR updated since S
                                               (gives creation times and current state)
  * /repos/{repo}/issues/events                 closed / reopened / merged events,
                                               newest first, paged back to S
  * /search/issues                              count of items still open that were
                                               not touched since S
From these, monthly opened / closed / reopened / merged counts and the number of
items open at each month end are rebuilt. Months the data does not fully cover stay NULL.
"""

import json
import os
import re
import time
import urllib.parse

from util import HttpError, cached_json, format_ts, http_get, month_windows, parse_ts

API = "https://api.github.com"
SERIES = ["issues_opened", "issues_closed", "issues_reopened", "pull_requests_opened",
          "pull_requests_closed_unmerged", "pull_requests_merged", "pull_requests_reopened",
          "issues_open_at_month_end", "pull_requests_open_at_month_end"]


def parse_github_repo(repository):
    """'owner/repo' from an npm repository field, or None if it is not on GitHub."""
    url = repository.get("url") if isinstance(repository, dict) else repository
    if not isinstance(url, str):
        return None
    url = url.strip()
    m = re.match(r"^(?:github:)?([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
    if m and ":" not in url.replace("github:", ""):
        return f"{m.group(1)}/{m.group(2)}"
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


class GitHub:
    def __init__(self, token, max_issue_pages=100, max_event_pages=150):
        self.token = token
        self.max_issue_pages = max_issue_pages
        self.max_event_pages = max_event_pages

    def _get(self, url):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        while True:
            try:
                return http_get(url, headers)
            except HttpError as e:
                if e.status in (403, 429) and "rate limit" in e.body.lower():
                    self._wait_for_reset(url)
                    continue
                raise

    def _wait_for_reset(self, url):
        kind = "search" if "/search/" in url else "core"
        _, _, body = http_get(f"{API}/rate_limit", {"Authorization": f"Bearer {self.token}"}
                              if self.token else {})
        reset = json.loads(body)["resources"][kind]["reset"]
        wait = max(reset - time.time(), 0) + 2
        print(f"    GitHub {kind} rate limit reached, waiting {wait / 60:.1f} min ...")
        time.sleep(wait)

    def _pages(self, url, max_pages, stop=None):
        """Yield items page by page. Returns (items, complete)."""
        items = []
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in url else "?"
            try:
                _, _, body = self._get(f"{url}{sep}per_page=100&page={page}")
            except HttpError as e:
                if e.status == 422:  # GitHub refuses to page this deep
                    return items, False
                raise
            batch = json.loads(body)
            items.extend(batch)
            if len(batch) < 100 or (stop and stop(batch)):
                return items, True
        return items, False

    def _search_count(self, query):
        time.sleep(2.2 if self.token else 6.5)  # search API: 30/min with token, 10/min without
        _, _, body = self._get(f"{API}/search/issues?per_page=1&q=" + urllib.parse.quote(query))
        return json.loads(body)["total_count"]

    def fetch_activity(self, repo, since):
        """Raw activity for a repository since `since` (cached on disk)."""
        key = f"github:{repo}:{format_ts(since)}:{self.max_issue_pages}:{self.max_event_pages}"
        return cached_json(key, lambda: self._fetch_activity(repo, since))

    def _fetch_activity(self, repo, since):
        try:
            issues, issues_complete = self._pages(
                f"{API}/repos/{repo}/issues?state=all&sort=updated&direction=desc"
                f"&since={format_ts(since)}", self.max_issue_pages)
        except HttpError as e:
            if e.status in (404, 410, 451):  # repo deleted, private, or issues disabled
                return {"error": f"HTTP {e.status}"}
            raise

        def reached_since(batch):
            return bool(batch) and parse_ts(batch[-1]["created_at"]) < since

        events, events_complete = self._pages(
            f"{API}/repos/{repo}/issues/events", self.max_event_pages, stop=reached_since)
        if events_complete or not events:
            coverage_start = format_ts(since)
        else:
            coverage_start = events[-1]["created_at"]

        day = f"{since:%Y-%m-%d}"
        untouched_open_issues = self._search_count(f"repo:{repo} is:issue is:open updated:<{day}")
        untouched_open_prs = self._search_count(f"repo:{repo} is:pr is:open updated:<{day}")

        return {
            "issues": [{"number": i["number"], "is_pr": "pull_request" in i,
                        "created_at": i["created_at"], "state": i["state"]} for i in issues],
            "issues_complete": issues_complete,
            "events": [{"number": ev["issue"]["number"], "is_pr": "pull_request" in ev["issue"],
                        "event": ev["event"], "created_at": ev["created_at"]}
                       for ev in events if ev.get("event") in ("closed", "reopened", "merged")
                       and ev.get("issue")],
            "coverage_start": coverage_start,
            "untouched_open_issues": untouched_open_issues,
            "untouched_open_prs": untouched_open_prs,
        }


def github_features(activity, prediction):
    """Monthly GitHub features M0..M11 for one prediction date. NULL where not covered."""
    out = {f"{s}_M{m}": None for s in SERIES for m in range(12)}
    if not activity or "error" in activity:
        return out

    windows = month_windows(prediction)
    coverage = parse_ts(activity["coverage_start"])
    items = [dict(i, created=parse_ts(i["created_at"])) for i in activity["issues"]]
    events = [dict(e, at=parse_ts(e["created_at"])) for e in activity["events"]]

    # Merge events let us tell "closed by merging" apart from "closed without merging".
    merges = {}
    for e in events:
        if e["event"] == "merged":
            merges.setdefault(e["number"], []).append(e["at"])

    def closed_by_merge(e):
        return any(abs((t - e["at"]).total_seconds()) <= 120 for t in merges.get(e["number"], []))

    events_by_item = {}
    for e in events:
        if e["event"] in ("closed", "reopened"):
            events_by_item.setdefault(e["number"], []).append(e)
    for evs in events_by_item.values():
        evs.sort(key=lambda e: e["at"])

    def open_at(item, moment):
        """Was the item open just before `moment`? Walk back from its current state."""
        later = [e for e in events_by_item.get(item["number"], []) if e["at"] >= moment]
        if later:
            return later[0]["event"] == "closed"
        return item["state"] == "open"

    for m, (start, end) in enumerate(windows):
        if activity["issues_complete"]:
            created = [i for i in items if start <= i["created"] < end]
            out[f"issues_opened_M{m}"] = sum(not i["is_pr"] for i in created)
            out[f"pull_requests_opened_M{m}"] = sum(i["is_pr"] for i in created)

        if coverage <= start:
            month_events = [e for e in events if start <= e["at"] < end]
            out[f"issues_closed_M{m}"] = sum(e["event"] == "closed" and not e["is_pr"]
                                             for e in month_events)
            out[f"issues_reopened_M{m}"] = sum(e["event"] == "reopened" and not e["is_pr"]
                                               for e in month_events)
            out[f"pull_requests_reopened_M{m}"] = sum(e["event"] == "reopened" and e["is_pr"]
                                                      for e in month_events)
            out[f"pull_requests_merged_M{m}"] = sum(e["event"] == "merged" for e in month_events)
            out[f"pull_requests_closed_unmerged_M{m}"] = sum(
                e["event"] == "closed" and e["is_pr"] and not closed_by_merge(e)
                for e in month_events)

        if activity["issues_complete"] and coverage <= end:
            open_issues = activity["untouched_open_issues"]
            open_prs = activity["untouched_open_prs"]
            for i in items:
                if i["created"] < end and open_at(i, end):
                    if i["is_pr"]:
                        open_prs += 1
                    else:
                        open_issues += 1
            out[f"issues_open_at_month_end_M{m}"] = open_issues
            out[f"pull_requests_open_at_month_end_M{m}"] = open_prs
    return out


def token_from_env():
    return os.environ.get("GITHUB_TOKEN") or None
