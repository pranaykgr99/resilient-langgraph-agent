# Add CSV and Excel analysis to your existing project

This upgrade adds actual file upload and analysis. It is ready to copy into your
existing project; it has not been pushed to your GitHub repository.

## 1. Copy the upgrade

In PowerShell, open your existing project:

```powershell
cd C:\Users\prana\Downloads\resilient-agent\resilient-agent
git status
docker compose stop
```

If Git shows your own uncommitted edits, save them before replacing source files.
Extract `resilient-agent-analytics.zip` into a separate folder. Open its
`resilient-agent` folder and copy its contents into your existing project folder,
allowing replacement of matching source files. This is an overlay: retain your
existing `.git` folder and `.env` file. Neither is included in the download.
Your existing Docker data volume stores saved tasks and remains in place.

## 2. Rebuild and open

Run in the same PowerShell project folder:

```powershell
docker compose up -d --build
docker compose ps
```

Open http://localhost:8080 and press Ctrl+F5 to refresh the updated interface.
Enter your existing API token. Keep `LLM_MODE=demo` for the built-in analyses.
The API URL field has been replaced by same-page requests.

## 3. Try your first analysis

1. Select `samples/sales.csv` under **Choose your data**.
2. Click **Upload and preview**. You should see 6 rows and 5 columns.
3. Choose **Totals and averages by group**.
4. Choose **Revenue** as the measure and **Category** as the group.
5. Click **Run task**.

Expected results: Bonding = 3,600; Composite = 6,400; total = 10,000.
Download the result CSV or the full JSON report.

Choose **Monthly totals and change**, with Revenue and Date, for January = 3,000,
February = 3,000 and March = 4,000. March change is about 33.33%.

To try Excel, save the sample as an Excel Workbook (`.xlsx`), then upload it.
The first worksheet is used unless you enter a sheet name. Use a values-only
copy if your workbook contains formulas.

## 4. Try data quality and recovery

Upload `samples/sales_with_quality_issues.csv` and run **Data quality and structure**.
It reports blanks and exact duplicate rows; it does not silently clean them.
Upload `samples/invalid_numeric.csv` and select Revenue by Category. It should
escalate with a clear numeric-data error instead of reporting an invented total.

For restart recovery, upload sales.csv, choose an analysis and click **Plan and
pause**. Save the Task ID. Run `docker compose stop` followed by
`docker compose up -d`. Paste the Task ID and click **Resume**. The task reads the
stored dataset and finishes. Use the dataset ID to reconnect it for a new task.

## 5. Save your upgrade to GitHub

Review `git diff` first, then run:

```powershell
git add agent api ui requirements.txt tests/test_datasets.py samples README.md UPGRADE_ANALYTICS.md docs/analytics.md .github/workflows/ci.yml
git commit -m "Add CSV and Excel upload with verified analysis"
git push
```

The previous CI fix that creates `.env` from `.env.example` is preserved.
The upgrade was tested in Python here; Docker is unavailable in this workspace,
so your Docker build and GitHub CI run are the final environment checks.
