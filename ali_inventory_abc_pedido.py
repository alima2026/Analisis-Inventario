import math
import re
from io import BytesIO

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


st.set_page_config(page_title="Ali Inventory", layout="wide")


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
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def detect_brand(part_no: str, description: str = "") -> str:
    p = normalize_part(part_no)
    d = str(description).upper().strip()

    # MAZDA
    # Ej: B631-14-302A / 0000-0000A
    if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{2}-[A-Z0-9]{3}[A-Z]?", p):
        return "Mazda"
    if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}[A-Z]?", p):
        return "Mazda"

    # KIA / HYUNDAI
    # Ej: 77004E500 / 555133N100
    if re.fullmatch(r"[0-9]{5}[A-Z][0-9]{3}", p):
        return "Kia/Hyundai"
    if re.fullmatch(r"[0-9]{6}[A-Z][0-9]{3}", p):
        return "Kia/Hyundai"

    # BMW / MINI
    if (
        p.startswith(("11", "12", "13", "16", "17", "18", "31", "32", "33", "34", "51", "61", "64"))
        and len(p) in [7, 11]
    ):
        return "BMW/MINI"
    if "BMW" in d or "MINI" in d:
        return "BMW/MINI"

    # MULTIMARCA
    # Ej: ATA.MICRO / A20-32 / ACIM026 / WL7070
    if "." in p:
        return "Multimarca"
    if re.fullmatch(r"[A-Z]{1,6}[0-9]{2,6}[A-Z0-9-]*", p):
        return "Multimarca"
    if re.fullmatch(r"[A-Z0-9]{1,6}-[A-Z0-9]{1,6}", p):
        return "Multimarca"

    if "MAZDA" in d:
        return "Mazda"
    if "KIA" in d or "HYUNDAI" in d:
        return "Kia/Hyundai"

    return "Multimarca"


def classify_abc(df: pd.DataFrame, value_col: str = "sales_uyu") -> pd.DataFrame:
    abc = df.copy()
    abc = abc.sort_values(value_col, ascending=False).reset_index(drop=True)
    total_value = abc[value_col].sum()

    if total_value <= 0:
        abc["abc"] = "C"
        return abc

    abc["pct"] = abc[value_col] / total_value
    abc["pct_acum"] = abc["pct"].cumsum()

    def label(p):
        if p <= 0.80:
            return "A"
        if p <= 0.95:
            return "B"
        return "C"

    abc["abc"] = abc["pct_acum"].apply(label)
    return abc


def to_excel_bytes(sheets: dict) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()


# =========================================================
# Lectura de archivos
# =========================================================
def load_sales(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file, header=[0, 1])

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
    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)
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
    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)

    return df.groupby(["part_no", "description", "brand"], as_index=False)["stock"].sum()


def load_backorder(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file)
    df = df.copy()

    part_col = "Buyer Part" if "Buyer Part" in df.columns else "Seller Part"
    desc_col = "Description" if "Description" in df.columns else None

    df["part_no"] = df[part_col].map(normalize_part)
    df["description"] = df[desc_col].astype(str).str.strip() if desc_col else ""

    for col in [
        "Under Investigation Qty",
        "Expected First Allocation Qty",
        "Expected Last Allocation Qty",
        "Allocation Qty",
        "Seller Part Qty",
    ]:
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
    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)

    return df.groupby(["part_no", "description", "brand"], as_index=False)["backorder_qty"].sum()


def load_monthly_order(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file)
    df = df.copy()

    part_col = "PART NO" if "PART NO" in df.columns else df.columns[0]
    qty_col = "PCS" if "PCS" in df.columns else df.columns[1]

    df["part_no"] = df[part_col].map(normalize_part)
    df["monthly_order_qty"] = safe_numeric(df[qty_col])
    df = df[df["part_no"] != ""].copy()

    return df.groupby("part_no", as_index=False)["monthly_order_qty"].sum()


