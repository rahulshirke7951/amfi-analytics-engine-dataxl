import sqlite3
import pandas as pd
import requests
import json
import os
import re
import logging
import gdown
from datetime import datetime, timedelta

# ==========================
# LOGGING SETUP
# ==========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ==========================
# 1. LOAD CONFIG & DOWNLOADS
# ==========================
with open("config.json", "r") as f:
    config = json.load(f)

# Historic DB (cached)
if not os.path.exists("historic.db"):
    log.info("Cache miss: Downloading historic database...")
    gdown.download(config["historic_db_url"], "historic.db", quiet=True, fuzzy=True)
else:
    log.info("Cache hit: Using cached historic.db")

# AMFI 01-Jun DB
if not os.path.exists("amfi2026.db"):
    log.info("Downloading AMFI 01-Jun database...")
    gdown.download(
        "https://drive.google.com/uc?id=1jOg0wjsKahf1vonssg1vDagqRLJGeo60",
        "amfi2026.db",
        quiet=False,
        fuzzy=True
    )
else:
    log.info("Cache hit: Using amfi2026.db")

# Daily mf.db
log.info("Fetching daily mf.db from GitHub API...")
try:
    response = requests.get(config["mf_release_api"], timeout=30)
    release_info = response.json()
    asset_url = next(
        (a["browser_download_url"] for a in release_info["assets"] if a["name"] == "mf.db"),
        None
    )
    if not asset_url:
        raise RuntimeError("mf.db not found among release assets.")
    
    with open("mf.db", "wb") as f:
        f.write(requests.get(asset_url, timeout=60).content)
    log.info("mf.db downloaded successfully.")
except Exception as e:
    raise RuntimeError(f"Failed to download mf.db: {e}") from e

# ==========================
# 2. DATA LOADING & CLEANING
# ==========================

def parse_dates_vectorized(series: pd.Series) -> pd.Series:
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
        parsed = pd.to_datetime(series, format=fmt, errors='coerce')
        if parsed.notna().mean() > 0.95:
            return parsed
    return pd.to_datetime(series, errors='coerce')

with sqlite3.connect(":memory:") as conn:

    conn.execute("ATTACH DATABASE 'mf.db' AS daily;")
    conn.execute("ATTACH DATABASE 'historic.db' AS historic;")
    conn.execute("ATTACH DATABASE 'amfi2026.db' AS amfi;")

    query = """
        SELECT
            scheme_code,
            nav,
            nav_date,
            'daily' AS source
        FROM daily.nav_history

        UNION ALL

        SELECT
            scheme_code,
            nav_value AS nav,    
            nav_date,
            'amfi' AS source
        FROM amfi.nav_history

        UNION ALL

        SELECT
            scheme_code,
            nav_value AS nav,
            nav_date,
            'historic' AS source
        FROM historic.nav_history
    """
        
    df = pd.read_sql_query(query, conn)

    # Metadata: Use the most recent entry per scheme to avoid duplicates
    meta_query = """
        SELECT scheme_code, scheme_name, amc_name, scheme_category
        FROM (
            SELECT scheme_code, scheme_name, amc_name, scheme_category,
                   ROW_NUMBER() OVER (PARTITION BY scheme_code ORDER BY nav_date DESC) as rn
            FROM daily.nav_history
        ) WHERE rn = 1
    """
    meta_raw = pd.read_sql_query(meta_query, conn)

# Deduplication: 'daily' source wins
df["nav_date"] = parse_dates_vectorized(df["nav_date"])
df = df.dropna(subset=["nav_date"])
source_priority = {
    "daily": 1,
    "amfi": 2,
    "historic": 3
}

df["priority"] = df["source"].map(source_priority)

df = (
    df.sort_values(
        ["scheme_code", "nav_date", "priority"]
    )
    .drop_duplicates(
        ["scheme_code", "nav_date"],
        keep="first"
    )
    .drop(columns=["source", "priority"])
)

log.info(
    "Merged Dataset | Rows=%s | Min Date=%s | Max Date=%s",
    f"{len(df):,}",
    df["nav_date"].min(),
    df["nav_date"].max()
)

# Derive Anchor Date after full merge
latest_nav_date = df["nav_date"].max()
if pd.isna(latest_nav_date):
    raise RuntimeError("No NAV data available.")

