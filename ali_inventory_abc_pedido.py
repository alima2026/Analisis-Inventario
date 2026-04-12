
import math
import os
import re
from io import BytesIO

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


st.set_page_config(page_title="Ali Inventory - Reposición ABC", layout="wide")

DEFAULT_SALES = "/mnt/data/ventas_de_3años.xls"
DEFAULT_STOCK = "/mnt/data/inventario_19032026.xls"
DEFAULT_BACKORDER = "/mnt/data/backorder.xls"
DEFAULT_ORDER = "/mnt/data/HCCA.xlsx"


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
        .str.replace("$", "", regex=False)
        .str.replace("U$S", "", regex=False)
        .str.replace("UYU", "", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def detect_brand(part_no: str, description: str = "") -> str:
    p = normalize_part(part_no)
    d = str(description).upper()

    if p.startswith(("PE", "B6", "KD", "DG", "D0", "PA", "N2", "N3", "BJS", "BLD", "GJ", "GH")):
        return "Mazda"
    if p.startswith(("263", "319", "0K", "OK", "495", "517", "528", "546", "58", "86", "97", "HY")):
        return "Kia/Hyundai"
    if "MAZDA" in d:
        return "Mazda"
    if "KIA" in d or "HYUNDAI" in d:
        return "Kia/Hyundai"
    return "Otros"


def load_sales(path) -> pd.DataFrame:
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
    df["avg_monthly_sales_uyu"] = df["sales_uyu"] / 36.0
    return df


def load_inventory(path) -> pd.DataFrame:
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


def load_backorder(path) -> pd.DataFrame:
    df = pd.read_excel(path)
    part_col = "Buyer Part" if "Buyer Part" in df.columns else "Seller Part"
    df["part_no"] = df[part_col].map(normalize_part)
    df["description"] = df.get("Description", "").astype(str).str.strip()

    qty_cols = [
        "Under Investigation Qty",
        "Expected First Allocation Qty",
        "Expected Last Allocation Qty",
        "Allocation Qty",
    ]
    for col in qty_cols:
        if col in df.columns:
            df[col] = safe_numeric(df[col])

    df["backorder_qty"] = 0
    for col in qty_cols:
        if col in df.columns:
            df["backorder_qty"] += df[col]

    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)
    return df.groupby(["part_no", "description", "brand"], as_index=False)["backorder_qty"].sum()


def load_monthly_order(path) -> pd.DataFrame:
    df = pd.read_excel(path)
    df["part_no"] = df["PART NO"].map(normalize_part)
    df["monthly_order_qty"] = safe_numeric(df["PCS"])
    df["brand"] = df["part_no"].map(lambda x: detect_brand(x, ""))
    return df.groupby(["part_no", "brand"], as_index=False)["monthly_order_qty"].sum()


def merge_all(sales: pd.DataFrame, stock: pd.DataFrame, bo: pd.DataFrame, order: pd.DataFrame) -> pd.DataFrame:
    base = sales[
        [
            "part_no", "description", "brand", "sales_units", "sales_uyu", "cost_uyu",
            "avg_monthly_units", "avg_annual_units", "avg_monthly_sales_uyu"
        ]
    ].copy()

    merged = (
        base.merge(stock[["part_no", "stock"]], on="part_no", how="outer")
            .merge(bo[["part_no", "backorder_qty"]], on="part_no", how="left")
            .merge(order[["part_no", "monthly_order_qty"]], on="part_no", how="left")
    )

    merged["description"] = merged["description"].fillna("")
    merged["brand"] = merged.apply(lambda r: r["brand"] if pd.notna(r["brand"]) else detect_brand(r["part_no"], r["description"]), axis=1)
    for col in ["sales_units", "sales_uyu", "cost_uyu", "avg_monthly_units", "avg_annual_units", "avg_monthly_sales_uyu", "stock", "backorder_qty", "monthly_order_qty"]:
        merged[col] = merged[col].fillna(0)

    merged["pipeline_qty"] = merged["backorder_qty"] + merged["monthly_order_qty"]
    merged["available_plus_pipeline"] = merged["stock"] + merged["pipeline_qty"]
    merged["unit_margin_uyu"] = (merged["sales_uyu"] - merged["cost_uyu"]) / merged["sales_units"].replace(0, pd.NA)
    merged["unit_margin_uyu"] = merged["unit_margin_uyu"].fillna(0)
    return merged


def apply_abc(df: pd.DataFrame, basis: str = "sales_uyu") -> pd.DataFrame:
    out = df.copy()
    out = out.sort_values(basis, ascending=False).reset_index(drop=True)
    total = out[basis].sum()
    if total <= 0:
        out["abc"] = "C"
        out["abc_cum_pct"] = 0.0
        return out

    out["abc_cum_pct"] = out[basis].cumsum() / total

    def classify(x):
        if x <= 0.80:
            return "A"
        if x <= 0.95:
            return "B"
        return "C"

    out["abc"] = out["abc_cum_pct"].apply(classify)
    return out


