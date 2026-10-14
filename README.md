# Rating report collector

[中文说明](README.zh-CN.md)

I built this tool to avoid downloading rating reports one by one and maintaining the file list by hand. Give it an issuer name or a spreadsheet of issuers; it collects matching disclosures, saves PDFs and builds an Excel inventory with source links and download status.

There are adapters for ChinaBond and ChinaMoney, a command-line runner, and a Tkinter desktop interface.

## A real collection run

![Report generated from downloaded PDFs and the collection manifest](docs/portfolio/live-evidence/screenshot.png)

On 14 October 2026, I ran the original collector against **ChinaBond**, using **中国长江三峡集团有限公司** as the issuer. This was a small run: the first listing page, one download worker and a direct connection.

| Output | Measured result |
| --- | --- |
| Report entries returned | 15 |
| PDFs downloaded and parsed | 15 |
| Unique PDFs, checked by SHA-256 | 10 |
| Duplicate copies | 5 |
| Pages across the 10 unique PDFs | 182 |

The duplicates matter: 15 downloaded files do not mean 15 different reports. The inventory keeps the entries traceable and marks files with matching hashes.

The screenshot is a report built from that run. The [15-row manifest](docs/portfolio/live-evidence/manifest.csv) includes source URLs, PDF URLs and hashes, and the [verification file](docs/portfolio/live-evidence/verification.json) records the totals. Source PDFs are not redistributed here.

A separate ChinaMoney check returned no matching records for this issuer. The numbers above are ChinaBond results only, and do not represent the issuer's complete reporting history.

## What the tool produces

- PDFs grouped by source and issuer.
- An Excel workbook with source sheets, a combined inventory and a summary.
- CSV inventories for further processing.
- Download status, source links, duplicate flags and error details.
- Saved task/page progress so interrupted work can resume.

The desktop interface supports list import, progress display and pause/resume controls. Business column names and the current interface are primarily Chinese.

## Run a small collection

Use Python 3.10 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --issuer "中国长江三峡集团有限公司" --source chinabond --max-pages 1 --no-proxy --config docs/portfolio/live-evidence/settings.json
```

The checked-in sample configuration contains no credentials. Before a larger run, review output paths, worker counts and request rates in your configuration.

Other entry points, after activating the environment:

```powershell
.\.venv\Scripts\Activate.ps1
python main.py --help
python main.py --excel issuers.xlsx --start 1 --limit 5 --max-pages 1 --no-proxy
python gui_app.py
```

Excel input uses an `issuer_name_clean` column. Sources can be `chinamoney`, `chinabond` or `both`. Resume is enabled by default; use `--no-resume` when you deliberately want to change that.

For a network-free example:

```powershell
python scripts/portfolio_demo.py
```

That example uses **synthetic records** to check duplicate detection, workbook export and checkpoints. Its report is `docs/portfolio/demo.html`; the real collection evidence above is separate.

## Files and code

| Path | Purpose |
| --- | --- |
| `downloads/` | Downloaded PDFs |
| `output/评级报告清单.xlsx` | Excel inventory |
| `output/inventory.csv` | Combined CSV inventory |
| `output/records.jsonl`, `progress.json` | Records and resumable state |
| `rating_crawler/chinamoney.py`, `chinabond.py` | Source adapters |
| `rating_crawler/http.py` | Requests and downloads |
| `rating_crawler/inventory.py` | Export and duplicate detection |
| `rating_crawler/progress.py` | Checkpoints |
| `scripts/build_exe.ps1` | Windows executable packaging |

Stack: Python, curl_cffi, pandas, openpyxl and Tkinter.

## Adapting it to another request

For a similar job, I would first confirm the issuer list, date range, sites and required spreadsheet columns, then check a small batch together. Scheduled updates or another source need their own implementation and verification.

These adapters are site-specific. A changed page or unavailable document can cause an error, which belongs in the inventory rather than being counted as a successful download.

[Original Chinese technical notes](docs/legacy-readme.zh-CN.md)
