# npm vulnerability prediction – training dataset

PCOM Homework 3. A training dataset built from public sources, following the class
record schema (PCOM-01). **One row = one npm package version at a prediction date.**
The goal column group is the number of new GitHub-reviewed security advisories (GHSAs) for
the package in the 90 days after the prediction date, split by severity.

## Files

| Path | Content |
|---|---|
| `data/training_data.sqlite` | The dataset (table `package_version_record`) |
| `data/training_data.csv` | The same rows as CSV |
| `data/selected_packages.txt` | Package sample used for this version of the data |
| `schema.sql` | `CREATE TABLE` statement (generated from `src/schema.py`) |
| `src/build_dataset.py` | Builds the dataset end to end |
| `src/npm_source.py` | npm registry + download counts |
| `src/github_source.py` | GitHub issue / pull request activity |
| `src/advisories.py` | GitHub-reviewed advisories (via the OSV.dev export) |
| `src/summarize.py` | Prints a summary of the dataset |
| `packages/popular.txt` | Popular packages added to the sample |

## How to run

Requires Python 3.9+ and no extra packages.

```bash
python3 src/build_dataset.py            # 300 packages, 9 prediction dates
python3 src/summarize.py
```

To also fill the GitHub issue/PR columns, set a GitHub token first (a fine-grained token
with read-only access to public repositories is enough):

```bash
export GITHUB_TOKEN=<your token>
python3 src/build_dataset.py
```

Downloaded data is cached in `cache/` (not committed), so re-runs are fast and an interrupted
run continues where it stopped.

## Data sources

| Columns | Source |
|---|---|
| purl, timestamps, size, file count, dependency counts, stable releases | npm registry (`registry.npmjs.org/<package>`) |
| downloads, observed days | npm downloads API (`api.npmjs.org/downloads/range/...`) |
| issues / pull requests | GitHub REST API (issues list, issue events, search) |
| `ghsa_*`, `target_ghsa_*` | GitHub Advisory Database, GitHub-reviewed records, via the OSV.dev npm export |

## Decisions and assumptions

The schema leaves some points open. These are the choices made here:

- **Prediction dates.** Quarterly dates (1st of a month) from 2024-08-01 to the latest
  month whose 90-day outcome window is already over. Each package contributes one row per
  prediction date. Because `purl` must be unique, a version that is still current at a later
  date is only kept once, at its first date.
- **Months.** `M0` is the calendar month directly before the prediction date and `M11`
  is eleven months before `M0`. All months are UTC and end-exclusive.
- **Selected version.** The most recently published stable (non-prerelease) version before
  the prediction date. If a package has no stable version, the latest prerelease is used.
- **Package sample.** Stratified with a fixed seed (42): 40% packages with an advisory in
  some outcome window, 30% packages with only older advisories, 30% popular packages
  (`packages/popular.txt`). This keeps enough positive examples. The natural rate of
  advisories is much lower, which matters when evaluating a model.
- **Advisories.** Only `GHSA-*` records marked `github_reviewed`. Withdrawn advisories are
  excluded from the moment they were withdrawn. Severity is the *current* severity, because
  severity history is not published. The schema asks for severity "at prediction" and at the
  "outcome window's end", so this is an approximation.
- **GitHub.** Activity is measured for the repository named in the package's `repository`
  field. Packages from one monorepo share the same values. Months the API data does not fully
  cover (very large repositories exceed the paging limits) are `NULL`, as the schema allows.
  Without a `GITHUB_TOKEN`, all GitHub columns are `NULL`.
- **Downloads.** A month with no observed days has `NULL` downloads and `0` observed days.

## Extension columns

Added after the 224 required columns (allowed by the homework):
`prediction_date`, `package_name`, `github_repo`, `collected_at`.
