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

scheme_code_input = str(sys.argv[1])

log.info(
    f"Debugging Scheme Code: {scheme_code_input}"
)

# ==========================
# LOAD CONFIG
# ==========================
with open("config.json", "r") as f:
    config = json.load(f)

# ==========================
# DOWNLOAD historic.db
# ==========================
if not os.path.exists("historic.db"):
    log.info("Downloading historic.db...")

    gdown.download(
        config["historic_db_url"],
        "historic.db",
        quiet=False,
        fuzzy=True
    )

# ==========================
# DOWNLOAD AMFI DB
# ==========================
if not os.path.exists("amfi2026.db"):
    log.info("Downloading amfi2026.db...")

    gdown.download(
        "https://drive.google.com/uc?id=1jOg0wjsKahf1vonssg1vDagqRLJGeo60",
        "amfi2026.db",
        quiet=False,
        fuzzy=True
    )

# ==========================
# DOWNLOAD mf.db
# ==========================
log.info("Downloading mf.db...")

response = requests.get(
    config["mf_release_api"],
    timeout=30
)

response.raise_for_status()

release_info = response.json()

asset_url = next(
    (
        asset["browser_download_url"]
        for asset in release_info["assets"]
        if asset["name"] == "mf.db"
    ),
    None
)

if not asset_url:
    raise RuntimeError(
        "mf.db not found in release assets."
    )

with open("mf.db", "wb") as f:
    f.write(
        requests.get(
            asset_url,
            timeout=60
        ).content
    )

log.info("mf.db downloaded successfully.")

# ==========================
# EXTRACT RAW DATA
# ==========================
log.info(
    "Extracting Daily / AMFI / Historic data..."
)

with sqlite3.connect(":memory:") as conn:

    conn.execute(
        "ATTACH DATABASE 'mf.db' AS daily;"
    )

    conn.execute(
        "ATTACH DATABASE 'amfi2026.db' AS amfi;"
    )

    conn.execute(
        "ATTACH DATABASE 'historic.db' AS historic;"
    )

    daily_df = pd.read_sql_query(
        f"""
        SELECT *
        FROM daily.nav_history
        WHERE scheme_code = '{scheme_code_input}'
        ORDER BY nav_date
        """,
        conn
    )

    amfi_df = pd.read_sql_query(
        f"""
        SELECT *
        FROM amfi.nav_history
        WHERE scheme_code = '{scheme_code_input}'
        ORDER BY nav_date
        """,
        conn
    )

    historic_df = pd.read_sql_query(
        f"""
        SELECT *
        FROM historic.nav_history
        WHERE scheme_code = '{scheme_code_input}'
        ORDER BY nav_date
        """,
        conn
    )

# ==========================
# TAG SOURCE
# ==========================
daily_df["source"] = "daily"
amfi_df["source"] = "amfi"
historic_df["source"] = "historic"

# Historic DB uses nav_value
if "nav_value" in historic_df.columns:
    historic_df["nav"] = historic_df["nav_value"]

# ==========================
# MERGED RAW
# ==========================
merged_raw = pd.concat(
    [
        daily_df,
        amfi_df,
        historic_df
    ],
    ignore_index=True,
    sort=False
)

# ==========================
# PROD-LIKE DEDUP LOGIC
# ==========================
dedup_df = merged_raw.copy()

source_priority = {
    "daily": 1,
    "amfi": 2,
    "historic": 3
}

dedup_df["priority"] = (
    dedup_df["source"]
    .map(source_priority)
)

dedup_df["nav_date"] = pd.to_datetime(
    dedup_df["nav_date"],
    errors="coerce"
)

dedup_df = (
    dedup_df
    .sort_values(
        [
            "scheme_code",
            "nav_date",
            "priority"
        ]
    )
    .drop_duplicates(
        [
            "scheme_code",
            "nav_date"
        ],
        keep="first"
    )
)

dedup_df = dedup_df.drop(
    columns=["priority"],
    errors="ignore"
)

# ==========================
# SUMMARY
# ==========================
summary_rows = []

for source_name, dfx in [
    ("Daily", daily_df),
    ("AMFI", amfi_df),
    ("Historic", historic_df)
]:

    summary_rows.append({
        "Source": source_name,
        "Records": len(dfx),
        "Min Date":
            dfx["nav_date"].min()
            if len(dfx)
            else None,
        "Max Date":
            dfx["nav_date"].max()
            if len(dfx)
            else None
    })

summary_df = pd.DataFrame(
    summary_rows
)

# ==========================
# EXPORT
# ==========================
os.makedirs(
    "debug_output",
    exist_ok=True
)

file_path = (
    f"debug_output/raw_debug_{scheme_code_input}.xlsx"
)

with pd.ExcelWriter(
    file_path,
    engine="xlsxwriter"
) as writer:

    summary_df.to_excel(
        writer,
        sheet_name="Summary",
        index=False
    )

    daily_df.to_excel(
        writer,
        sheet_name="Raw_Daily_DB",
        index=False
    )

    amfi_df.to_excel(
        writer,
        sheet_name="Raw_AMFI_DB",
        index=False
    )

    historic_df.to_excel(
        writer,
        sheet_name="Raw_Historic_DB",
        index=False
    )

    merged_raw.to_excel(
        writer,
        sheet_name="Merged_All_Sources",
        index=False
    )

    dedup_df.to_excel(
        writer,
        sheet_name="Deduped_Final_View",
        index=False
    )

log.info("")
log.info("========== SUMMARY ==========")
log.info(f"Daily Records    : {len(daily_df):,}")
log.info(f"AMFI Records     : {len(amfi_df):,}")
log.info(f"Historic Records : {len(historic_df):,}")
log.info(f"Final Records    : {len(dedup_df):,}")
log.info("=============================")
log.info("")
log.info(f"Debug file saved to: {file_path}")
log.info("Debug completed successfully.")
