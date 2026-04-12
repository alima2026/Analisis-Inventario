import math
import os
import re
from typing import Optional

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


st.set_page_config(page_title="Ali Inventory - Reposición", layout="wide")

DEFAULT_SALES = "/mnt/data/ventas_de_3años.xls"
DEFAULT_STOCK = "/mnt/data/inventario_19032026.xls"
DEFAULT_BACKORDER = "/mnt/data/backorder.xls"
DEFAULT_ORDER = "/mnt/data/HCCA.xlsx"


# =========================
# Utilidades
# =========================
def normalize_part(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip().upper()
    text = re.sub(r"\s+", "", text)
    text = text.replace("*", "")
    return text


def safe_numeric(series: pd.Series) -> pd.Series:
    cleaned = (
        series.astype(str)
        .str.replace(".", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.replace(" ", "", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def detect_brand(part_no: str, description: str = "") -> str:
    p = normalize_part(part_no)
    d = str(description).upper().strip()

    # -------------------------
    # Reglas Mazda
    # Ejemplos:
    # B631-14-302A
    # 0000-0000A
    # -------------------------
    if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{2}-[A-Z0-9]{3}[A-Z]?", p):
        return "Mazda"
    if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}[A-Z]?", p):
        return "Mazda"

    # -------------------------
    # Reglas Kia / Hyundai
    # Ejemplos:
    # 77004E500
    # 555133N100
    # -------------------------
    if re.fullmatch(r"[0-9]{5}[A-Z][0-9]{3}", p):
        return "Kia/Hyundai"
    if re.fullmatch(r"[0-9]{6}[A-Z][0-9]{3}", p):
        return "Kia/Hyundai"

    # -------------------------
    # Multimarca / Alimatico
    # Ejemplos:
    # ATA.MICRO
    # A20-32
    # ACIM026
    # WL7070
    # -------------------------
    if "." in p:
        return "Multimarca"
    if re.fullmatch(r"[A-Z]{1,4}[0-9]{2,6}[A-Z0-9-]*", p):
        return "Multimarca"
    if re.fullmatch(r"[A-Z0-9]{1,5}-[A-Z0-9]{1,5}", p):
        return "Multimarca"

    # Reglas antiguas de apoyo
    if p.startswith(("PE", "B6", "KD", "DG", "D0", "PA", "N2", "N3", "BJS", "BLD", "GJ", "GH")):
        return "Mazda"
    if p.startswith(("263", "319", "0K", "OK", "495", "517", "528", "546", "58", "86", "97", "HY")):
        return "Kia/Hyundai"

    if "MAZDA" in d:
        return "Mazda"
    if "KIA" in d or "HYUNDAI" in d:
        return "Kia/Hyundai"

    return "Multimarca"


# =========================
# Carga de archivos exactos del usuario
# =========================
def load_sales(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, header=[0, 1])
    df.columns = [
        "part_no", "description", "unit", "sales_units", "bonif_units", "net_units",
        "sample_units", "sales_uyu", "sales_usd", "sales_pct", "cost_uyu", "cost_usd", "cost_pct"
    ]
    df = df.copy()
    df["part_no"] = df["part_no"].map(normalize_part)
    df["description"] = df["description"].astype(str).str.strip()
    for col in ["sales_units", "bonif_units", "net_units", "sample_units", "sales_uyu", "sales_usd", "cost_uyu", "cost_usd"]:
        df[col] = safe_numeric(df[col])
    df = df[df["part_no"] != ""].copy()
    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)
    df["avg_monthly_units"] = df["sales_units"] / 36.0
    df["avg_annual_units"] = df["sales_units"] / 3.0
    return df


def load_inventory(path: str) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None)
    df = raw.iloc[5:, [2, 8, 16, 20]].copy()
    df.columns = ["part_no", "description", "unit", "stock"]
    df = df.dropna(subset=["part_no"]).copy()
    df["part_no"] = df["part_no"].map(normalize_part)
    df["description"] = df["description"].astype(str).str.strip()
    df["stock"] = safe_numeric(df["stock"])
    df = df[df["part_no"] != ""].copy()
    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)
    return df.groupby(["part_no", "description", "brand"], as_index=False)["stock"].sum()


def load_backorder(path: str) -> pd.DataFrame:
    df = pd.read_excel(path)
    df = df.copy()
    part_col = "Buyer Part" if "Buyer Part" in df.columns else "Seller Part"
    df["part_no"] = df[part_col].map(normalize_part)
    df["description"] = df.get("Description", "").astype(str).str.strip()

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
    if "Under Investigation Qty" in df.columns:
        df["backorder_qty"] += df["Under Investigation Qty"]
    if "Expected First Allocation Qty" in df.columns:
        df["backorder_qty"] += df["Expected First Allocation Qty"]
    if "Expected Last Allocation Qty" in df.columns:
        df["backorder_qty"] += df["Expected Last Allocation Qty"]
    if "Allocation Qty" in df.columns:
        df["backorder_qty"] += df["Allocation Qty"]

    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)
    out = df.groupby(["part_no", "description", "brand"], as_index=False)["backorder_qty"].sum()
    return out