# =========================================================
# Motor principal
# =========================================================
def merge_all(
    sales: pd.DataFrame,
    stock: pd.DataFrame,
    bo: pd.DataFrame,
    order: pd.DataFrame,
) -> pd.DataFrame:
    base = sales[
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

    merged = (
        base.merge(stock[["part_no", "stock"]], on="part_no", how="outer")
        .merge(bo[["part_no", "backorder_qty"]], on="part_no", how="left")
        .merge(order[["part_no", "monthly_order_qty"]], on="part_no", how="left")
    )

    merged["part_no"] = merged["part_no"].fillna("").map(normalize_part)
    merged["description"] = merged["description"].fillna("")

    merged["brand"] = merged.apply(
        lambda r: r["brand"]
        if pd.notna(r["brand"]) and str(r["brand"]).strip() != ""
        else detect_brand(r["part_no"], r["description"]),
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
        merged[col] = merged[col].fillna(0)

    merged["pipeline_qty"] = merged["backorder_qty"] + merged["monthly_order_qty"]
    merged["available_plus_pipeline"] = merged["stock"] + merged["pipeline_qty"]

    merged["unit_margin_uyu"] = (
        (merged["sales_uyu"] - merged["cost_uyu"])
        / merged["sales_units"].replace(0, pd.NA)
    )
    merged["unit_margin_uyu"] = merged["unit_margin_uyu"].fillna(0)

    return merged


def add_inventory_logic(df: pd.DataFrame, target_months: int, lead_time_months: int) -> pd.DataFrame:
    out = df.copy()

    out["months_of_stock"] = out["stock"] / out["avg_monthly_units"].replace(0, pd.NA)
    out["months_of_stock"] = out["months_of_stock"].fillna(999)

    out["target_stock_qty"] = (out["avg_monthly_units"] * target_months).apply(math.ceil)
    out["lead_time_need_qty"] = (out["avg_monthly_units"] * lead_time_months).apply(math.ceil)

    out["suggested_order_qty"] = (
        out["target_stock_qty"] - out["available_plus_pipeline"]
    ).clip(lower=0).apply(math.ceil)

    def define_status(row):
        if row["sales_units"] <= 0 and row["stock"] > 0:
            return "Stock muerto"
        if row["sales_units"] <= 0 and row["stock"] <= 0:
            return "Sin historial"
        if row["available_plus_pipeline"] <= 0:
            return "Crítico"
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
    abc_df = classify_abc(df[["part_no", "sales_uyu"]].copy(), value_col="sales_uyu")
    out = df.merge(abc_df[["part_no", "abc"]], on="part_no", how="left")
    out["abc"] = out["abc"].fillna("C")
    return out


def add_intelligent_order(df: pd.DataFrame, capital: float) -> pd.DataFrame:
    out = df.copy()

    abc_score = {"A": 100, "B": 70, "C": 40}
    status_score = {
        "Crítico": 100,
        "Comprar ya": 80,
        "Comprar": 60,
        "OK": 20,
        "Sin historial": 10,
        "Stock muerto": 0,
    }

    out["abc_score"] = out["abc"].map(abc_score).fillna(40)
    out["status_score"] = out["status"].map(status_score).fillna(20)
    out["rotation_score"] = out["avg_monthly_units"].fillna(0) * 10
    out["margin_score"] = out["unit_margin_uyu"].fillna(0) / 100

    out["smart_score"] = (
        out["abc_score"] * 0.35
        + out["status_score"] * 0.35
        + out["rotation_score"] * 0.20
        + out["margin_score"] * 0.10
    )

    out["estimated_unit_cost"] = 0.0
    mask_cost = out["sales_units"] > 0
    out.loc[mask_cost, "estimated_unit_cost"] = (
        out.loc[mask_cost, "cost_uyu"] / out.loc[mask_cost, "sales_units"]
    )
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
    buy_qty = []
    buy_cost = []

    for _, row in candidates.iterrows():
        qty = int(row["suggested_order_qty"])
        unit_cost = float(row["estimated_unit_cost"])

        if qty <= 0:
            buy_qty.append(0)
            buy_cost.append(0.0)
            continue

        if unit_cost <= 0:
            buy_qty.append(qty)
            buy_cost.append(0.0)
            continue

        max_affordable = int(remaining_capital // unit_cost)
        final_qty = min(qty, max_affordable)
        final_cost = final_qty * unit_cost

        buy_qty.append(final_qty)
        buy_cost.append(final_cost)
        remaining_capital -= final_cost

    candidates["intelligent_buy_qty"] = buy_qty
    candidates["intelligent_buy_cost"] = buy_cost
    candidates["selected_for_purchase"] = candidates["intelligent_buy_qty"] > 0

    out = out.merge(
        candidates[
            ["part_no", "intelligent_buy_qty", "intelligent_buy_cost", "selected_for_purchase"]
        ],
        on="part_no",
        how="left",
    )

    out["intelligent_buy_qty"] = out["intelligent_buy_qty"].fillna(0)
    out["intelligent_buy_cost"] = out["intelligent_buy_cost"].fillna(0)
    out["selected_for_purchase"] = out["selected_for_purchase"].fillna(False)

    return out


# =========================================================
# Interfaz
# =========================================================
st.title("Ali Inventory")
st.caption("Pedido inteligente, ABC, stock muerto y ofertas")

if "empresa_seleccionada" not in st.session_state:
    st.session_state.empresa_seleccionada = None

if st.session_state.empresa_seleccionada is None:
    st.subheader("Seleccioná la empresa para consultar")
    empresa_inicio = st.radio("Empresa", ["Magna", "Alimatico SRL"], horizontal=True)
    if st.button("Continuar"):
        st.session_state.empresa_seleccionada = empresa_inicio
        st.rerun()
    st.stop()

empresa_activa = st.session_state
