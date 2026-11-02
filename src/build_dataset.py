"""Build the training dataset: one row per npm package version at a prediction date.

Usage (from the repository root):
    python3 src/build_dataset.py                    # default: 300 packages, 9 prediction dates
    python3 src/build_dataset.py --packages 50      # smaller, faster run
    GITHUB_TOKEN=... python3 src/build_dataset.py   # also fill the GitHub issue/PR columns

Outputs:
    data/training_data.sqlite   table package_version_record
    data/training_data.csv      same rows as CSV
    data/selected_packages.txt  the package sample that was used
    schema.sql                  the CREATE TABLE statement
"""

import argparse
import csv
import random
import sqlite3
import sys
from datetime import datetime, timezone

import schema
from advisories import advisory_features, load_advisories
from github_source import GitHub, github_features, parse_github_repo, token_from_env
from npm_source import (base_features, download_series, fetch_daily_downloads,
                        fetch_packument, package_meta, pick_version, purl_for,
                        stable_release_counts)
from util import OUTCOME, ROOT, add_months, format_ts, utc_date

DATA_DIR = ROOT / "data"


def default_prediction_dates(today, count=9, step_months=3):
    """The latest first-of-month whose 90-day outcome window is already over,
    then every `step_months` before it."""
    latest = utc_date(today.year, today.month)
    while latest + OUTCOME > today:
        latest = add_months(latest, -1)
    return sorted(add_months(latest, -step_months * i) for i in range(count))


def select_packages(advisories, prediction_dates, n, seed):
    """Stratified sample so the dataset contains enough positive examples:
    40% packages with an advisory inside some 90-day outcome window,
    30% packages with only older advisories, 30% popular packages."""
    rng = random.Random(seed)

    def in_outcome_window(advs):
        return any(t <= a["published"] < t + OUTCOME for a in advs for t in prediction_dates)

    positives = sorted(p for p, advs in advisories.items() if in_outcome_window(advs))
    history = sorted(p for p, advs in advisories.items()
                     if p not in set(positives)
                     and any(a["published"] < prediction_dates[-1] for a in advs))
    popular = [line.strip() for line in (ROOT / "packages" / "popular.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")]

    n_pos, n_hist = int(n * 0.4), int(n * 0.3)
    chosen = rng.sample(positives, min(n_pos, len(positives)))
    chosen += rng.sample(history, min(n_hist, len(history)))
    rest = [p for p in popular if p not in set(chosen)]
    chosen += rest[: n - len(chosen)]
    return sorted(set(chosen))


def build_rows(name, advisories, prediction_dates, github, collected_at):
    packument = fetch_packument(name)
    if not packument or not packument["versions"]:
        print("    skipped: not found on npm")
        return []

    repo = parse_github_repo(packument.get("repository"))
    window_start = add_months(prediction_dates[0], -12)
    activity = None
    if repo and github:
        try:
            activity = github.fetch_activity(repo, window_start)
        except Exception as e:  # keep going; GitHub columns stay NULL
            print(f"    GitHub data unavailable for {repo}: {e}")

    try:
        daily = fetch_daily_downloads(name, window_start, prediction_dates[-1])
    except Exception as e:  # keep the package; download columns stay NULL
        print(f"    download counts unavailable: {e}")
        daily = None

    rows = []
    for prediction in prediction_dates:
        version, published = pick_version(packument, prediction)
        if not version:
            continue
        meta = package_meta(packument, version, published)
        if not meta["package_created_at"]:
            continue
        row = {"purl": purl_for(name, version)}
        row.update(meta)
        row.update(base_features(packument, version))

        downloads, observed = download_series(daily, prediction)
        releases = stable_release_counts(packument, prediction)
        for m in range(12):
            row[f"package_downloads_M{m}"] = downloads[m]
            row[f"package_download_days_observed_M{m}"] = observed[m]
            row[f"package_stable_release_count_M{m}"] = releases[m]

        row.update(github_features(activity, prediction))
        row.update(advisory_features(advisories.get(name, []), prediction))
        row.update({"prediction_date": f"{prediction:%Y-%m-%d}", "package_name": name,
                    "github_repo": repo, "collected_at": collected_at})
        rows.append(row)
    return rows


def write_outputs(rows):
    columns = schema.column_names()
    (ROOT / "schema.sql").write_text(schema.create_table_sql())

    db_path = DATA_DIR / "training_data.sqlite"
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(schema.create_table_sql())
    placeholders = ",".join("?" * len(columns))
    con.executemany(f"INSERT INTO {schema.TABLE} ({','.join(columns)}) VALUES ({placeholders})",
                    [[r.get(c) for c in columns] for r in rows])
    con.commit()
    con.close()

    with open(DATA_DIR / "training_data.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for r in rows:
            writer.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
    return db_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--packages", type=int, default=300, help="number of packages to sample")
    ap.add_argument("--seed", type=int, default=42, help="random seed for the package sample")
    ap.add_argument("--packages-file", help="use this list of package names instead of sampling")
    ap.add_argument("--prediction-dates", help="comma-separated YYYY-MM-01 dates")
    ap.add_argument("--no-github", action="store_true", help="skip GitHub even if a token is set")
    ap.add_argument("--github-cache-only", action="store_true",
                    help="use GitHub data downloaded so far, without calling GitHub")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # show progress even when output is redirected

    now = datetime.now(timezone.utc)
    if args.prediction_dates:
        dates = sorted(utc_date(*map(int, d.split("-"))) for d in args.prediction_dates.split(","))
    else:
        dates = default_prediction_dates(now)
    for d in dates:
        if d.day != 1 or d + OUTCOME > now:
            sys.exit(f"Prediction date {d:%Y-%m-%d} must be the 1st of a month at least 90 days ago.")
    print("Prediction dates:", ", ".join(f"{d:%Y-%m-%d}" for d in dates))

    advisories = load_advisories()
    print(f"Loaded GitHub-reviewed npm advisories for {len(advisories)} packages")

    DATA_DIR.mkdir(exist_ok=True)
    if args.packages_file:
        packages = [l.strip() for l in open(args.packages_file) if l.strip()]
    else:
        packages = select_packages(advisories, dates, args.packages, args.seed)
        (DATA_DIR / "selected_packages.txt").write_text("\n".join(packages) + "\n")

    token = None if args.no_github else token_from_env()
    if args.github_cache_only:
        github = GitHub(None, cache_only=True)
        print("Using only GitHub data that was already downloaded.")
    else:
        github = GitHub(token) if token else None
    if not github:
        print("No GITHUB_TOKEN set: GitHub issue/PR columns will be NULL.")

    collected_at = format_ts(now)
    rows, seen = [], set()
    for i, name in enumerate(packages, 1):
        print(f"[{i}/{len(packages)}] {name}")
        try:
            new_rows = build_rows(name, advisories, dates, github, collected_at)
        except Exception as e:
            print(f"    skipped: {e}")
            continue
        for r in new_rows:
            if r["purl"] not in seen:  # purl must be unique: same version at two dates -> keep first
                seen.add(r["purl"])
                rows.append(r)

    db_path = write_outputs(rows)
    print(f"\nWrote {len(rows)} rows to {db_path.relative_to(ROOT)} and data/training_data.csv")


if __name__ == "__main__":
    main()