def load_monthly_order(path: str) -> pd.DataFrame:
    df = pd.read_excel(path)
    df = df.copy()
    df["part_no"] = df["PART NO"].map(normalize_part)
    df["monthly_order_qty"] = safe_numeric(df["PCS"])
    df["brand"] = df["part_no"].map(lambda x: detect_brand(x, ""))
    out = df.groupby(["part_no", "brand"], as_index=False)["monthly_order_qty"].sum()
    return out


def merge_all(sales: pd.DataFrame, stock: pd.DataFrame, bo: pd.DataFrame, order: pd.DataFrame) -> pd.DataFrame:
    base = sales[[
        "part_no", "description", "brand", "sales_units", "sales_uyu", "cost_uyu",
        "avg_monthly_units", "avg_annual_units"
    ]].copy()

    merged = base.merge(
        stock[["part_no", "stock"]], on="part_no", how="left"
    ).merge(
        bo[["part_no", "backorder_qty"]], on="part_no", how="left"
    ).merge(
        order[["part_no", "monthly_order_qty"]], on="part_no", how="left"
    )

    merged["stock"] = merged["stock"].fillna(0)
    merged["backorder_qty"] = merged["backorder_qty"].fillna(0)
    merged["monthly_order_qty"] = merged["monthly_order_qty"].fillna(0)
    merged["pipeline_qty"] = merged["backorder_qty"] + merged["monthly_order_qty"]
    merged["available_plus_pipeline"] = merged["stock"] + merged["pipeline_qty"]
    merged["unit_margin_uyu"] = (merged["sales_uyu"] - merged["cost_uyu"]) / merged["sales_units"].replace(0, pd.NA)
    merged["unit_margin_uyu"] = merged["unit_margin_uyu"].fillna(0)
        return merged


def add_replenishment_logic(df: pd.DataFrame, target_months: int, lead_time_months: int) -> pd.DataFrame:
    out = df.copy()
    out["months_of_stock"] = out["stock"] / out["avg_monthly_units"].replace(0, pd.NA)
    out["months_of_stock"] = out["months_of_stock"].fillna(999)
    out["target_stock_qty"] = (out["avg_monthly_units"] * target_months).apply(math.ceil)
    out["lead_time_need_qty"] = (out["avg_monthly_units"] * lead_time_months).apply(math.ceil)
    out["suggested_order_qty"] = (out["target_stock_qty"] - out["available_plus_pipeline"]).clip(lower=0).apply(math.ceil)

    def classify(row):
        if row["avg_monthly_units"] <= 0 and row["stock"] > 0:
            return "Sin venta / revisar"
        if row["avg_monthly_units"] <= 0 and row["stock"] <= 0:
            return "Sin historial"
        if row["available_plus_pipeline"] <= 0:
            return "Crítico"
        if row["available_plus_pipeline"] < row["lead_time_need_qty"]:
            return "Comprar ya"
        if row["available_plus_pipeline"] < row["target_stock_qty"]:
            return "Comprar"
        return "OK"

    out["status"] = out.apply(classify, axis=1)
    return out


def to_excel_bytes(df_detail: pd.DataFrame, df_summary: pd.DataFrame) -> bytes:
    from io import BytesIO
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_detail.to_excel(writer, sheet_name="detalle", index=False)
        df_summary.to_excel(writer, sheet_name="resumen", index=False)
    return output.getvalue()


# =========================
# UI
# =========================
st.title("Ali Inventory - Reposición inteligente")
st.caption("Integra ventas 3 años + inventario + backorder + pedido mensual")

# Selección inicial de empresa
if "empresa_seleccionada" not in st.session_state:
    st.session_state.empresa_seleccionada = None

if st.session_state.empresa_seleccionada is None:
    st.subheader("Seleccioná la empresa para consultar")
    empresa_inicio = st.radio(
        "Empresa",
        ["Magna", "Alimatico SRL"],
        horizontal=True,
    )
    if st.button("Continuar"):
        st.session_state.empresa_seleccionada = empresa_inicio
        st.rerun()
    st.stop()

empresa_activa = st.session_state.empresa_seleccionada

col_empresa_a, col_empresa_b = st.columns([3, 1])
col_empresa_a.info(f"Empresa activa: {empresa_activa}")
if col_empresa_b.button("Cambiar empresa"):
    st.session_state.empresa_seleccionada = None
    st.rerun()

with st.sidebar:
    st.header("Parámetros")
    target_months = st.slider("Cobertura objetivo (meses)", 1, 12, 6)
    lead_time_months = st.slider("Lead time / demora (meses)", 1, 12, 6)
    top_n = st.slider("Top productos", 5, 50, 20)
    st.markdown("---")
    st.write("El sistema intenta usar tus archivos cargados automáticamente.")


def file_exists(path: str) -> bool:
    return os.path.exists(path)

