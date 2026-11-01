"""npm registry and npm download-count API."""

import json
import re
import time
import urllib.parse
from datetime import timedelta

from util import HttpError, cached_json, format_ts, http_get, month_windows, parse_ts

REGISTRY = "https://registry.npmjs.org/"
DOWNLOADS = "https://api.npmjs.org/downloads/range/"
NON_VERSION_KEYS = {"created", "modified", "unpublished"}


def fetch_packument(name):
    """Full registry document for a package (all versions + publish times), or None."""
    url = REGISTRY + urllib.parse.quote(name, safe="@")

    def fetch():
        try:
            _, _, body = http_get(url)
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        doc = json.loads(body)
        # Keep only what we need; full documents can be >10 MB.
        keep_fields = ("dependencies", "optionalDependencies", "peerDependencies",
                       "peerDependenciesMeta", "devDependencies", "bundleDependencies",
                       "bundledDependencies", "dist")
        versions = {}
        for v, meta in (doc.get("versions") or {}).items():
            entry = {k: meta[k] for k in keep_fields if k in meta}
            if "dist" in entry:
                entry["dist"] = {k: entry["dist"][k] for k in ("unpackedSize", "fileCount")
                                 if k in entry["dist"]}
            versions[v] = entry
        return {"name": doc.get("name", name), "time": doc.get("time") or {},
                "repository": doc.get("repository"), "versions": versions}

    return cached_json("npm:" + url, fetch)


def is_prerelease(version):
    return "-" in version.split("+", 1)[0]


def version_times(packument):
    """{version: publish datetime} for every version that has a publish time."""
    out = {}
    for v, t in packument["time"].items():
        if v in NON_VERSION_KEYS or not isinstance(t, str):
            continue
        try:
            out[v] = parse_ts(t)
        except ValueError:
            continue
    return out


def pick_version(packument, prediction):
    """The version a user would have installed at the prediction date: the most recently
    published stable version before it (falling back to a prerelease if no stable exists)."""
    times = version_times(packument)
    candidates = [(t, v) for v, t in times.items()
                  if t < prediction and v in packument["versions"]]
    stable = [c for c in candidates if not is_prerelease(c[1])]
    pool = stable or candidates
    if not pool:
        return None, None
    t, v = max(pool)
    return v, t


def purl_for(name, version):
    """Package URL per the purl spec: pkg:npm/%40scope/name@version"""
    if name.startswith("@") and "/" in name:
        scope, pkg = name[1:].split("/", 1)
        path = "%40" + urllib.parse.quote(scope, safe="") + "/" + urllib.parse.quote(pkg, safe="")
    else:
        path = urllib.parse.quote(name, safe="")
    return f"pkg:npm/{path}@{urllib.parse.quote(version, safe='.-_~+')}"


def base_features(packument, version):
    meta = packument["versions"][version]
    dist = meta.get("dist") or {}

    def nkeys(field):
        value = meta.get(field)
        return len(value) if isinstance(value, dict) else 0

    peers = set(meta.get("peerDependencies") or {})
    for peer, info in (meta.get("peerDependenciesMeta") or {}).items():
        if isinstance(info, dict) and info.get("optional"):
            peers.add(peer)

    bundled = meta.get("bundleDependencies", meta.get("bundledDependencies"))
    if bundled is True:  # "bundle all dependencies"
        bundled_count = nkeys("dependencies")
    elif isinstance(bundled, list):
        bundled_count = len({b for b in bundled if isinstance(b, str)})
    else:
        bundled_count = 0

    def nonneg_int(x):
        return x if isinstance(x, int) and x >= 0 else None

    return {
        "unpacked_size": nonneg_int(dist.get("unpackedSize")),
        "file_count": nonneg_int(dist.get("fileCount")),
        "dependency_count": nkeys("dependencies"),
        "optional_dependency_count": nkeys("optionalDependencies"),
        "peer_dependency_count": len(peers),
        "dev_dependency_count": nkeys("devDependencies"),
        "bundled_dependency_count": bundled_count,
    }


def stable_release_counts(packument, prediction):
    counts = [0] * 12
    windows = month_windows(prediction)
    for v, t in version_times(packument).items():
        if is_prerelease(v):
            continue
        for m, (start, end) in enumerate(windows):
            if start <= t < end:
                counts[m] += 1
    return counts


def package_meta(packument, version, published):
    created = packument["time"].get("created")
    return {
        "package_created_at": format_ts(parse_ts(created)) if created else None,
        "published_at": format_ts(published),
    }


def fetch_daily_downloads(name, start, end):
    """{'YYYY-MM-DD': downloads} for start <= day < end, or None if unavailable.
    The API allows at most 18 months per request, so the range is split into chunks."""
    days = {}
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=500), end)
        rng = f"{chunk_start:%Y-%m-%d}:{(chunk_end - timedelta(days=1)):%Y-%m-%d}"
        url = DOWNLOADS + rng + "/" + urllib.parse.quote(name, safe="@/")

        def fetch(url=url):
            time.sleep(1.0)  # the downloads API rate-limits bursts of requests
            try:
                _, _, body = http_get(url)
            except HttpError as e:
                if e.status in (400, 404):
                    return {"error": e.body[:200]}
                raise
            return json.loads(body)

        data = cached_json("downloads:" + url, fetch)
        if "downloads" not in data:
            return None
        for item in data["downloads"]:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.get("day", "")) and \
                    isinstance(item.get("downloads"), int):
                days[item["day"]] = item["downloads"]
        chunk_start = chunk_end
    return days


def download_series(daily, prediction):
    """(downloads per month, observed days per month) for M0..M11; None where unavailable."""
    if daily is None:
        return [None] * 12, [None] * 12
    totals, observed = [], []
    for start, end in month_windows(prediction):
        n_days, total = 0, 0
        day = start
        while day < end:
            key = f"{day:%Y-%m-%d}"
            if key in daily:
                n_days += 1
                total += daily[key]
            day += timedelta(days=1)
        totals.append(total if n_days else None)
        observed.append(n_days)
    return totals, observed