def add_logic(df: pd.DataFrame, target_months: int, lead_time_months: int) -> pd.DataFrame:
    out = df.copy()

    out["months_of_stock"] = out["stock"] / out["avg_monthly_units"].replace(0, pd.NA)
    out["months_of_stock"] = out["months_of_stock"].fillna(999)

    out["years_of_stock"] = out["months_of_stock"] / 12.0
    out["target_stock_qty"] = (out["avg_monthly_units"] * target_months).apply(math.ceil)
    out["lead_time_need_qty"] = (out["avg_monthly_units"] * lead_time_months).apply(math.ceil)

    out["suggested_monthly_order_qty"] = (out["target_stock_qty"] - out["available_plus_pipeline"]).clip(lower=0).apply(math.ceil)

    def classify_status(row):
        if row["sales_units"] <= 0 and row["stock"] > 0:
            return "Stock muerto"
        if row["avg_monthly_units"] <= 0 and row["stock"] <= 0:
            return "Sin historial"
        if row["available_plus_pipeline"] <= 0 and row["avg_monthly_units"] > 0:
            return "Crítico"
        if row["available_plus_pipeline"] < row["lead_time_need_qty"]:
            return "Comprar ya"
        if row["available_plus_pipeline"] < row["target_stock_qty"]:
            return "Comprar"
        return "OK"

    out["status"] = out.apply(classify_status, axis=1)

    out["offer_suggestion"] = ""
    mask_offer = (
        (out["years_of_stock"] >= 2.0)
        & (out["years_of_stock"] <= 2.5)
        & (out["sales_units"] > 0)
        & (out["stock"] > 0)
    )
    out.loc[mask_offer, "offer_suggestion"] = "Sugerir oferta / promoción"

    out["dead_stock_flag"] = ((out["sales_units"] <= 0) & (out["stock"] > 0))
    out["overstock_flag"] = out["years_of_stock"] > 3
    out["inactive_offer_flag"] = mask_offer

    priority_map = {"A": 1, "B": 2, "C": 3}
    urgency_map = {"Crítico": 1, "Comprar ya": 2, "Comprar": 3, "OK": 4, "Stock muerto": 5, "Sin historial": 6}
    out["priority_rank"] = out["abc"].map(priority_map).fillna(9) * 10 + out["status"].map(urgency_map).fillna(9)

    return out


def to_excel_bytes(sheets: dict) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()


st.title("Ali Inventory - Pedido mensual, ABC y stock muerto")
st.caption("Ventas 3 años + inventario + backorder + pedido mensual")

with st.sidebar:
    st.header("Parámetros")
    target_months = st.slider("Cobertura objetivo", 1, 12, 6)
    lead_time_months = st.slider("Lead time importación", 1, 12, 6)
    abc_basis = st.selectbox("ABC según", ["sales_uyu", "sales_units"], index=0)
    top_n = st.slider("Top filas", 10, 100, 30)

sales_path = DEFAULT_SALES
stock_path = DEFAULT_STOCK
bo_path = DEFAULT_BACKORDER
order_path = DEFAULT_ORDER

missing = [p for p in [sales_path, stock_path, bo_path, order_path] if not os.path.exists(p)]
if missing:
    st.warning("Faltan archivos por defecto. Subilos manualmente.")
    sales_path = st.file_uploader("Ventas 3 años", type=["xls", "xlsx"])
    stock_path = st.file_uploader("Inventario", type=["xls", "xlsx"])
    bo_path = st.file_uploader("Backorder", type=["xls", "xlsx"])
    order_path = st.file_uploader("Pedido mensual", type=["xls", "xlsx"])
    if not all([sales_path, stock_path, bo_path, order_path]):
        st.stop()

try:
    sales_df = load_sales(sales_path)
    stock_df = load_inventory(stock_path)
    bo_df = load_backorder(bo_path)
    order_df = load_monthly_order(order_path)

    merged = merge_all(sales_df, stock_df, bo_df, order_df)
    merged = apply_abc(merged, basis=abc_basis)
    final_df = add_logic(merged, target_months, lead_time_months)
except Exception as e:
    st.error(f"No pude procesar los archivos: {e}")
    st.stop()

brand_options = ["Todos"] + sorted(final_df["brand"].dropna().unique().tolist())
abc_options = ["Todos"] + sorted(final_df["abc"].dropna().unique().tolist())
status_options = ["Todos"] + sorted(final_df["status"].dropna().unique().tolist())

c1, c2, c3, c4 = st.columns(4)
selected_brand = c1.selectbox("Marca", brand_options)
selected_abc = c2.selectbox("ABC", abc_options)
selected_status = c3.selectbox("Estado", status_options)
search_text = c4.text_input("Buscar código o descripción")

