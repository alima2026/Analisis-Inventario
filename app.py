import hashlib
import math
import re
import sqlite3
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "pedidos_v1.db"

DEFAULT_TARGET_MONTHS = 6
DEFAULT_LEAD_TIME_MONTHS = 6
DEFAULT_CAPITAL = 500000.0
DEFAULT_COMPANY = "Magna"
AUTO_ORDER_FOLDER = APP_DIR / "Pedidos Solicitados"


# =========================================================
# Utilidades
# =========================================================
def normalize_part(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip().upper()
    text = re.sub(r"\s+", "", text)
    return text


def safe_numeric(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace("$", "", regex=False)
        .str.replace("USD", "", regex=False)
        .str.replace("UYU", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", "", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def detect_brand(part_no: str, description: str = "") -> str:
    part = normalize_part(part_no)
    description_text = str(description).upper().strip()

    if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{2}-[A-Z0-9]{3}[A-Z]?", part):
        return "Mazda"
    if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}[A-Z]?", part):
        return "Mazda"

    if re.fullmatch(r"[0-9]{5}[A-Z][0-9]{3}", part):
        return "Kia/Hyundai"
    if re.fullmatch(r"[0-9]{6}[A-Z][0-9]{3}", part):
        return "Kia/Hyundai"

    if part.startswith(("11", "12", "13", "16", "17", "18", "31", "32", "33", "34", "51", "61", "64")) and len(part) in [7, 11]:
        return "BMW/MINI"
    if "BMW" in description_text or "MINI" in description_text:
        return "BMW/MINI"

    if "." in part:
        return "Multimarca"
    if re.fullmatch(r"[A-Z]{1,5}[0-9]{2,6}[A-Z0-9-]*", part):
        return "Multimarca"
    if re.fullmatch(r"[A-Z0-9]{1,6}-[A-Z0-9]{1,6}", part):
        return "Multimarca"

    if "MAZDA" in description_text:
        return "Mazda"
    if "KIA" in description_text or "HYUNDAI" in description_text:
        return "Kia/Hyundai"

    return "Multimarca"


def classify_abc(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    abc = df.copy()
    abc = abc.sort_values(value_col, ascending=False).reset_index(drop=True)
    total_value = abc[value_col].sum()

    if total_value <= 0:
        abc["abc"] = "C"
        return abc

    abc["pct"] = abc[value_col] / total_value
    abc["pct_acum"] = abc["pct"].cumsum()

    def label(accumulated_pct):
        if accumulated_pct <= 0.80:
            return "A"
        if accumulated_pct <= 0.95:
            return "B"
        return "C"

    abc["abc"] = abc["pct_acum"].apply(label)
    return abc


def to_excel_bytes(sheets: dict) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            export_df = df.copy()
            export_df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()


def dataframe_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Hoja1") -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()


def normalize_analysis_date(raw_value) -> date:
    analysis_ts = pd.Timestamp(raw_value).replace(day=1)
    return analysis_ts.date()


def get_analysis_month(analysis_date: date) -> str:
    return analysis_date.strftime("%Y-%m")


def get_rolling_window(analysis_date: date):
    analysis_ts = pd.Timestamp(analysis_date).replace(day=1)
    rolling_start = (analysis_ts - pd.DateOffset(years=3)).date()
    rolling_end = analysis_ts.date()
    return rolling_start, rolling_end


def calendar_month_gap(previous_date: date, current_date: date) -> int:
    return max((current_date.year - previous_date.year) * 12 + (current_date.month - previous_date.month), 0)


def elapsed_months(previous_created_at: str, previous_analysis_date: str, current_analysis_date: date) -> float:
    current_dt = datetime.now()
    previous_dt = pd.to_datetime(previous_created_at).to_pydatetime()
    day_gap = max((current_dt - previous_dt).total_seconds() / 86400.0, 0.0)
    day_based_months = day_gap / 30.0
    previous_analysis = pd.to_datetime(previous_analysis_date).date()
    month_gap = float(calendar_month_gap(previous_analysis, current_analysis_date))
    return max(day_based_months, month_gap)


def safe_text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


class LocalSourceFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.name = self.path.name
        self._bytes = None

    def getvalue(self) -> bytes:
        if self._bytes is None:
            self._bytes = self.path.read_bytes()
        return self._bytes

    def __fspath__(self):
        return str(self.path)


def build_file_hash(uploaded_file) -> str:
    hasher = hashlib.sha256()
    hasher.update(uploaded_file.name.encode("utf-8"))
    hasher.update(uploaded_file.getvalue())
    return hasher.hexdigest()


def find_latest_file(base_dir: Path, patterns: list[str], recursive: bool = False) -> Optional[LocalSourceFile]:
    candidates = []
    for pattern in patterns:
        iterator = base_dir.rglob(pattern) if recursive else base_dir.glob(pattern)
        candidates.extend(path for path in iterator if path.is_file())

    if not candidates:
        return None

    latest_path = max(candidates, key=lambda item: (item.stat().st_mtime, item.name))
    return LocalSourceFile(latest_path)


def detect_default_source_files(base_dir: Path) -> dict:
    return {
        "ventas": find_latest_file(base_dir, ["ventas_de_3*.xls", "ventas_de_3*.xlsx"]),
        "inventario": find_latest_file(base_dir, ["inventario_*.xls", "inventario_*.xlsx"]),
        "backorder": find_latest_file(base_dir, ["backorder*.xls", "backorder*.xlsx"]),
        "pedido_fabrica": find_latest_file(AUTO_ORDER_FOLDER, ["*.xls", "*.xlsx"], recursive=True)
        if AUTO_ORDER_FOLDER.exists()
        else None,
    }


def resolve_source_file(uploaded_file, detected_file):
    return uploaded_file if uploaded_file is not None else detected_file


def build_source_hash(
    empresa: str,
    analysis_month: str,
    target_months: int,
    lead_time_months: int,
    capital_available: float,
    ventas_file,
    inventario_file,
    backorder_file,
    pedido_file,
) -> str:
    hasher = hashlib.sha256()
    header = f"{empresa}|{analysis_month}|{target_months}|{lead_time_months}|{capital_available:.2f}"
    hasher.update(header.encode("utf-8"))

    for uploaded in [ventas_file, inventario_file, backorder_file, pedido_file]:
        if uploaded is None:
            hasher.update(b"SIN_PEDIDO_FABRICA")
            continue
        hasher.update(uploaded.name.encode("utf-8"))
        hasher.update(uploaded.getvalue())

    return hasher.hexdigest()


# =========================================================
# Lectura de archivos
# =========================================================
def load_sales(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file, header=[0, 1])
    if df.shape[1] < 13:
        raise ValueError("El archivo de ventas no tiene el formato esperado.")

    df.columns = [
        "part_no",
        "description",
        "unit",
        "sales_units",
        "bonif_units",
        "net_units",
        "sample_units",
        "sales_uyu",
        "sales_usd",
        "sales_pct",
        "cost_uyu",
        "cost_usd",
        "cost_pct",
    ]

    df = df.copy()
    df["part_no"] = df["part_no"].map(normalize_part)
    df["description"] = df["description"].astype(str).str.strip()

    for col in [
        "sales_units",
        "bonif_units",
        "net_units",
        "sample_units",
        "sales_uyu",
        "sales_usd",
        "cost_uyu",
        "cost_usd",
    ]:
        df[col] = safe_numeric(df[col])

    df = df[df["part_no"] != ""].copy()
    df["brand"] = df.apply(lambda row: detect_brand(row["part_no"], row["description"]), axis=1)
    df["avg_monthly_units"] = df["sales_units"] / 36.0
    df["avg_annual_units"] = df["sales_units"] / 3.0
    df["avg_monthly_sales_uyu"] = df["sales_uyu"] / 36.0
    return df


def load_inventory(uploaded_file) -> pd.DataFrame:
    raw = pd.read_excel(uploaded_file, header=None)
    df = raw.iloc[5:, [2, 8, 16, 20]].copy()
    df.columns = ["part_no", "description", "unit", "stock"]
    df = df.dropna(subset=["part_no"]).copy()
    df["part_no"] = df["part_no"].map(normalize_part)
    df["description"] = df["description"].astype(str).str.strip()
    df["stock"] = safe_numeric(df["stock"])
    df = df[df["part_no"] != ""].copy()
    df["brand"] = df.apply(lambda row: detect_brand(row["part_no"], row["description"]), axis=1)
    return df.groupby(["part_no", "description", "brand"], as_index=False)["stock"].sum()


def load_backorder(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file)
    df = df.copy()

    part_col = "Buyer Part" if "Buyer Part" in df.columns else "Seller Part"
    desc_col = "Description" if "Description" in df.columns else None

    df["part_no"] = df[part_col].map(normalize_part)
    df["description"] = df[desc_col].astype(str).str.strip() if desc_col else ""

    numeric_cols = [
        "Under Investigation Qty",
        "Expected First Allocation Qty",
        "Expected Last Allocation Qty",
        "Allocation Qty",
        "Seller Part Qty",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = safe_numeric(df[col])

    df["backorder_qty"] = 0
    for col in [
        "Under Investigation Qty",
        "Expected First Allocation Qty",
        "Expected Last Allocation Qty",
        "Allocation Qty",
    ]:
        if col in df.columns:
            df["backorder_qty"] += df[col]

    df = df[df["part_no"] != ""].copy()
    df["brand"] = df.apply(lambda row: detect_brand(row["part_no"], row["description"]), axis=1)
    return df.groupby(["part_no", "description", "brand"], as_index=False)["backorder_qty"].sum()


def load_monthly_order(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file)
    df = df.copy()

    part_col = "PART NO" if "PART NO" in df.columns else df.columns[0]
    qty_col = "PCS" if "PCS" in df.columns else df.columns[1]
    order_no_col = "ORDER NO" if "ORDER NO" in df.columns else None

    df["part_no"] = df[part_col].map(normalize_part)
    df["monthly_order_qty"] = safe_numeric(df[qty_col])
    df = df[df["part_no"] != ""].copy()
    order_summary = df.groupby("part_no", as_index=False)["monthly_order_qty"].sum()

    order_code = ""
    if order_no_col and order_no_col in df.columns:
        order_values = [safe_text(value) for value in df[order_no_col].dropna().tolist() if safe_text(value)]
        if order_values:
            order_code = order_values[0]

    order_summary.attrs["order_code"] = order_code if order_code else Path(uploaded_file.name).stem
    order_summary.attrs["source_file_name"] = uploaded_file.name
    return order_summary


def empty_monthly_order(source_name: str = "Sin pedido a fabrica") -> pd.DataFrame:
    df = pd.DataFrame(columns=["part_no", "monthly_order_qty"])
    df.attrs["order_code"] = ""
    df.attrs["source_file_name"] = source_name
    return df


def build_mazda_order_to_request(pedido_inteligente: pd.DataFrame) -> pd.DataFrame:
    if pedido_inteligente.empty:
        return pd.DataFrame(columns=["ORDER NO", "LINE NO", "PART NO", "PCS"])

    order_df = pedido_inteligente.copy()
    mazda_mask = order_df["brand"].astype(str).str.upper().str.contains("MAZDA", na=False)
    if mazda_mask.any():
        order_df = order_df[mazda_mask].copy()

    order_df["PCS"] = pd.to_numeric(order_df["intelligent_buy_qty"], errors="coerce").fillna(0).apply(math.ceil)
    order_df = order_df[order_df["PCS"] > 0].copy()
    order_df = order_df.sort_values(["smart_score", "sales_units"], ascending=[False, False]).reset_index(drop=True)

    export_df = pd.DataFrame(
        {
            "ORDER NO": "",
            "LINE NO": range(1, len(order_df) + 1),
            "PART NO": order_df["part_no"].astype(str),
            "PCS": order_df["PCS"].astype(int),
        }
    )
    return export_df


# =========================================================
# Motor principal
# =========================================================
def merge_all(sales: pd.DataFrame, stock: pd.DataFrame, backorder: pd.DataFrame, order: pd.DataFrame) -> pd.DataFrame:
    sales_base = sales[
        [
            "part_no",
            "description",
            "brand",
            "sales_units",
            "sales_uyu",
            "cost_uyu",
            "avg_monthly_units",
            "avg_annual_units",
            "avg_monthly_sales_uyu",
        ]
    ].copy()

    stock_base = stock[["part_no", "description", "brand", "stock"]].rename(
        columns={"description": "description_stock", "brand": "brand_stock"}
    )
    backorder_base = backorder[["part_no", "description", "brand", "backorder_qty"]].rename(
        columns={"description": "description_backorder", "brand": "brand_backorder"}
    )
    order_base = order[["part_no", "monthly_order_qty"]].copy()

    merged = (
        sales_base.merge(stock_base, on="part_no", how="outer")
        .merge(backorder_base, on="part_no", how="outer")
        .merge(order_base, on="part_no", how="outer")
    )

    merged["part_no"] = merged["part_no"].fillna("").map(normalize_part)
    merged["description"] = merged["description"].fillna("")
    merged["brand"] = merged["brand"].fillna("")

    for alternative_col in ["description_stock", "description_backorder"]:
        merged["description"] = merged["description"].where(
            merged["description"].astype(str).str.strip() != "",
            merged[alternative_col].fillna(""),
        )

    for alternative_col in ["brand_stock", "brand_backorder"]:
        merged["brand"] = merged["brand"].where(
            merged["brand"].astype(str).str.strip() != "",
            merged[alternative_col].fillna(""),
        )

    merged["brand"] = merged.apply(
        lambda row: row["brand"] if safe_text(row["brand"]) else detect_brand(row["part_no"], row["description"]),
        axis=1,
    )

    for col in [
        "sales_units",
        "sales_uyu",
        "cost_uyu",
        "avg_monthly_units",
        "avg_annual_units",
        "avg_monthly_sales_uyu",
        "stock",
        "backorder_qty",
        "monthly_order_qty",
    ]:
        merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(0.0)

    merged["pipeline_qty"] = pd.to_numeric(merged["backorder_qty"] + merged["monthly_order_qty"], errors="coerce").fillna(0.0)
    merged["available_plus_pipeline"] = pd.to_numeric(merged["stock"] + merged["pipeline_qty"], errors="coerce").fillna(0.0)
    merged["unit_margin_uyu"] = pd.to_numeric(
        (merged["sales_uyu"] - merged["cost_uyu"]) / merged["sales_units"].replace(0, pd.NA),
        errors="coerce",
    ).fillna(0.0)

    merged = merged.drop(columns=["description_stock", "brand_stock", "description_backorder", "brand_backorder"])
    return merged


def add_inventory_logic(df: pd.DataFrame, target_months: int, lead_time_months: int) -> pd.DataFrame:
    out = df.copy()
    out["months_of_stock"] = out["stock"] / out["avg_monthly_units"].replace(0, pd.NA)
    out["months_of_stock"] = pd.to_numeric(out["months_of_stock"], errors="coerce").fillna(999.0)

    out["target_stock_qty"] = (out["avg_monthly_units"] * target_months).apply(math.ceil)
    out["lead_time_need_qty"] = (out["avg_monthly_units"] * lead_time_months).apply(math.ceil)
    out["suggested_order_qty"] = (out["target_stock_qty"] - out["available_plus_pipeline"]).clip(lower=0).apply(math.ceil)

    def define_status(row):
        if row["sales_units"] <= 0 and row["stock"] > 0:
            return "Stock muerto"
        if row["sales_units"] <= 0 and row["stock"] <= 0:
            return "Sin historial"
        if row["available_plus_pipeline"] <= 0:
            return "Critico"
        if row["available_plus_pipeline"] < row["lead_time_need_qty"]:
            return "Comprar ya"
        if row["available_plus_pipeline"] < row["target_stock_qty"]:
            return "Comprar"
        return "OK"

    out["status"] = out.apply(define_status, axis=1)
    out["stock_muerto"] = (out["stock"] > 0) & (out["sales_units"] <= 0)
    out["oferta_sugerida"] = (
        (out["stock"] > 0)
        & (out["sales_units"] > 0)
        & (out["months_of_stock"] >= 24)
        & (out["months_of_stock"] <= 30)
    )
    return out


def add_abc(df: pd.DataFrame) -> pd.DataFrame:
    metric_col = "sales_uyu" if df["sales_uyu"].sum() > 0 else "sales_units"
    abc_df = classify_abc(df[["part_no", metric_col]].copy(), value_col=metric_col)
    out = df.merge(abc_df[["part_no", "abc"]], on="part_no", how="left")
    out["abc"] = out["abc"].fillna("C")
    return out


def add_intelligent_order(df: pd.DataFrame, capital: float) -> pd.DataFrame:
    out = df.copy()

    abc_score = {"A": 100, "B": 70, "C": 40}
    status_score = {
        "Critico": 100,
        "Comprar ya": 80,
        "Comprar": 60,
        "OK": 20,
        "Sin historial": 10,
        "Stock muerto": 0,
    }

    out["abc_score"] = out["abc"].map(abc_score).fillna(40)
    out["status_score"] = out["status"].map(status_score).fillna(20)
    out["rotation_score"] = out["avg_monthly_units"].fillna(0) * 10
    out["margin_score"] = out["unit_margin_uyu"].fillna(0) / 100.0

    out["smart_score"] = (
        out["abc_score"] * 0.35
        + out["status_score"] * 0.35
        + out["rotation_score"] * 0.20
        + out["margin_score"] * 0.10
    )

    out["estimated_unit_cost"] = 0.0
    mask_cost = out["sales_units"] > 0
    out.loc[mask_cost, "estimated_unit_cost"] = out.loc[mask_cost, "cost_uyu"] / out.loc[mask_cost, "sales_units"]
    out["estimated_unit_cost"] = out["estimated_unit_cost"].fillna(0)

    out["estimated_purchase_cost"] = out["suggested_order_qty"] * out["estimated_unit_cost"]
    out["estimated_gross_profit"] = out["suggested_order_qty"] * out["unit_margin_uyu"]

    candidates = out[
        (out["suggested_order_qty"] > 0)
        & (~out["stock_muerto"])
        & (out["estimated_unit_cost"] >= 0)
    ].copy()

    candidates = candidates.sort_values(
        ["smart_score", "estimated_gross_profit", "sales_units"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    remaining_capital = capital
    intelligent_qty = []
    intelligent_cost = []

    for _, row in candidates.iterrows():
        qty = int(row["suggested_order_qty"])
        unit_cost = float(row["estimated_unit_cost"])

        if qty <= 0:
            intelligent_qty.append(0)
            intelligent_cost.append(0.0)
            continue

        if unit_cost <= 0:
            intelligent_qty.append(qty)
            intelligent_cost.append(0.0)
            continue

        max_affordable = int(remaining_capital // unit_cost)
        final_qty = min(qty, max_affordable)
        final_cost = final_qty * unit_cost

        intelligent_qty.append(final_qty)
        intelligent_cost.append(final_cost)
        remaining_capital -= final_cost

    candidates["intelligent_buy_qty"] = intelligent_qty
    candidates["intelligent_buy_cost"] = intelligent_cost
    candidates["selected_for_purchase"] = candidates["intelligent_buy_qty"] > 0

    out = out.merge(
        candidates[["part_no", "intelligent_buy_qty", "intelligent_buy_cost", "selected_for_purchase"]],
        on="part_no",
        how="left",
    )

    for col in ["intelligent_buy_qty", "intelligent_buy_cost"]:
        out[col] = out[col].fillna(0)
    out["selected_for_purchase"] = out["selected_for_purchase"].eq(True)
    return out


def build_analysis_dataframe(
    sales_df: pd.DataFrame,
    stock_df: pd.DataFrame,
    backorder_df: pd.DataFrame,
    order_df: pd.DataFrame,
    open_orders_df: Optional[pd.DataFrame],
    target_months: int,
    lead_time_months: int,
    capital_available: float,
    empresa: str,
) -> pd.DataFrame:
    final_df = merge_all(sales_df, stock_df, backorder_df, order_df)
    if open_orders_df is None or open_orders_df.empty:
        final_df["open_order_qty_db"] = 0.0
    else:
        final_df = final_df.merge(open_orders_df[["part_no", "open_order_qty_db"]], on="part_no", how="left")
        final_df["open_order_qty_db"] = pd.to_numeric(final_df["open_order_qty_db"], errors="coerce").fillna(0.0)

    final_df["pipeline_qty"] = final_df["backorder_qty"] + final_df["monthly_order_qty"] + final_df["open_order_qty_db"]
    final_df["available_plus_pipeline"] = final_df["stock"] + final_df["pipeline_qty"]
    final_df = add_inventory_logic(final_df, target_months, lead_time_months)
    final_df = add_abc(final_df)
    final_df = add_intelligent_order(final_df, capital_available)
    final_df["empresa"] = empresa
    final_df = final_df.sort_values(["brand", "part_no"]).reset_index(drop=True)
    return final_df


# =========================================================
# SQLite y persistencia historica
# =========================================================
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_column(conn, table_name: str, column_name: str, definition: str):
    existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in existing_cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa TEXT NOT NULL,
                analysis_month TEXT NOT NULL,
                analysis_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                rolling_start TEXT,
                rolling_end TEXT,
                target_months INTEGER,
                lead_time_months INTEGER,
                capital_available REAL,
                sales_filename TEXT,
                inventory_filename TEXT,
                backorder_filename TEXT,
                order_filename TEXT,
                sales_rows INTEGER DEFAULT 0,
                inventory_rows INTEGER DEFAULT 0,
                backorder_rows INTEGER DEFAULT 0,
                order_rows INTEGER DEFAULT 0,
                source_hash TEXT NOT NULL UNIQUE,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_run_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                part_no TEXT NOT NULL,
                description TEXT,
                brand TEXT,
                sales_units REAL DEFAULT 0,
                sales_uyu REAL DEFAULT 0,
                cost_uyu REAL DEFAULT 0,
                avg_monthly_units REAL DEFAULT 0,
                avg_annual_units REAL DEFAULT 0,
                avg_monthly_sales_uyu REAL DEFAULT 0,
                stock REAL DEFAULT 0,
                backorder_qty REAL DEFAULT 0,
                monthly_order_qty REAL DEFAULT 0,
                pipeline_qty REAL DEFAULT 0,
                available_plus_pipeline REAL DEFAULT 0,
                unit_margin_uyu REAL DEFAULT 0,
                months_of_stock REAL DEFAULT 0,
                target_stock_qty REAL DEFAULT 0,
                lead_time_need_qty REAL DEFAULT 0,
                suggested_order_qty REAL DEFAULT 0,
                abc TEXT,
                status TEXT,
                stock_muerto INTEGER DEFAULT 0,
                oferta_sugerida INTEGER DEFAULT 0,
                estimated_unit_cost REAL DEFAULT 0,
                intelligent_buy_qty REAL DEFAULT 0,
                intelligent_buy_cost REAL DEFAULT 0,
                estimated_gross_profit REAL DEFAULT 0,
                smart_score REAL DEFAULT 0,
                FOREIGN KEY (run_id) REFERENCES analysis_runs(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS factory_order_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                empresa TEXT NOT NULL,
                analysis_month TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_type TEXT NOT NULL,
                order_name TEXT,
                order_code TEXT,
                order_file_hash TEXT,
                file_name TEXT,
                total_items INTEGER DEFAULT 0,
                total_qty REAL DEFAULT 0,
                status TEXT DEFAULT 'ABIERTO',
                source_hash TEXT NOT NULL UNIQUE,
                notes TEXT,
                FOREIGN KEY (run_id) REFERENCES analysis_runs(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS factory_order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                part_no TEXT NOT NULL,
                description TEXT,
                brand TEXT,
                quantity REAL DEFAULT 0,
                received_qty REAL DEFAULT 0,
                open_qty REAL DEFAULT 0,
                last_reconciled_at TEXT,
                status TEXT DEFAULT 'ABIERTO',
                FOREIGN KEY (batch_id) REFERENCES factory_order_batches(id) ON DELETE CASCADE
            )
            """
        )

        ensure_column(conn, "factory_order_batches", "order_code", "TEXT")
        ensure_column(conn, "factory_order_batches", "order_file_hash", "TEXT")
        ensure_column(conn, "factory_order_batches", "file_name", "TEXT")
        ensure_column(conn, "factory_order_items", "received_qty", "REAL DEFAULT 0")
        ensure_column(conn, "factory_order_items", "open_qty", "REAL DEFAULT 0")
        ensure_column(conn, "factory_order_items", "last_reconciled_at", "TEXT")
        conn.execute("UPDATE factory_order_items SET received_qty = COALESCE(received_qty, 0)")
        conn.execute("UPDATE factory_order_items SET open_qty = COALESCE(open_qty, quantity)")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analysis_runs_company_date ON analysis_runs(empresa, analysis_date, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analysis_items_run_part ON analysis_run_items(run_id, part_no)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_batches_company_date ON factory_order_batches(empresa, analysis_month, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_items_batch_part ON factory_order_items(batch_id, part_no)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_batches_file_hash ON factory_order_batches(order_file_hash)"
        )

        migrate_legacy_data(conn)
        conn.commit()


def insert_analysis_run_record(
    conn,
    empresa: str,
    analysis_month: str,
    analysis_date: date,
    created_at: str,
    rolling_start: date,
    rolling_end: date,
    target_months: int,
    lead_time_months: int,
    capital_available: float,
    sales_filename: str,
    inventory_filename: str,
    backorder_filename: str,
    order_filename: str,
    sales_rows: int,
    inventory_rows: int,
    backorder_rows: int,
    order_rows: int,
    source_hash: str,
    notes: str,
):
    existing = conn.execute("SELECT id FROM analysis_runs WHERE source_hash = ?", (source_hash,)).fetchone()
    if existing:
        return existing["id"], True

    cursor = conn.execute(
        """
        INSERT INTO analysis_runs (
            empresa, analysis_month, analysis_date, created_at, rolling_start, rolling_end,
            target_months, lead_time_months, capital_available,
            sales_filename, inventory_filename, backorder_filename, order_filename,
            sales_rows, inventory_rows, backorder_rows, order_rows, source_hash, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            empresa,
            analysis_month,
            analysis_date.isoformat(),
            created_at,
            rolling_start.isoformat(),
            rolling_end.isoformat(),
            target_months,
            lead_time_months,
            float(capital_available),
            sales_filename,
            inventory_filename,
            backorder_filename,
            order_filename,
            int(sales_rows),
            int(inventory_rows),
            int(backorder_rows),
            int(order_rows),
            source_hash,
            notes,
        ),
    )
    return cursor.lastrowid, False


def persist_analysis_items(conn, run_id: int, final_df: pd.DataFrame):
    storage_df = final_df.copy()
    required_columns = [
        "part_no",
        "description",
        "brand",
        "sales_units",
        "sales_uyu",
        "cost_uyu",
        "avg_monthly_units",
        "avg_annual_units",
        "avg_monthly_sales_uyu",
        "stock",
        "backorder_qty",
        "monthly_order_qty",
        "pipeline_qty",
        "available_plus_pipeline",
        "unit_margin_uyu",
        "months_of_stock",
        "target_stock_qty",
        "lead_time_need_qty",
        "suggested_order_qty",
        "abc",
        "status",
        "stock_muerto",
        "oferta_sugerida",
        "estimated_unit_cost",
        "intelligent_buy_qty",
        "intelligent_buy_cost",
        "estimated_gross_profit",
        "smart_score",
    ]

    for col in required_columns:
        if col not in storage_df.columns:
            storage_df[col] = 0 if col not in {"description", "brand", "abc", "status"} else ""

    rows = []
    for _, row in storage_df[required_columns].iterrows():
        rows.append(
            (
                run_id,
                safe_text(row["part_no"]),
                safe_text(row["description"]),
                safe_text(row["brand"]),
                float(row["sales_units"]),
                float(row["sales_uyu"]),
                float(row["cost_uyu"]),
                float(row["avg_monthly_units"]),
                float(row["avg_annual_units"]),
                float(row["avg_monthly_sales_uyu"]),
                float(row["stock"]),
                float(row["backorder_qty"]),
                float(row["monthly_order_qty"]),
                float(row["pipeline_qty"]),
                float(row["available_plus_pipeline"]),
                float(row["unit_margin_uyu"]),
                float(row["months_of_stock"]),
                float(row["target_stock_qty"]),
                float(row["lead_time_need_qty"]),
                float(row["suggested_order_qty"]),
                safe_text(row["abc"]),
                safe_text(row["status"]),
                int(bool(row["stock_muerto"])),
                int(bool(row["oferta_sugerida"])),
                float(row["estimated_unit_cost"]),
                float(row["intelligent_buy_qty"]),
                float(row["intelligent_buy_cost"]),
                float(row["estimated_gross_profit"]),
                float(row["smart_score"]),
            )
        )

    conn.executemany(
        """
        INSERT INTO analysis_run_items (
            run_id, part_no, description, brand, sales_units, sales_uyu, cost_uyu,
            avg_monthly_units, avg_annual_units, avg_monthly_sales_uyu,
            stock, backorder_qty, monthly_order_qty, pipeline_qty, available_plus_pipeline,
            unit_margin_uyu, months_of_stock, target_stock_qty, lead_time_need_qty,
            suggested_order_qty, abc, status, stock_muerto, oferta_sugerida,
            estimated_unit_cost, intelligent_buy_qty, intelligent_buy_cost,
            estimated_gross_profit, smart_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def create_order_batch(
    conn,
    run_id,
    empresa: str,
    analysis_month: str,
    created_at: str,
    order_file_hash: str,
    source_type: str,
    order_name: str,
    order_df: pd.DataFrame,
    final_df: pd.DataFrame,
    file_name: str,
    notes: str,
):
    if order_df.empty:
        return None, False

    batch_hash = f"pedido:{order_file_hash}" if order_file_hash else f"pedido:{analysis_month}:{order_name}"
    existing = conn.execute(
        "SELECT id FROM factory_order_batches WHERE source_hash = ? OR order_file_hash = ?",
        (batch_hash, order_file_hash),
    ).fetchone()
    if existing:
        return existing["id"], True

    order_enriched = order_df.merge(
        final_df[["part_no", "description", "brand"]].drop_duplicates("part_no"),
        on="part_no",
        how="left",
    ).copy()
    order_enriched["description"] = order_enriched["description"].fillna("")
    order_enriched["brand"] = order_enriched["brand"].fillna("")

    order_enriched = order_enriched[order_enriched["monthly_order_qty"] > 0].copy()
    if order_enriched.empty:
        return None, False

    order_code = safe_text(order_df.attrs.get("order_code", "")) or safe_text(order_name)
    total_qty = float(order_enriched["monthly_order_qty"].sum())
    total_items = int(len(order_enriched))

    cursor = conn.execute(
        """
        INSERT INTO factory_order_batches (
            run_id, empresa, analysis_month, created_at, source_type, order_name, order_code,
            order_file_hash, file_name, total_items, total_qty, status, source_hash, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ABIERTO', ?, ?)
        """,
        (
            run_id,
            empresa,
            analysis_month,
            created_at,
            source_type,
            order_name,
            order_code,
            order_file_hash,
            file_name,
            total_items,
            total_qty,
            batch_hash,
            notes,
        ),
    )
    batch_id = cursor.lastrowid

    item_rows = []
    for _, row in order_enriched.iterrows():
        qty = float(row["monthly_order_qty"])
        item_rows.append(
            (
                batch_id,
                safe_text(row["part_no"]),
                safe_text(row["description"]),
                safe_text(row["brand"]),
                qty,
                0.0,
                qty,
                None,
                "ABIERTO",
            )
        )

    conn.executemany(
        """
        INSERT INTO factory_order_items (
            batch_id, part_no, description, brand, quantity, received_qty, open_qty, last_reconciled_at, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        item_rows,
    )
    return batch_id, False


def migrate_legacy_data(conn):
    existing_runs = conn.execute("SELECT COUNT(*) AS qty FROM analysis_runs").fetchone()["qty"]
    if existing_runs > 0:
        return

    legacy_tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    required_legacy_tables = {"stock_mensual", "backorder", "pedidos_emitidos", "ventas_base_3anios"}
    if not required_legacy_tables.issubset(legacy_tables):
        return

    legacy_periods = [row["periodo"] for row in conn.execute("SELECT DISTINCT periodo FROM stock_mensual ORDER BY periodo")]
    if not legacy_periods:
        return

    legacy_sales = pd.read_sql_query(
        """
        SELECT
            COALESCE(NULLIF(codigo_norm, ''), codigo) AS part_no,
            COALESCE(descripcion, '') AS description,
            COALESCE(marca, '') AS brand,
            COALESCE(ventas_3_anios, 0) AS sales_units,
            0.0 AS sales_uyu,
            0.0 AS cost_uyu,
            COALESCE(promedio_mensual_base, 0) AS avg_monthly_units,
            COALESCE(promedio_mensual_base, 0) * 12.0 AS avg_annual_units,
            0.0 AS avg_monthly_sales_uyu
        FROM ventas_base_3anios
        """,
        conn,
    )
    legacy_sales["part_no"] = legacy_sales["part_no"].map(normalize_part)

    for period in legacy_periods:
        legacy_stock = pd.read_sql_query(
            """
            SELECT
                COALESCE(NULLIF(codigo_norm, ''), codigo) AS part_no,
                COALESCE(descripcion, '') AS description,
                COALESCE(marca, '') AS brand,
                SUM(COALESCE(stock_actual, 0)) AS stock
            FROM stock_mensual
            WHERE periodo = ?
            GROUP BY 1, 2, 3
            """,
            conn,
            params=(period,),
        )
        legacy_stock["part_no"] = legacy_stock["part_no"].map(normalize_part)

        legacy_backorder = pd.read_sql_query(
            """
            SELECT
                COALESCE(NULLIF(codigo_norm, ''), codigo) AS part_no,
                COALESCE(descripcion, '') AS description,
                COALESCE(marca, '') AS brand,
                SUM(COALESCE(cantidad_backorder, 0)) AS backorder_qty
            FROM backorder
            WHERE periodo = ?
            GROUP BY 1, 2, 3
            """,
            conn,
            params=(period,),
        )
        legacy_backorder["part_no"] = legacy_backorder["part_no"].map(normalize_part)

        legacy_orders = pd.read_sql_query(
            """
            SELECT
                COALESCE(NULLIF(codigo_norm, ''), codigo) AS part_no,
                SUM(COALESCE(cantidad, 0)) AS monthly_order_qty
            FROM pedidos_emitidos
            WHERE periodo = ?
            GROUP BY 1
            """,
            conn,
            params=(period,),
        )
        legacy_orders["part_no"] = legacy_orders["part_no"].map(normalize_part)

        analysis_date = normalize_analysis_date(f"{period}-01")
        rolling_start, rolling_end = get_rolling_window(analysis_date)
        final_df = build_analysis_dataframe(
            legacy_sales,
            legacy_stock,
            legacy_backorder,
            legacy_orders,
            None,
            DEFAULT_TARGET_MONTHS,
            DEFAULT_LEAD_TIME_MONTHS,
            0.0,
            DEFAULT_COMPANY,
        )

        source_hash = f"legacy::{DEFAULT_COMPANY}::{period}"
        created_at = f"{period}-01T00:00:00"
        run_id, _ = insert_analysis_run_record(
            conn=conn,
            empresa=DEFAULT_COMPANY,
            analysis_month=period,
            analysis_date=analysis_date,
            created_at=created_at,
            rolling_start=rolling_start,
            rolling_end=rolling_end,
            target_months=DEFAULT_TARGET_MONTHS,
            lead_time_months=DEFAULT_LEAD_TIME_MONTHS,
            capital_available=0.0,
            sales_filename="legacy:ventas_base_3anios",
            inventory_filename=f"legacy:stock_mensual:{period}",
            backorder_filename=f"legacy:backorder:{period}",
            order_filename=f"legacy:pedidos_emitidos:{period}",
            sales_rows=len(legacy_sales),
            inventory_rows=len(legacy_stock),
            backorder_rows=len(legacy_backorder),
            order_rows=len(legacy_orders),
            source_hash=source_hash,
            notes="Migracion automatica desde tablas legacy.",
        )

        already_loaded = conn.execute(
            "SELECT COUNT(*) AS qty FROM analysis_run_items WHERE run_id = ?",
            (run_id,),
        ).fetchone()["qty"]
        if already_loaded == 0:
            persist_analysis_items(conn, run_id, final_df)

        create_order_batch(
            conn=conn,
            run_id=run_id,
            empresa=DEFAULT_COMPANY,
            analysis_month=period,
            created_at=created_at,
            order_file_hash=f"legacy-order::{DEFAULT_COMPANY}::{period}",
            source_type="legacy_migration",
            order_name=f"Pedido legacy {period}",
            order_df=legacy_orders,
            final_df=final_df,
            file_name=f"legacy:{period}",
            notes="Migracion automatica de pedidos_emitidos.",
        )


def save_analysis_run(
    empresa: str,
    analysis_date: date,
    target_months: int,
    lead_time_months: int,
    capital_available: float,
    ventas_file,
    inventario_file,
    backorder_file,
    pedido_file,
    sales_df: pd.DataFrame,
    stock_df: pd.DataFrame,
    backorder_df: pd.DataFrame,
    order_df: pd.DataFrame,
    final_df: pd.DataFrame,
    source_hash: str,
    order_file_hash: str,
    register_current_order: bool,
    notes: str,
):
    analysis_month = get_analysis_month(analysis_date)
    rolling_start, rolling_end = get_rolling_window(analysis_date)
    created_at = datetime.now().replace(microsecond=0).isoformat()

    with get_connection() as conn:
        run_id, duplicated = insert_analysis_run_record(
            conn=conn,
            empresa=empresa,
            analysis_month=analysis_month,
            analysis_date=analysis_date,
            created_at=created_at,
            rolling_start=rolling_start,
            rolling_end=rolling_end,
            target_months=target_months,
            lead_time_months=lead_time_months,
            capital_available=capital_available,
            sales_filename=ventas_file.name,
            inventory_filename=inventario_file.name,
            backorder_filename=backorder_file.name,
            order_filename=pedido_file.name if pedido_file is not None else "Sin pedido a fabrica",
            sales_rows=len(sales_df),
            inventory_rows=len(stock_df),
            backorder_rows=len(backorder_df),
            order_rows=len(order_df),
            source_hash=source_hash,
            notes=notes,
        )

        if duplicated:
            return {
                "status": "duplicate",
                "run_id": run_id,
                "batch_id": None,
                "message": "Esta corrida ya estaba guardada en la base.",
            }

        persist_analysis_items(conn, run_id, final_df)
        reconcile_open_orders_with_inventory(conn, empresa, final_df, created_at)

        batch_id = None
        if register_current_order and pedido_file is not None and not order_df.empty:
            order_code = safe_text(order_df.attrs.get("order_code", ""))
            order_name = order_code if order_code else f"Pedido fabrica {analysis_month} - corrida {run_id}"
            batch_id, _ = create_order_batch(
                conn=conn,
                run_id=run_id,
                empresa=empresa,
                analysis_month=analysis_month,
                created_at=created_at,
                order_file_hash=order_file_hash,
                source_type="archivo_pedido_mensual",
                order_name=order_name,
                order_df=order_df,
                final_df=final_df,
                file_name=pedido_file.name,
                notes=notes,
            )

        conn.commit()
        return {
            "status": "saved",
            "run_id": run_id,
            "batch_id": batch_id,
            "message": "Corrida guardada correctamente en la base.",
        }


def load_recent_runs(empresa: str, limit: int = 12) -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                r.id AS corrida_id,
                r.analysis_month AS mes_analisis,
                r.created_at AS fecha_carga,
                r.rolling_start AS ventana_desde,
                r.rolling_end AS ventana_hasta,
                COUNT(i.id) AS items,
                ROUND(COALESCE(SUM(i.stock), 0), 2) AS stock_total,
                ROUND(COALESCE(SUM(i.backorder_qty), 0), 2) AS backorder_total,
                ROUND(COALESCE(SUM(i.monthly_order_qty), 0), 2) AS pedido_archivo_total,
                ROUND(COALESCE(SUM(i.intelligent_buy_qty), 0), 2) AS compra_inteligente_total
            FROM analysis_runs r
            LEFT JOIN analysis_run_items i ON i.run_id = r.id
            WHERE r.empresa = ?
            GROUP BY r.id, r.analysis_month, r.created_at, r.rolling_start, r.rolling_end
            ORDER BY r.analysis_date DESC, r.created_at DESC
            LIMIT ?
            """,
            conn,
            params=(empresa, limit),
        )
    return df


def load_recent_order_batches(empresa: str, limit: int = 12) -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                b.id AS lote_id,
                b.analysis_month AS mes_analisis,
                b.created_at AS fecha_carga,
                b.source_type AS origen,
                COALESCE(b.order_code, b.order_name) AS nombre_lote,
                b.file_name AS archivo,
                b.total_items,
                b.total_qty,
                ROUND(COALESCE(SUM(i.open_qty), 0), 2) AS qty_abierta,
                b.status
            FROM factory_order_batches b
            LEFT JOIN factory_order_items i ON i.batch_id = b.id
            WHERE b.empresa = ?
            GROUP BY b.id, b.analysis_month, b.created_at, b.source_type, b.order_code, b.order_name, b.file_name, b.total_items, b.total_qty, b.status
            ORDER BY created_at DESC
            LIMIT ?
            """,
            conn,
            params=(empresa, limit),
        )
    return df


def load_previous_run(empresa: str, current_source_hash: str, current_analysis_date: date):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM analysis_runs
            WHERE empresa = ?
              AND source_hash <> ?
              AND analysis_date <= ?
            ORDER BY analysis_date DESC, created_at DESC
            LIMIT 1
            """,
            (empresa, current_source_hash, current_analysis_date.isoformat()),
        ).fetchone()
    return row


def load_run_items(run_id: int) -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                part_no,
                description,
                brand,
                sales_units,
                avg_monthly_units,
                stock,
                backorder_qty,
                monthly_order_qty
            FROM analysis_run_items
            WHERE run_id = ?
            """,
            conn,
            params=(run_id,),
        )
    return df


def load_open_factory_orders_by_part(empresa: str, exclude_order_file_hash: Optional[str] = None) -> pd.DataFrame:
    params = [empresa]
    query = """
        SELECT
            i.part_no,
            ROUND(COALESCE(SUM(i.open_qty), 0), 2) AS open_order_qty_db
        FROM factory_order_items i
        INNER JOIN factory_order_batches b ON b.id = i.batch_id
        WHERE b.empresa = ?
          AND b.status <> 'CANCELADO'
          AND COALESCE(i.open_qty, 0) > 0
    """

    if exclude_order_file_hash:
        query += " AND COALESCE(b.order_file_hash, '') <> ?"
        params.append(exclude_order_file_hash)

    query += " GROUP BY i.part_no"

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)

    return df


def load_order_history_by_part(empresa: str) -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                i.part_no,
                ROUND(COALESCE(SUM(i.quantity), 0), 2) AS ordered_total_db,
                ROUND(COALESCE(SUM(i.received_qty), 0), 2) AS received_total_db,
                ROUND(COALESCE(SUM(i.open_qty), 0), 2) AS open_order_qty_db,
                MAX(b.created_at) AS last_order_at,
                MAX(COALESCE(b.order_code, b.order_name)) AS last_order_code,
                COUNT(DISTINCT b.id) AS order_batches_db
            FROM factory_order_items i
            INNER JOIN factory_order_batches b ON b.id = i.batch_id
            WHERE b.empresa = ?
              AND b.status <> 'CANCELADO'
            GROUP BY i.part_no
            """,
            conn,
            params=(empresa,),
        )
    return df


def refresh_batch_status(conn, batch_id: int):
    totals = conn.execute(
        """
        SELECT
            COALESCE(SUM(quantity), 0) AS total_qty,
            COALESCE(SUM(open_qty), 0) AS open_qty,
            COALESCE(SUM(received_qty), 0) AS received_qty
        FROM factory_order_items
        WHERE batch_id = ?
        """,
        (batch_id,),
    ).fetchone()

    open_qty = float(totals["open_qty"] or 0)
    received_qty = float(totals["received_qty"] or 0)
    total_qty = float(totals["total_qty"] or 0)

    if total_qty <= 0:
        new_status = "VACIO"
    elif open_qty <= 0:
        new_status = "RECIBIDO_INFERIDO"
    elif received_qty > 0:
        new_status = "PARCIAL"
    else:
        new_status = "ABIERTO"

    conn.execute("UPDATE factory_order_batches SET status = ? WHERE id = ?", (new_status, batch_id))


def reconcile_open_orders_with_inventory(
    conn,
    empresa: str,
    final_df: pd.DataFrame,
    created_at: str,
):
    if "estimated_receipts_qty" not in final_df.columns:
        return

    receipts_df = final_df[["part_no", "estimated_receipts_qty"]].copy()
    receipts_df["estimated_receipts_qty"] = pd.to_numeric(receipts_df["estimated_receipts_qty"], errors="coerce").fillna(0.0)
    receipts_df = receipts_df[receipts_df["estimated_receipts_qty"] > 0]

    if receipts_df.empty:
        return

    for _, row in receipts_df.iterrows():
        remaining_qty = float(row["estimated_receipts_qty"])
        if remaining_qty <= 0:
            continue

        open_items = conn.execute(
            """
            SELECT
                i.id,
                i.batch_id,
                COALESCE(i.received_qty, 0) AS received_qty,
                COALESCE(i.open_qty, i.quantity) AS open_qty
            FROM factory_order_items i
            INNER JOIN factory_order_batches b ON b.id = i.batch_id
            WHERE b.empresa = ?
              AND b.status <> 'CANCELADO'
              AND i.part_no = ?
              AND COALESCE(i.open_qty, 0) > 0
            ORDER BY b.created_at ASC, i.id ASC
            """,
            (empresa, safe_text(row["part_no"])),
        ).fetchall()

        for item in open_items:
            if remaining_qty <= 0:
                break

            item_open_qty = float(item["open_qty"] or 0)
            if item_open_qty <= 0:
                continue

            inferred_received = min(item_open_qty, remaining_qty)
            new_open_qty = item_open_qty - inferred_received
            new_received_qty = float(item["received_qty"] or 0) + inferred_received
            new_status = "RECIBIDO_INFERIDO" if new_open_qty <= 0 else "PARCIAL"

            conn.execute(
                """
                UPDATE factory_order_items
                SET received_qty = ?, open_qty = ?, last_reconciled_at = ?, status = ?
                WHERE id = ?
                """,
                (new_received_qty, new_open_qty, created_at, new_status, item["id"]),
            )
            refresh_batch_status(conn, item["batch_id"])
            remaining_qty -= inferred_received


# =========================================================
# Seguimiento historico
# =========================================================
def determine_tracking_status(row) -> str:
    current_file_order = float(row.get("monthly_order_qty", 0) or 0)
    registered_orders = float(row.get("ordered_total_db", 0) or 0)
    open_orders = float(row.get("open_order_qty_db", 0) or 0)
    current_backorder = float(row.get("backorder_qty", 0) or 0)
    estimated_receipts = float(row.get("estimated_receipts_qty", 0) or 0)
    stock_delta = float(row.get("stock_delta", 0) or 0)

    if registered_orders <= 0 and current_file_order > 0:
        return "Pedido en archivo actual"
    if registered_orders <= 0 and current_backorder <= 0:
        return "Sin pedido registrado"
    if current_backorder > 0 and estimated_receipts > 0:
        return "Ingreso parcial con backorder pendiente"
    if current_backorder > 0:
        return "Pedido pendiente en backorder"
    if estimated_receipts > 0:
        return "Posible ingreso detectado"
    if open_orders > 0:
        return "Pedido abierto en fabrica"
    if stock_delta < 0:
        return "Baja de stock; pudo venderse"
    if registered_orders > 0:
        return "Pedido registrado sin movimiento claro"
    return "Sin movimiento"


def add_historical_context(
    current_df: pd.DataFrame,
    empresa: str,
    analysis_date: date,
    current_source_hash: str,
):
    out = current_df.copy()
    baseline = load_previous_run(empresa, current_source_hash, analysis_date)
    order_history = load_order_history_by_part(empresa)

    if order_history.empty:
        order_history = pd.DataFrame(
            columns=[
                "part_no",
                "ordered_total_db",
                "received_total_db",
                "open_order_total_db",
                "last_order_at",
                "last_order_code",
                "order_batches_db",
            ]
        )
    else:
        order_history = order_history.rename(columns={"open_order_qty_db": "open_order_total_db"})

    out = out.merge(order_history, on="part_no", how="left")
    out["ordered_total_db"] = pd.to_numeric(out["ordered_total_db"], errors="coerce").fillna(0.0)
    out["received_total_db"] = pd.to_numeric(out["received_total_db"], errors="coerce").fillna(0.0)
    if "open_order_qty_db" not in out.columns:
        out["open_order_qty_db"] = 0.0
    out["open_order_qty_db"] = pd.to_numeric(out["open_order_qty_db"], errors="coerce").fillna(0.0)
    out["open_order_total_db"] = pd.to_numeric(out["open_order_total_db"], errors="coerce").fillna(0.0)
    out["order_batches_db"] = pd.to_numeric(out["order_batches_db"], errors="coerce").fillna(0.0)
    out["last_order_date"] = pd.to_datetime(out["last_order_at"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    out["last_order_code"] = out["last_order_code"].fillna("")
    out = out.drop(columns=["last_order_at"])

    if baseline is None:
        for col in [
            "stock_prev",
            "backorder_prev",
            "avg_monthly_units_prev",
            "stock_delta",
            "backorder_delta",
            "estimated_consumption_qty",
            "estimated_receipts_qty",
        ]:
            out[col] = 0.0
        out["tracking_status"] = out.apply(determine_tracking_status, axis=1)
        return out, None

    previous_items = load_run_items(baseline["id"]).rename(
        columns={
            "stock": "stock_prev",
            "backorder_qty": "backorder_prev",
            "avg_monthly_units": "avg_monthly_units_prev",
            "sales_units": "sales_units_prev",
            "monthly_order_qty": "monthly_order_qty_prev",
        }
    )

    out = out.merge(
        previous_items[["part_no", "stock_prev", "backorder_prev", "avg_monthly_units_prev"]],
        on="part_no",
        how="left",
    )

    for col in ["stock_prev", "backorder_prev", "avg_monthly_units_prev"]:
        out[col] = out[col].fillna(0)

    months_since_baseline = elapsed_months(baseline["created_at"], baseline["analysis_date"], analysis_date)
    out["stock_delta"] = out["stock"] - out["stock_prev"]
    out["backorder_delta"] = out["backorder_qty"] - out["backorder_prev"]
    out["estimated_consumption_qty"] = out["avg_monthly_units_prev"] * months_since_baseline
    projected_stock_without_receipts = (out["stock_prev"] - out["estimated_consumption_qty"]).clip(lower=0)
    out["estimated_receipts_qty"] = (out["stock"] - projected_stock_without_receipts).clip(lower=0)
    out["tracking_status"] = out.apply(determine_tracking_status, axis=1)
    return out, baseline


# =========================================================
# Render de interfaz
# =========================================================
def render_save_feedback():
    feedback = st.session_state.pop("save_feedback", None)
    if not feedback:
        return

    if feedback["status"] == "saved":
        text = f"Corrida #{feedback['run_id']} guardada."
        if feedback.get("batch_id"):
            text += f" Pedido a fabrica #{feedback['batch_id']} registrado."
        st.success(text)
    elif feedback["status"] == "duplicate":
        st.warning(f"La corrida ya existia en la base con el id #{feedback['run_id']}.")
    else:
        st.error(feedback.get("message", "No se pudo guardar la corrida."))


def render_history_sections(empresa: str):
    st.subheader("Historial guardado")
    history_df = load_recent_runs(empresa)
    if history_df.empty:
        st.info("Todavia no hay corridas historicas guardadas para esta empresa.")
    else:
        st.dataframe(history_df, use_container_width=True, height=260)

    st.subheader("Pedidos a fabrica registrados")
    batches_df = load_recent_order_batches(empresa)
    if batches_df.empty:
        st.info("Todavia no hay pedidos a fabrica registrados en la base.")
    else:
        st.dataframe(batches_df, use_container_width=True, height=240)


def main():
    st.set_page_config(page_title="Pedidos Magna", layout="wide")
    init_db()

    st.title("Pedidos Magna")
    st.caption("Analisis de inventario, pedidos y seguimiento historico en SQLite")
    render_save_feedback()

    if "empresa_seleccionada" not in st.session_state:
        st.session_state.empresa_seleccionada = None

    if st.session_state.empresa_seleccionada is None:
        st.subheader("Selecciona la empresa para consultar")
        empresa_inicio = st.radio("Empresa", ["Magna", "Alimatico SRL"], horizontal=True)
        if st.button("Continuar"):
            st.session_state.empresa_seleccionada = empresa_inicio
            st.rerun()
        st.stop()

    empresa_activa = st.session_state.empresa_seleccionada

    info_col, btn_col = st.columns([4, 1])
    info_col.success(f"Empresa activa: {empresa_activa}")
    if btn_col.button("Cambiar empresa"):
        st.session_state.empresa_seleccionada = None
        st.rerun()

    detected_sources = detect_default_source_files(APP_DIR)

    with st.sidebar:
        st.header("Parametros")
        analysis_input = st.date_input("Mes de analisis", value=date.today().replace(day=1))
        target_months = st.slider("Cobertura objetivo (meses)", 1, 24, DEFAULT_TARGET_MONTHS)
        lead_time_months = st.slider("Lead time (meses)", 1, 12, DEFAULT_LEAD_TIME_MONTHS)
        capital_available = st.number_input(
            "Capital disponible (UYU)",
            min_value=0.0,
            value=DEFAULT_CAPITAL,
            step=50000.0,
        )
        top_n = st.slider("Top productos", 5, 50, 20)
        register_current_order = st.checkbox("Registrar pedido a fabrica adjunto si existe", value=True)
        save_note = st.text_input("Nota de corrida", placeholder="Ej: segunda carga del mes")

    analysis_date = normalize_analysis_date(analysis_input)
    analysis_month = get_analysis_month(analysis_date)
    rolling_start, rolling_end = get_rolling_window(analysis_date)

    st.info(
        f"Ventana base declarada para ventas 3 anios: {rolling_start.strftime('%Y-%m')} a {rolling_end.strftime('%Y-%m')}."
    )

    st.subheader("Fuentes detectadas en carpeta")
    detected_rows = []
    for label, key in [
        ("Ventas 3 anios", "ventas"),
        ("Inventario", "inventario"),
        ("Backorder", "backorder"),
        ("Pedido a fabrica", "pedido_fabrica"),
    ]:
        source = detected_sources.get(key)
        detected_rows.append(
            {
                "tipo": label,
                "archivo": source.name if source else "No encontrado",
                "ruta": str(source.path) if source else "",
            }
        )
    st.dataframe(pd.DataFrame(detected_rows), use_container_width=True, hide_index=True)

    st.subheader("Opcional: reemplazar archivos detectados")
    upload_col_1, upload_col_2 = st.columns(2)
    ventas_upload = upload_col_1.file_uploader("Ventas 3 anios", type=["xls", "xlsx"])
    inventario_upload = upload_col_2.file_uploader("Inventario", type=["xls", "xlsx"])
    upload_col_3, upload_col_4 = st.columns(2)
    backorder_upload = upload_col_3.file_uploader("Backorder", type=["xls", "xlsx"])
    pedido_upload = upload_col_4.file_uploader("Pedido a fabrica", type=["xls", "xlsx"])

    ventas_file = resolve_source_file(ventas_upload, detected_sources["ventas"])
    inventario_file = resolve_source_file(inventario_upload, detected_sources["inventario"])
    backorder_file = resolve_source_file(backorder_upload, detected_sources["backorder"])
    pedido_file = resolve_source_file(pedido_upload, detected_sources["pedido_fabrica"])

    render_history_sections(empresa_activa)

    if not all([ventas_file, inventario_file, backorder_file]):
        st.info("Para calcular el pedido a solicitar necesitas cargar Ventas 3 anios, Inventario y Backorder. El Pedido a fabrica es opcional.")
        st.stop()

    source_hash = build_source_hash(
        empresa_activa,
        analysis_month,
        target_months,
        lead_time_months,
        capital_available,
        ventas_file,
        inventario_file,
        backorder_file,
        pedido_file,
    )
    order_file_hash = build_file_hash(pedido_file) if pedido_file is not None else ""
    open_orders_df = load_open_factory_orders_by_part(
        empresa_activa,
        exclude_order_file_hash=order_file_hash if order_file_hash else None,
    )

    try:
        sales_df = load_sales(ventas_file)
        stock_df = load_inventory(inventario_file)
        backorder_df = load_backorder(backorder_file)
        order_df = load_monthly_order(pedido_file) if pedido_file is not None else empty_monthly_order()

        final_df = build_analysis_dataframe(
            sales_df=sales_df,
            stock_df=stock_df,
            backorder_df=backorder_df,
            order_df=order_df,
            open_orders_df=open_orders_df,
            target_months=target_months,
            lead_time_months=lead_time_months,
            capital_available=capital_available,
            empresa=empresa_activa,
        )
        final_df, baseline = add_historical_context(
            current_df=final_df,
            empresa=empresa_activa,
            analysis_date=analysis_date,
            current_source_hash=source_hash,
        )
    except Exception as exc:
        st.error(f"Error procesando archivos: {exc}")
        st.stop()

    action_col_1, action_col_2 = st.columns([2, 5])
    if action_col_1.button("Guardar corrida en base", type="primary"):
        try:
            result = save_analysis_run(
                empresa=empresa_activa,
                analysis_date=analysis_date,
                target_months=target_months,
                lead_time_months=lead_time_months,
                capital_available=capital_available,
                ventas_file=ventas_file,
                inventario_file=inventario_file,
                backorder_file=backorder_file,
                pedido_file=pedido_file,
                sales_df=sales_df,
                stock_df=stock_df,
                backorder_df=backorder_df,
                order_df=order_df,
                final_df=final_df,
                source_hash=source_hash,
                order_file_hash=order_file_hash,
                register_current_order=register_current_order,
                notes=save_note,
            )
            st.session_state["save_feedback"] = result
            st.rerun()
        except Exception as exc:
            st.session_state["save_feedback"] = {
                "status": "error",
                "message": f"No se pudo guardar la corrida: {exc}",
            }
            st.rerun()

    baseline_text = "Sin corrida historica previa guardada."
    if baseline is not None:
        baseline_text = (
            f"Comparando contra la corrida #{baseline['id']} del {baseline['analysis_month']} "
            f"guardada el {pd.to_datetime(baseline['created_at']).strftime('%Y-%m-%d %H:%M')}."
        )
    action_col_2.caption(baseline_text)

    brand_options = ["Todos"] + sorted(final_df["brand"].dropna().unique().tolist())
    status_options = ["Todos"] + sorted(final_df["status"].dropna().unique().tolist())
    abc_options = ["Todos", "A", "B", "C"]

    filter_col_1, filter_col_2, filter_col_3, filter_col_4 = st.columns(4)
    selected_brand = filter_col_1.selectbox("Marca", brand_options)
    selected_status = filter_col_2.selectbox("Estado", status_options)
    selected_abc = filter_col_3.selectbox("ABC", abc_options)
    search_text = filter_col_4.text_input("Buscar codigo o descripcion")

    view = final_df.copy()
    if selected_brand != "Todos":
        view = view[view["brand"] == selected_brand]
    if selected_status != "Todos":
        view = view[view["status"] == selected_status]
    if selected_abc != "Todos":
        view = view[view["abc"] == selected_abc]
    if search_text:
        term = search_text.strip().upper()
        view = view[
            view["part_no"].astype(str).str.upper().str.contains(term, na=False)
            | view["description"].astype(str).str.upper().str.contains(term, na=False)
        ]

    metric_col_1, metric_col_2, metric_col_3, metric_col_4, metric_col_5, metric_col_6 = st.columns(6)
    metric_col_1.metric("Items", f"{len(view):,}")
    metric_col_2.metric("Stock muerto", f"{int(view['stock_muerto'].sum()):,}")
    metric_col_3.metric("Ofertas", f"{int(view['oferta_sugerida'].sum()):,}")
    metric_col_4.metric("Pedido archivo", f"{int(view['monthly_order_qty'].sum()):,}")
    metric_col_5.metric("Compra sugerida", f"{int(view['suggested_order_qty'].sum()):,}")
    metric_col_6.metric("Abierto DB", f"{int(view['open_order_qty_db'].sum()):,}")

    st.subheader("Resumen por marca")
    summary_brand = (
        view.groupby("brand", as_index=False)
        .agg(
            items=("part_no", "count"),
            ventas_3y=("sales_units", "sum"),
            stock=("stock", "sum"),
            backorder=("backorder_qty", "sum"),
            pedido_archivo=("monthly_order_qty", "sum"),
            abierto_db=("open_order_qty_db", "sum"),
            compra_inteligente=("intelligent_buy_qty", "sum"),
        )
        .sort_values("ventas_3y", ascending=False)
    )
    st.dataframe(summary_brand, use_container_width=True)

    st.subheader("Resumen ABC")
    summary_abc = (
        view.groupby("abc", as_index=False)
        .agg(
            items=("part_no", "count"),
            ventas_base=("sales_units", "sum"),
            stock=("stock", "sum"),
            pedido_archivo=("monthly_order_qty", "sum"),
            abierto_db=("open_order_qty_db", "sum"),
            sugerido=("suggested_order_qty", "sum"),
        )
        .sort_values("abc")
    )
    st.dataframe(summary_abc, use_container_width=True)

    st.subheader("Top productos por ventas")
    top_sales = view.sort_values("sales_units", ascending=False).head(top_n)
    st.dataframe(
        top_sales[
            [
                "empresa",
                "part_no",
                "description",
                "brand",
                "sales_units",
                "stock",
                "backorder_qty",
                "monthly_order_qty",
                "open_order_qty_db",
                "months_of_stock",
                "abc",
                "status",
            ]
        ],
        use_container_width=True,
    )

    if not top_sales.empty:
        fig, ax = plt.subplots(figsize=(10, 5))
        plot_df = top_sales.head(15)
        ax.bar(plot_df["part_no"], plot_df["sales_units"])
        ax.set_title("Top 15 por unidades vendidas")
        ax.set_xlabel("Codigo")
        ax.set_ylabel("Unidades")
        ax.tick_params(axis="x", rotation=60)
        fig.tight_layout()
        st.pyplot(fig)

    st.subheader("Pedido inteligente")
    pedido_inteligente = view[view["selected_for_purchase"]].copy()
    pedido_inteligente = pedido_inteligente.sort_values(
        ["smart_score", "intelligent_buy_cost"],
        ascending=[False, False],
    )
    st.dataframe(
        pedido_inteligente[
            [
                "empresa",
                "part_no",
                "description",
                "brand",
                "abc",
                "status",
                "stock",
                "backorder_qty",
                "monthly_order_qty",
                "open_order_qty_db",
                "suggested_order_qty",
                "intelligent_buy_qty",
                "estimated_unit_cost",
                "intelligent_buy_cost",
                "estimated_gross_profit",
                "smart_score",
            ]
        ],
        use_container_width=True,
        height=420,
    )

    st.subheader("Pedido a solicitar a Mazda")
    pedido_mazda_df = build_mazda_order_to_request(pedido_inteligente)
    if pedido_mazda_df.empty:
        st.info("No hay piezas Mazda seleccionadas para pedir con los parametros actuales.")
    else:
        st.dataframe(pedido_mazda_df, use_container_width=True, height=320)
        st.download_button(
            "Descargar pedido a solicitar a Mazda",
            data=dataframe_to_excel_bytes(pedido_mazda_df),
            file_name=f"pedido_a_solicitar_mazda_{analysis_month}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    st.subheader("Seguimiento historico de pedidos")
    tracking_view = view[
        (view["ordered_total_db"] > 0)
        | (view["monthly_order_qty"] > 0)
        | (view["backorder_qty"] > 0)
        | (view["estimated_receipts_qty"] > 0)
        | (view["stock_delta"] != 0)
    ].copy()
    tracking_view = tracking_view.sort_values(
        ["ordered_total_db", "monthly_order_qty", "estimated_receipts_qty", "backorder_qty"],
        ascending=[False, False, False, False],
    )

    tracking_cols = [
        "empresa",
        "part_no",
        "description",
        "brand",
        "ordered_total_db",
        "received_total_db",
        "open_order_qty_db",
        "order_batches_db",
        "last_order_code",
        "last_order_date",
        "monthly_order_qty",
        "stock_prev",
        "stock",
        "stock_delta",
        "backorder_prev",
        "backorder_qty",
        "backorder_delta",
        "estimated_consumption_qty",
        "estimated_receipts_qty",
        "tracking_status",
    ]
    if tracking_view.empty:
        st.info("No hay items con seguimiento historico para mostrar en esta corrida.")
    else:
        st.dataframe(tracking_view[tracking_cols], use_container_width=True, height=420)

    st.subheader("Stock muerto")
    stock_muerto_df = view[view["stock_muerto"]].copy()
    st.dataframe(
        stock_muerto_df[["empresa", "part_no", "description", "brand", "stock", "months_of_stock"]],
        use_container_width=True,
        height=280,
    )

    st.subheader("Ofertas sugeridas")
    ofertas_df = view[view["oferta_sugerida"]].copy()
    st.dataframe(
        ofertas_df[
            [
                "empresa",
                "part_no",
                "description",
                "brand",
                "stock",
                "months_of_stock",
                "sales_units",
                "abc",
            ]
        ],
        use_container_width=True,
        height=280,
    )

    st.subheader("Detalle completo")
    detail_cols = [
        "empresa",
        "part_no",
        "description",
        "brand",
        "sales_units",
        "sales_uyu",
        "cost_uyu",
        "avg_monthly_units",
        "avg_annual_units",
        "stock_prev",
        "stock",
        "stock_delta",
        "backorder_prev",
        "backorder_qty",
        "backorder_delta",
        "monthly_order_qty",
        "ordered_total_db",
        "received_total_db",
        "open_order_qty_db",
        "order_batches_db",
        "last_order_code",
        "last_order_date",
        "pipeline_qty",
        "available_plus_pipeline",
        "months_of_stock",
        "target_stock_qty",
        "lead_time_need_qty",
        "suggested_order_qty",
        "abc",
        "status",
        "tracking_status",
        "estimated_consumption_qty",
        "estimated_receipts_qty",
        "stock_muerto",
        "oferta_sugerida",
        "estimated_unit_cost",
        "intelligent_buy_qty",
        "intelligent_buy_cost",
        "estimated_gross_profit",
        "smart_score",
    ]
    st.dataframe(view[detail_cols], use_container_width=True, height=520)

    history_export_df = load_recent_runs(empresa_activa, limit=100)
    batches_export_df = load_recent_order_batches(empresa_activa, limit=100)

    excel_bytes = to_excel_bytes(
        {
            "pedido_inteligente": pedido_inteligente[detail_cols],
            "pedido_a_solicitar_mazda": pedido_mazda_df,
            "seguimiento_pedidos": tracking_view[tracking_cols] if not tracking_view.empty else pd.DataFrame(columns=tracking_cols),
            "stock_muerto": stock_muerto_df[detail_cols],
            "ofertas": ofertas_df[detail_cols],
            "resumen_marca": summary_brand,
            "resumen_abc": summary_abc,
            "historial_corridas": history_export_df,
            "lotes_pedidos": batches_export_df,
            "detalle_completo": view[detail_cols],
        }
    )

    st.download_button(
        "Descargar analisis en Excel",
        data=excel_bytes,
        file_name=f"pedidos_magna_{analysis_month}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.success("Analisis generado. Puedes guardarlo para que quede registrado en la base historica.")


if __name__ == "__main__":
    main()