sales_path = DEFAULT_SALES
stock_path = DEFAULT_STOCK
bo_path = DEFAULT_BACKORDER
order_path = DEFAULT_ORDER

if not all(file_exists(p) for p in [sales_path, stock_path, bo_path, order_path]):
    st.warning("Faltan uno o más archivos por defecto. Subilos manualmente.")
    up_sales = st.file_uploader("Ventas 3 años", type=["xls", "xlsx"], key="sales")
    up_stock = st.file_uploader("Inventario", type=["xls", "xlsx"], key="stock")
    up_bo = st.file_uploader("Backorder", type=["xls", "xlsx"], key="bo")
    up_order = st.file_uploader("Pedido mensual", type=["xls", "xlsx"], key="order")
    if not all([up_sales, up_stock, up_bo, up_order]):
        st.stop()
    sales_path, stock_path, bo_path, order_path = up_sales, up_stock, up_bo, up_order

try:
    sales_df = load_sales(sales_path)
    stock_df = load_inventory(stock_path)
    bo_df = load_backorder(bo_path)
    order_df = load_monthly_order(order_path)
    merged = merge_all(sales_df, stock_df, bo_df, order_df)
    final_df = add_replenishment_logic(merged, target_months, lead_time_months)
except Exception as e:
    st.error(f"Error procesando archivos: {e}")
    st.stop()

brand_options = ["Todos"] + sorted(final_df["brand"].dropna().unique().tolist())
status_options = ["Todos"] + sorted(final_df["status"].dropna().unique().tolist())

c1, c2, c3 = st.columns(3)
selected_brand = c1.selectbox("Marca", brand_options)
selected_status = c2.selectbox("Estado", status_options)
search_part = c3.text_input("Buscar código o descripción")

view = final_df.copy()
view["empresa"] = empresa_activa
if selected_brand != "Todos":
    view = view[view["brand"] == selected_brand]
if selected_status != "Todos":
    view = view[view["status"] == selected_status]
if search_part:
    term = search_part.strip().upper()
    view = view[
        view["part_no"].str.contains(term, na=False)
        | view["description"].str.upper().str.contains(term, na=False)
    ]

k1, k2, k3, k4 = st.columns(4)
k1.metric("Ítems analizados", f"{len(view):,}")
k2.metric("Comprar ya", f"{(view['status'] == 'Comprar ya').sum():,}")
k3.metric("Compra sugerida total", f"{int(view['suggested_order_qty'].sum()):,}")
k4.metric("Stock total", f"{int(view['stock'].sum()):,}")

st.subheader("Resumen por estado")
summary = (
    view.groupby("status", as_index=False)
    .agg(
        items=("part_no", "count"),
        stock=("stock", "sum"),
        pipeline=("pipeline_qty", "sum"),
        sugerido=("suggested_order_qty", "sum"),
        ventas_3y=("sales_units", "sum"),
    )
    .sort_values("sugerido", ascending=False)
)
st.dataframe(summary, use_container_width=True)

st.subheader("Top repuestos por ventas (3 años)")
top_sales = view.sort_values("sales_units", ascending=False).head(top_n)[[
    "part_no", "description", "brand", "sales_units", "avg_monthly_units", "stock", "pipeline_qty", "months_of_stock", "status"
]]
st.dataframe(top_sales, use_container_width=True)

fig1, ax1 = plt.subplots(figsize=(10, 5))
ax1.bar(top_sales["part_no"].head(15), top_sales["sales_units"].head(15))
ax1.set_title("Top 15 por unidades vendidas en 3 años")
ax1.set_xlabel("Código")
ax1.set_ylabel("Unidades")
ax1.tick_params(axis="x", rotation=60)
fig1.tight_layout()
st.pyplot(fig1)

st.subheader("Repuestos para comprar ya")
urgent = view[view["status"].isin(["Crítico", "Comprar ya", "Comprar"])].copy()
urgent = urgent.sort_values(["status", "suggested_order_qty", "sales_units"], ascending=[True, False, False])
show_cols = [
    "empresa", "part_no", "description", "brand", "sales_units", "avg_monthly_units", "stock",
    "backorder_qty", "monthly_order_qty", "available_plus_pipeline",
    "target_stock_qty", "suggested_order_qty", "months_of_stock", "status"
]
st.dataframe(urgent[show_cols], use_container_width=True, height=500)

st.subheader("Detalle completo")
st.dataframe(view[show_cols + ["sales_uyu", "cost_uyu", "unit_margin_uyu"]], use_container_width=True, height=500)

excel_bytes = to_excel_bytes(
    view[show_cols + ["sales_uyu", "cost_uyu", "unit_margin_uyu"]],
    summary,
)
st.download_button(
    "Descargar análisis en Excel",
    data=excel_bytes,
    file_name="ali_inventory_reposicion.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

st.success("Listo. El próximo paso es agregar ranking ABC, stock muerto y sugerencia de pedido eficiente por proveedor.")