view = final_df.copy()
if selected_brand != "Todos":
    view = view[view["brand"] == selected_brand]
if selected_abc != "Todos":
    view = view[view["abc"] == selected_abc]
if selected_status != "Todos":
    view = view[view["status"] == selected_status]
if search_text:
    term = search_text.strip().upper()
    view = view[
        view["part_no"].str.contains(term, na=False)
        | view["description"].str.upper().str.contains(term, na=False)
    ]

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Ítems", f"{len(view):,}")
k2.metric("Pedido mensual sugerido", f"{int(view['suggested_monthly_order_qty'].sum()):,}")
k3.metric("Stock muerto", f"{int(view['dead_stock_flag'].sum()):,}")
k4.metric("Ofertas sugeridas", f"{int(view['inactive_offer_flag'].sum()):,}")
k5.metric("Ítems ABC A", f"{int((view['abc'] == 'A').sum()):,}")

st.subheader("Resumen ABC")
abc_summary = (
    view.groupby("abc", as_index=False)
    .agg(
        items=("part_no", "count"),
        ventas_3y=("sales_units", "sum"),
        ventas_uyu=("sales_uyu", "sum"),
        stock=("stock", "sum"),
        sugerido=("suggested_monthly_order_qty", "sum"),
    )
    .sort_values("abc")
)
st.dataframe(abc_summary, use_container_width=True)

st.subheader("Pedido mensual recomendado")
monthly_order = view[
    (view["suggested_monthly_order_qty"] > 0) & (~view["dead_stock_flag"])
].copy()
monthly_order = monthly_order.sort_values(
    ["priority_rank", "suggested_monthly_order_qty", "sales_units"],
    ascending=[True, False, False],
)
monthly_cols = [
    "part_no", "description", "brand", "abc", "status", "sales_units", "avg_monthly_units",
    "stock", "backorder_qty", "monthly_order_qty", "available_plus_pipeline",
    "target_stock_qty", "suggested_monthly_order_qty", "years_of_stock"
]
st.dataframe(monthly_order[monthly_cols], use_container_width=True, height=450)

st.subheader("Mercadería en stock clasificada ABC")
stock_abc = view[view["stock"] > 0].copy().sort_values(["abc", "sales_uyu"], ascending=[True, False])
stock_cols = ["part_no", "description", "brand", "abc", "stock", "sales_units", "sales_uyu", "years_of_stock", "status"]
st.dataframe(stock_abc[stock_cols], use_container_width=True, height=450)

st.subheader("Stock muerto")
dead_stock = view[view["dead_stock_flag"]].copy().sort_values("stock", ascending=False)
dead_cols = ["part_no", "description", "brand", "stock", "sales_units", "sales_uyu", "abc", "status"]
st.dataframe(dead_stock[dead_cols], use_container_width=True, height=300)

st.subheader("Sugerencia de ofertas (2 a 2,5 años de stock)")
offer_df = view[view["inactive_offer_flag"]].copy().sort_values(["years_of_stock", "stock"], ascending=[False, False])
offer_cols = ["part_no", "description", "brand", "abc", "stock", "sales_units", "avg_monthly_units", "years_of_stock", "offer_suggestion"]
st.dataframe(offer_df[offer_cols], use_container_width=True, height=300)

st.subheader("Gráfico de categorías ABC")
fig1, ax1 = plt.subplots(figsize=(8, 4))
abc_counts = view["abc"].value_counts().reindex(["A", "B", "C"]).fillna(0)
ax1.bar(abc_counts.index, abc_counts.values)
ax1.set_xlabel("Categoría")
ax1.set_ylabel("Cantidad de ítems")
ax1.set_title("Distribución ABC")
fig1.tight_layout()
st.pyplot(fig1)

st.subheader("Top ventas")
top_sales = view.sort_values("sales_uyu", ascending=False).head(top_n)
fig2, ax2 = plt.subplots(figsize=(10, 5))
ax2.bar(top_sales["part_no"].head(15), top_sales["sales_uyu"].head(15))
ax2.set_xlabel("Código")
ax2.set_ylabel("Ventas UYU 3 años")
ax2.set_title("Top 15 por ventas")
ax2.tick_params(axis="x", rotation=60)
fig2.tight_layout()
st.pyplot(fig2)

excel_bytes = to_excel_bytes(
    {
        "pedido_mensual": monthly_order[monthly_cols],
        "stock_abc": stock_abc[stock_cols],
        "stock_muerto": dead_stock[dead_cols],
        "ofertas": offer_df[offer_cols],
        "resumen_abc": abc_summary,
        "detalle_completo": view,
    }
)
st.download_button(
    "Descargar análisis completo",
    data=excel_bytes,
    file_name="ali_inventory_abc_pedido_mensual.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

st.success("Listo. Ahora tenés pedido mensual sugerido, clasificación ABC, stock muerto y sugerencias de ofertas.")
