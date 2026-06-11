import sqlite3
import pandas as pd
import requests
import json
import os
import logging
import gdown
import sys

# ==========================
# LOGGING
# ==========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ==========================
# READ SCHEME CODE
# ==========================
if len(sys.argv) < 2:
    raise RuntimeError(
        "Scheme code required. Example: python debug_audit_history.py 140088"
    )

scheme_code_input = int(sys.argv[1]) if sys.argv[1].isdigit() else str(sys.argv[1])

log.info(f"Debugging Scheme Code: {scheme_code_input}")

# ==========================
# LOAD CONFIG
# ==========================
with open("config.json", "r") as f:
    config = json.load(f)

# ==========================
# DOWNLOAD CACHED DBs
# ==========================
if not os.path.exists("historic.db"):
    log.info("Downloading historic.db...")
    gdown.download(config["historic_db_url"], "historic.db", quiet=True, fuzzy=True)

if not os.path.exists("amfi2026.db"):
    log.info("Downloading amfi2026.db...")
    gdown.download(config["amfi_db_url"], "amfi2026.db", quiet=True, fuzzy=True)

# ==========================
# DOWNLOAD mf.db (Daily)
# ==========================
log.info("Fetching daily mf.db from GitHub API...")
response = requests.get(config["mf_release_api"], timeout=30)
response.raise_for_status()
release_info = response.json()

asset_url = next(
    (a["browser_download_url"] for a in release_info["assets"] if a["name"] == "mf.db"),
    None
)

if not asset_url:
    raise RuntimeError("mf.db not found in release assets.")

with open("mf.db", "wb") as f:
    f.write(requests.get(asset_url, timeout=60).content)
log.info("mf.db downloaded successfully.")

# ==========================
# DATE PARSING UTILITY
# ==========================
def parse_dates_vectorized(series: pd.Series) -> pd.Series:
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
        parsed = pd.to_datetime(series, format=fmt, errors='coerce')
        if parsed.notna().mean() > 0.95:
            return parsed
    return pd.to_datetime(series, errors='coerce')

# ==========================
# EXTRACT RAW DATA (Isolated per Source)
# ==========================
log.info("Extracting Isolated Source Data for Debugging...")

with sqlite3.connect(":memory:") as conn:
    conn.execute("ATTACH DATABASE 'mf.db' AS daily;")
    conn.execute("ATTACH DATABASE 'amfi2026.db' AS amfi;")
    conn.execute("ATTACH DATABASE 'historic.db' AS historic;")

    # Isolated extractions matching production column rules
    daily_df = pd.read_sql_query(
        "SELECT scheme_code, nav, nav_date FROM daily.nav_history WHERE scheme_code = ?", 
        conn, params=[scheme_code_input]
    )
    
    amfi_df = pd.read_sql_query(
        "SELECT scheme_code, nav_value AS nav, nav_date FROM amfi.nav_history WHERE scheme_code = ?", 
        conn, params=[scheme_code_input]
    )
    
    historic_df = pd.read_sql_query(
        "SELECT scheme_code, nav_value AS nav, nav_date FROM historic.nav_history WHERE scheme_code = ?", 
        conn, params=[scheme_code_input]
    )

# Assign explicit sources
daily_df["source"] = "daily"
amfi_df["source"] = "amfi"
historic_df["source"] = "historic"

# Create combined un-deduped view
merged_raw = pd.concat([daily_df, amfi_df, historic_df], ignore_index=True, sort=False)

# ==========================
# PROD-IDENTICAL DEDUP LOGIC
# ==========================
log.info("Applying production deduplication workflow...")
dedup_df = merged_raw.copy()
dedup_df["nav_date"] = parse_dates_vectorized(dedup_df["nav_date"])
dedup_df = dedup_df.dropna(subset=["nav_date"])

source_priority = {"daily": 1, "amfi": 2, "historic": 3}
dedup_df["priority"] = dedup_df["source"].map(source_priority)

dedup_df = (
    dedup_df.sort_values(["scheme_code", "nav_date", "priority"])
    .drop_duplicates(["scheme_code", "nav_date"], keep="first")
    .drop(columns=["source", "priority"])
    .sort_values("nav_date")
)

# ==========================
# COMPUTE SUMMARY METRICS
# ==========================
summary_rows = []
for name, df_src in [("Daily", daily_df), ("AMFI", amfi_df), ("Historic", historic_df)]:
    parsed_dates = parse_dates_vectorized(df_src["nav_date"])
    summary_rows.append({
        "Source": name,
        "Total Records": len(df_src),
        "Min Date": parsed_dates.min() if not df_src.empty else None,
        "Max Date": parsed_dates.max() if not df_src.empty else None
    })
summary_df = pd.DataFrame(summary_rows)

# ==========================
# EXPORT TO EXCEL
# ==========================
os.makedirs("debug_output", exist_ok=True)
file_path = f"debug_output/raw_debug_{scheme_code_input}.xlsx"

with pd.ExcelWriter(file_path, engine="xlsxwriter") as writer:
    summary_df.to_excel(writer, sheet_name="Summary", index=False)
    daily_df.to_excel(writer, sheet_name="Raw_Daily_DB", index=False)
    amfi_df.to_excel(writer, sheet_name="Raw_AMFI_DB", index=False)
    historic_df.to_excel(writer, sheet_name="Raw_Historic_DB", index=False)
    merged_raw.to_excel(writer, sheet_name="Merged_All_Sources", index=False)
    dedup_df.to_excel(writer, sheet_name="Deduped_Final_View", index=False)

log.info("")
log.info("========== DEBUG SUMMARY ==========")
log.info(f"Daily Records    : {len(daily_df):,}")
log.info(f"AMFI Records     : {len(amfi_df):,}")
log.info(f"Historic Records : {len(historic_df):,}")
log.info(f"Final (Deduped)  : {len(dedup_df):,}")
log.info("===================================")
log.info(f"Debug report generated at: {file_path}")