# Setting 'today' as the reference point
today = (latest_nav_date - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

log.info("Anchor Date set to: %s (Based on Max Date: %s)", today.date(), latest_nav_date.date())

# ==========================
# 3. IDENTIFY STATUS & REINDEX
# ==========================
max_period = max(config["return_periods_days"])
buffer_days = config.get("reindex_buffer_days", 15)
freshness_days = config.get("freshness_threshold_days", 5)

reindex_start = today - timedelta(days=max_period + buffer_days)
freshness_threshold = today - timedelta(days=freshness_days)

latest_nav_df = df.sort_values("nav_date").groupby("scheme_code").tail(1).copy()
audit_trail = latest_nav_df[["scheme_code", "nav_date", "nav"]].rename(
    columns={"nav_date": "latest_nav_date", "nav": "latest_nav"}
)
audit_trail["status"] = audit_trail["latest_nav_date"].apply(
    lambda x: "Active" if x >= freshness_threshold else "Excluded: Stale Data"
)

# Reindexing
active_codes = audit_trail[audit_trail["status"] == "Active"]["scheme_code"].unique()
df_active = df[df["scheme_code"].isin(active_codes) & (df["nav_date"] >= reindex_start)].copy()

all_dates = pd.date_range(df_active["nav_date"].min(), today, freq="D")
idx = pd.MultiIndex.from_product([active_codes, all_dates], names=["scheme_code", "nav_date"])

df_filled = (
    df_active.set_index(["scheme_code", "nav_date"])
             .reindex(idx)
             .groupby(level=0)
             .ffill()
             .reset_index()
)

# ==========================
# 4. COMPUTE RETURNS (PIVOT)
# ==========================
nav_pivot = df_filled.pivot_table(index="nav_date", columns="scheme_code", values="nav")

def get_nav_at_offset(pivot: pd.DataFrame, anchor: datetime, days: int) -> pd.Series:
    target = anchor - timedelta(days=days)
    available = pivot.index[pivot.index <= target]
    if available.empty: return pd.Series(dtype=float, name=f"nav_{days}d")
    result = pivot.loc[available[-1]].copy()
    result.name = f"nav_{days}d"
    return result

for d in config["return_periods_days"]:
    past_nav = get_nav_at_offset(nav_pivot, today, d)
    audit_trail = audit_trail.merge(
        past_nav.reset_index().rename(columns={"scheme_code": "scheme_code", past_nav.name: f"nav_{d}d"}),
        on="scheme_code", how="left"
    )
    mask = (audit_trail["status"] == "Active") & (audit_trail[f"nav_{d}d"] > 0)
    audit_trail.loc[mask, f"return_{d}d"] = (
        (audit_trail.loc[mask, "latest_nav"] - audit_trail.loc[mask, f"nav_{d}d"]) 
        / audit_trail.loc[mask, f"nav_{d}d"] * 100
    ).round(2)

# ==========================
# 5. METADATA & EXPORT
# ==========================
_CAT_PATTERN = re.compile(r'^(.*?)\s*\(\s*(.*?)\s*\)$')
_PLAN_PATTERN = re.compile(r'\b(Direct|Regular)\b', re.IGNORECASE)
_OPTION_PATTERN = re.compile(r'\b(IDCW|Dividend|Bonus|Growth)\b', re.IGNORECASE)

def split_category(cat_str):
    if not isinstance(cat_str, str): return pd.Series(["NA", "NA", "NA"])
    m = _CAT_PATTERN.match(cat_str.strip())
    if not m: return pd.Series([cat_str.strip(), "NA", "NA"])
    main = m.group(1).strip()
    parts = [p.strip() for p in m.group(2).split(" - ")]
    return pd.Series([main, parts[0] if len(parts)>0 else "NA", parts[1] if len(parts)>1 else "NA"])

meta_raw[["cat_level_1", "cat_level_2", "cat_level_3"]] = meta_raw["scheme_category"].apply(split_category)
meta_raw["plan_type"] = meta_raw["scheme_name"].apply(lambda x: (m := _PLAN_PATTERN.search(str(x))) and m.group(1).capitalize() or "Regular")
meta_raw["option_type"] = meta_raw["scheme_name"].apply(lambda x: (m := _OPTION_PATTERN.search(str(x))) and (m.group(1).upper().replace("DIVIDEND", "IDCW").capitalize()) or "Growth")

analytics_dashboard = audit_trail[audit_trail["status"] == "Active"].merge(meta_raw, on="scheme_code", how="left")

os.makedirs("output", exist_ok=True)
with pd.ExcelWriter("output/dashboard_data.xlsx", engine="xlsxwriter") as writer:
    analytics_dashboard.to_excel(writer, sheet_name="Active_Analytics", index=False)
    audit_trail.to_excel(writer, sheet_name="Full_Audit_Trail", index=False)

log.info("Process Complete. Output saved to output/dashboard_data.xlsx")
