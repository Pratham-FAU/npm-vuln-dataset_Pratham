"""GitHub-reviewed security advisories (GHSAs) for npm, taken from the OSV.dev bulk export.

OSV mirrors the GitHub Advisory Database. Its npm export is one public zip file, so no
GitHub token is needed. Only GHSA-* records with github_reviewed = true are used.
"""

import json
import zipfile
from collections import defaultdict

from util import CACHE_DIR, OUTCOME, http_get, month_windows, parse_ts

OSV_NPM_ZIP = "https://osv-vulnerabilities.storage.googleapis.com/npm/all.zip"
SEVERITY_MAP = {"LOW": "low", "MODERATE": "moderate", "MEDIUM": "moderate",
                "HIGH": "high", "CRITICAL": "critical"}


def load_advisories(refresh=False):
    """{package_name: [advisory, ...]} where advisory = dict(id, published, withdrawn, severity)."""
    CACHE_DIR.mkdir(exist_ok=True)
    zip_path = CACHE_DIR / "osv_npm_all.zip"
    if refresh or not zip_path.exists():
        print("Downloading OSV npm advisory export (~200 MB, one time)...")
        _, _, body = http_get(OSV_NPM_ZIP)
        zip_path.write_bytes(body)

    by_package = defaultdict(list)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.startswith("GHSA-"):
                continue  # skip MAL-* (malware reports) and other sources
            rec = json.loads(zf.read(name))
            extra = rec.get("database_specific") or {}
            if not extra.get("github_reviewed"):
                continue
            adv = {
                "id": rec["id"],
                "published": parse_ts(rec["published"]),
                "withdrawn": parse_ts(rec["withdrawn"]) if rec.get("withdrawn") else None,
                "severity": SEVERITY_MAP.get(str(extra.get("severity", "")).upper(), "unknown"),
            }
            packages = {a["package"]["name"] for a in rec.get("affected", [])
                        if a.get("package", {}).get("ecosystem") == "npm"}
            for pkg in packages:
                by_package[pkg].append(adv)
    return dict(by_package)


def _valid_at(adv, moment):
    """Advisory exists (not withdrawn) at the given moment."""
    return adv["withdrawn"] is None or adv["withdrawn"] > moment


def advisory_features(advisories, prediction):
    """Historical counts, monthly counts and 90-day targets for one package."""
    sevs = ["low", "moderate", "high", "critical", "unknown"]
    out = {f"ghsa_{s}_count": 0 for s in sevs}
    for s in sevs:
        for m in range(12):
            out[f"ghsa_{s}_count_M{m}"] = 0
        out[f"target_ghsa_{s}_count_90d"] = 0

    windows = month_windows(prediction)
    outcome_end = prediction + OUTCOME
    seen_hist, seen_target = set(), set()
    for adv in advisories:
        sev = adv["severity"]
        pub = adv["published"]
        if pub < prediction and _valid_at(adv, prediction) and adv["id"] not in seen_hist:
            seen_hist.add(adv["id"])
            out[f"ghsa_{sev}_count"] += 1
            for m, (start, end) in enumerate(windows):
                if start <= pub < end:
                    out[f"ghsa_{sev}_count_M{m}"] += 1
        if prediction <= pub < outcome_end and _valid_at(adv, outcome_end) \
                and adv["id"] not in seen_target:
            seen_target.add(adv["id"])
            out[f"target_ghsa_{sev}_count_90d"] += 1
    return out
