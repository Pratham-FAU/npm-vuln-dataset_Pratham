"""Column definitions for the PCOM-01 record schema (one row per npm package version).

The SQL table is generated from these definitions so the column list, the
CREATE TABLE statement and the CSV header can never drift apart.
"""

MONTHS = 12  # M0 .. M11
SEVERITIES = ["low", "moderate", "high", "critical", "unknown"]

# Time-series families in the order of the schema document.
NPM_SERIES = [
    "package_downloads",
    "package_download_days_observed",
    "package_stable_release_count",
]
GITHUB_SERIES = [
    "issues_opened",
    "issues_closed",
    "issues_reopened",
    "pull_requests_opened",
    "pull_requests_closed_unmerged",
    "pull_requests_merged",
    "pull_requests_reopened",
    "issues_open_at_month_end",
    "pull_requests_open_at_month_end",
]

TIMESTAMP_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z"
)


def _timestamp(name):
    return (name, "TEXT", f"NOT NULL CHECK ({name} GLOB '{TIMESTAMP_GLOB}')")


def _count(name, required):
    null = "NOT NULL " if required else ""
    return (name, "INTEGER", f"{null}CHECK ({name} >= 0)")


def core_columns():
    cols = [
        ("purl", "TEXT",
         "NOT NULL UNIQUE CHECK (purl LIKE 'pkg:npm/%@%' AND length(purl) <= 2048)"),
        _timestamp("package_created_at"),
        _timestamp("published_at"),
        _count("unpacked_size", required=False),
        _count("file_count", required=False),
        _count("dependency_count", required=True),
        _count("optional_dependency_count", required=True),
        _count("peer_dependency_count", required=True),
        _count("dev_dependency_count", required=True),
        _count("bundled_dependency_count", required=True),
    ]
    for family in NPM_SERIES + GITHUB_SERIES:
        for m in range(MONTHS):
            name = f"{family}_M{m}"
            if family == "package_download_days_observed":
                cols.append((name, "INTEGER", f"CHECK ({name} BETWEEN 0 AND 31)"))
            else:
                cols.append(_count(name, required=False))
    for sev in SEVERITIES:
        cols.append(_count(f"ghsa_{sev}_count", required=True))
    for sev in SEVERITIES:
        for m in range(MONTHS):
            cols.append(_count(f"ghsa_{sev}_count_M{m}", required=True))
    for sev in SEVERITIES:
        cols.append(_count(f"target_ghsa_{sev}_count_90d", required=True))
    return cols


# Extra columns (allowed by the homework: "you can extend the data").
# They record the context needed to reproduce each row.
EXTENSION_COLUMNS = [
    ("prediction_date", "TEXT",
     "NOT NULL CHECK (date(prediction_date) IS prediction_date)"),
    ("package_name", "TEXT", "NOT NULL"),
    ("github_repo", "TEXT", ""),
    ("collected_at", "TEXT", f"NOT NULL CHECK (collected_at GLOB '{TIMESTAMP_GLOB}')"),
]

TABLE = "package_version_record"


def all_columns():
    return core_columns() + EXTENSION_COLUMNS


def column_names():
    return [c[0] for c in all_columns()]


def create_table_sql():
    lines = [f"    {name} {sqltype} {constraint}".rstrip()
             for name, sqltype, constraint in all_columns()]
    return f"CREATE TABLE {TABLE} (\n" + ",\n".join(lines) + "\n);\n"


if __name__ == "__main__":
    print(create_table_sql(), end="")
