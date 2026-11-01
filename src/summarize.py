"""Print a short summary of data/training_data.sqlite (row counts, label balance, missing data)."""

import sqlite3

import schema
from util import ROOT

con = sqlite3.connect(ROOT / "data" / "training_data.sqlite")
q = lambda sql: con.execute(sql).fetchone()[0]
t = schema.TABLE

rows = q(f"SELECT count(*) FROM {t}")
print(f"Rows:                       {rows}")
print(f"Distinct packages:          {q(f'SELECT count(DISTINCT package_name) FROM {t}')}")
print(f"Prediction dates:           {q(f'SELECT group_concat(d, \", \") FROM (SELECT DISTINCT prediction_date d FROM {t} ORDER BY d)')}")
targets = " + ".join(f"target_ghsa_{s}_count_90d" for s in schema.SEVERITIES)
positive = q(f"SELECT count(*) FROM {t} WHERE {targets} > 0")
print(f"Rows with >=1 new advisory: {positive} ({100 * positive / max(rows, 1):.1f}%)")
print(f"Rows with a GitHub repo:    {q(f'SELECT count(*) FROM {t} WHERE github_repo IS NOT NULL')}")

print("\nShare of NULL values per column group:")
groups = {"npm downloads": "package_downloads_M", "npm releases": "package_stable_release_count_M",
          "GitHub issues/PRs": "issues_opened_M"}
for label, prefix in groups.items():
    cols = [f"{prefix}{m}" for m in range(12)]
    nulls = sum(q(f"SELECT count(*) FROM {t} WHERE {c} IS NULL") for c in cols)
    print(f"  {label:20} {100 * nulls / max(rows * 12, 1):5.1f}%")
