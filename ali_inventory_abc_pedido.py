import math
import os
import re
from io import BytesIO

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


st.set_page_config(page_title="Ali Inventory - Pedido Inteligente", layout="wide")

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
    df["unit_cost_uyu"] = (df["cost_uyu"] / df["sales_units"].replace(0, pd.NA)).fillna(0)
    df["unit_sale_uyu"] = (df["sales_uyu"] / df["sales_units"].replace(0, pd.NA)).fillna(0)
    df["unit_margin_uyu"] = (df["unit_sale_uyu"] - df["unit_cost_uyu"]).fillna(0)
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
    base = sales[[
        "part_no", "description", "brand", "sales_units", "sales_uyu", "cost_uyu",
        "avg_monthly_units", "avg_annual_units", "avg_monthly_sales_uyu",
        "unit_cost_uyu", "unit_sale_uyu", "unit_margin_uyu"
    ]].copy()

    merged = (
        base.merge(stock[["part_no", "stock"]], on="part_no", how="outer")
            .merge(bo[["part_no", "backorder_qty"]], on="part_no", how="left")
            .merge(order[["part_no", "monthly_order_qty"]], on="part_no", how="left")
    )

    merged["description"] = merged["description"].fillna("")
    merged["brand"] = merged.apply(
        lambda r: r["brand"] if pd.notna(r["brand"]) else detect_brand(r["part_no"], r["description"]), axis=1
    )
    for col in [
        "sales_units", "sales_uyu", "cost_uyu", "avg_monthly_units", "avg_annual_units",
        "avg_monthly_sales_uyu", "unit_cost_uyu", "unit_sale_uyu", "unit_margin_uyu",
        "stock", "backorder_qty", "monthly_order_qty"
    ]:
        merged[col] = merged[col].fillna(0)

    merged["pipeline_qty"] = merged["backorder_qty"] + merged["monthly_order_qty"]
    merged["available_plus_pipeline"] = merged["stock"] + merged["pipeline_qty"]
    return merged



def apply_abc(df: pd.DataFrame, basis: str = "sales_uyu") -> pd.DataFrame:
    out = df.copy().sort_values(basis, ascending=False).reset_index(drop=True)
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
    out["suggested_monthly_order_qty"] = (
        out["target_stock_qty"] - out["available_plus_pipeline"]
    ).clip(lower=0).apply(math.ceil)

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
    out["dead_stock_flag"] = ((out["sales_units"] <= 0) & (out["stock"] > 0))
    out["offer_flag"] = (
        (out["years_of_stock"] >= 2.0)
        & (out["years_of_stock"] <= 2.5)
        & (out["sales_units"] > 0)
        & (out["stock"] > 0)
    )
    out["offer_suggestion"] = out["offer_flag"].map({True: "Sugerir oferta / promoción", False: ""})

    urgency_map = {"Crítico": 100, "Comprar ya": 80, "Comprar": 50, "OK": 15, "Stock muerto": 0, "Sin historial": 5}
    abc_map = {"A": 100, "B": 60, "C": 25}

    out["urgency_score"] = out["status"].map(urgency_map).fillna(0)
    out["abc_score"] = out["abc"].map(abc_map).fillna(0)

    max_sales = max(float(out["avg_monthly_sales_uyu"].max()), 1.0)
    max_margin = max(float(out["unit_margin_uyu"].clip(lower=0).max()), 1.0)

    out["demand_score"] = (out["avg_monthly_sales_uyu"].clip(lower=0) / max_sales) * 100
    out["margin_score"] = (out["unit_margin_uyu"].clip(lower=0) / max_margin) * 100

    out["estimated_purchase_cost"] = out["suggested_monthly_order_qty"] * out["unit_cost_uyu"]
    out["estimated_gross_profit"] = out["suggested_monthly_order_qty"] * out["unit_margin_uyu"]

    out["smart_score"] = (
        out["urgency_score"] * 0.40
        + out["abc_score"] * 0.25
        + out["demand_score"] * 0.20
        + out["margin_score"] * 0.15
    )

    out.loc[out["dead_stock_flag"], "smart_score"] = 0
    out.loc[out["suggested_monthly_order_qty"] <= 0, "smart_score"] = 0

    out["score_per_cost"] = out["smart_score"] / out["unit_cost_uyu"].replace(0, pd.NA)
    out["score_per_cost"] = out["score_per_cost"].fillna(out["smart_score"])

    return out



def build_intelligent_order(df: pd.DataFrame, budget_uyu: float, allow_partial: bool = True) -> pd.DataFrame:
    candidates = df[
        (df["suggested_monthly_order_qty"] > 0)
        & (~df["dead_stock_flag"])
        & (df["unit_cost_uyu"] > 0)
        & (df["smart_score"] > 0)
    ].copy()

    candidates = candidates.sort_values(
        ["status", "abc", "score_per_cost", "smart_score", "avg_monthly_sales_uyu"],
        ascending=[True, True, False, False, False],
        key=lambda col: col.map({"Crítico": 0, "Comprar ya": 1, "Comprar": 2, "OK": 3}).fillna(col) if col.name == "status" else col
    )

    remaining = float(budget_uyu)
    chosen_rows = []

    for _, row in candidates.iterrows():
        row = row.copy()
        full_qty = int(max(row["suggested_monthly_order_qty"], 0))
        unit_cost = float(max(row["unit_cost_uyu"], 0))
        if full_qty <= 0 or unit_cost <= 0:
            continue

        max_affordable_qty = int(remaining // unit_cost) if unit_cost > 0 else 0
        buy_qty = 0

        if remaining >= full_qty * unit_cost:
            buy_qty = full_qty
        elif allow_partial and max_affordable_qty > 0:
            buy_qty = min(full_qty, max_affordable_qty)

        if buy_qty <= 0:
            continue

        row["recommended_buy_qty"] = int(buy_qty)
        row["recommended_buy_cost"] = float(buy_qty * unit_cost)
        row["recommended_buy_profit"] = float(buy_qty * row["unit_margin_uyu"])
        row["remaining_budget_after"] = float(remaining - row["recommended_buy_cost"])
        chosen_rows.append(row)
        remaining -= row["recommended_buy_cost"]

        if remaining <= 0:
            break

    if not chosen_rows:
        return pd.DataFrame(columns=list(candidates.columns) + [
            "recommended_buy_qty", "recommended_buy_cost", "recommended_buy_profit", "remaining_budget_after"
        ])

    return pd.DataFrame(chosen_rows)



def to_excel_bytes(sheets: dict) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    return output.getvalue()


st.title("Ali Inventory - Pedido inteligente")
st.caption("Prioriza qué comprar primero según rotación, ABC, urgencia, margen y capital disponible")

with st.sidebar:
    st.header("Parámetros")
    target_months = st.slider("Cobertura objetivo", 1, 12, 6)
    lead_time_months = st.slider("Lead time importación", 1, 12, 6)
    abc_basis = st.selectbox("ABC según", ["sales_uyu", "sales_units"], index=0)
    budget_uyu = st.number_input("Capital disponible para comprar (UYU)", min_value=0.0, value=500000.0, step=50000.0)
    allow_partial = st.checkbox("Permitir compra parcial si no alcanza el capital", value=True)
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
    smart_order = build_intelligent_order(final_df, budget_uyu=budget_uyu, allow_partial=allow_partial)
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

smart_view = smart_order.copy()
if selected_brand != "Todos":
    smart_view = smart_view[smart_view["brand"] == selected_brand]
if selected_abc != "Todos":
    smart_view = smart_view[smart_view["abc"] == selected_abc]
if selected_status != "Todos":
    smart_view = smart_view[smart_view["status"] == selected_status]
if search_text:
    term = search_text.strip().upper()
    smart_view = smart_view[
        smart_view["part_no"].str.contains(term, na=False)
        | smart_view["description"].str.upper().str.contains(term, na=False)
    ]

capital_used = float(smart_order["recommended_buy_cost"].sum()) if not smart_order.empty else 0.0
capital_left = float(budget_uyu - capital_used)
estimated_profit = float(smart_order["recommended_buy_profit"].sum()) if not smart_order.empty else 0.0

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Capital disponible", f"{budget_uyu:,.0f}")
k2.metric("Capital asignado", f"{capital_used:,.0f}")
k3.metric("Capital restante", f"{capital_left:,.0f}")
k4.metric("Ganancia bruta estimada", f"{estimated_profit:,.0f}")
k5.metric("Ítems elegidos", f"{len(smart_order):,}")

st.subheader("Pedido inteligente recomendado")
smart_cols = [
    "part_no", "description", "brand", "abc", "status", "smart_score", "score_per_cost",
    "sales_units", "avg_monthly_units", "stock", "backorder_qty", "monthly_order_qty",
    "suggested_monthly_order_qty", "recommended_buy_qty", "unit_cost_uyu", "unit_margin_uyu",
    "recommended_buy_cost", "recommended_buy_profit", "remaining_budget_after"
]
if smart_view.empty:
    st.info("Con el capital actual no se pudo armar una compra sugerida. Probá subir el presupuesto o permitir compra parcial.")
else:
    st.dataframe(
        smart_view[smart_cols].sort_values(["smart_score", "recommended_buy_cost"], ascending=[False, False]),
        use_container_width=True,
        height=450,
    )

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

st.subheader("Mercadería en stock clasificada ABC")
stock_abc = view[view["stock"] > 0].copy().sort_values(["abc", "sales_uyu"], ascending=[True, False])
stock_cols = ["part_no", "description", "brand", "abc", "stock", "sales_units", "sales_uyu", "years_of_stock", "status"]
st.dataframe(stock_abc[stock_cols], use_container_width=True, height=350)

st.subheader("Stock muerto")
dead_stock = view[view["dead_stock_flag"]].copy().sort_values("stock", ascending=False)
dead_cols = ["part_no", "description", "brand", "stock", "sales_units", "sales_uyu", "abc", "status"]
st.dataframe(dead_stock[dead_cols], use_container_width=True, height=250)

st.subheader("Sugerencia de ofertas (2 a 2,5 años de stock)")
offer_df = view[view["offer_flag"]].copy().sort_values(["years_of_stock", "stock"], ascending=[False, False])
offer_cols = ["part_no", "description", "brand", "abc", "stock", "sales_units", "avg_monthly_units", "years_of_stock", "offer_suggestion"]
st.dataframe(offer_df[offer_cols], use_container_width=True, height=250)

st.subheader("Top compra inteligente")
fig1, ax1 = plt.subplots(figsize=(10, 5))
plot_df = smart_order.sort_values("recommended_buy_cost", ascending=False).head(15)
if not plot_df.empty:
    ax1.bar(plot_df["part_no"], plot_df["recommended_buy_cost"])
    ax1.set_xlabel("Código")
    ax1.set_ylabel("Costo de compra sugerido (UYU)")
    ax1.set_title("Top 15 compra sugerida por costo")
    ax1.tick_params(axis="x", rotation=60)
    fig1.tight_layout()
    st.pyplot(fig1)
else:
    st.info("No hay datos para graficar en la compra inteligente.")

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

export_detail_cols = [
    "part_no", "description", "brand", "abc", "status", "sales_units", "sales_uyu", "avg_monthly_units",
    "stock", "backorder_qty", "monthly_order_qty", "pipeline_qty", "available_plus_pipeline",
    "target_stock_qty", "suggested_monthly_order_qty", "unit_cost_uyu", "unit_margin_uyu",
    "estimated_purchase_cost", "estimated_gross_profit", "smart_score", "score_per_cost",
    "years_of_stock", "offer_suggestion"
]

excel_bytes = to_excel_bytes(
    {
        "pedido_inteligente": smart_order[smart_cols] if not smart_order.empty else pd.DataFrame(columns=smart_cols),
        "resumen_abc": abc_summary,
        "stock_abc": stock_abc[stock_cols],
        "stock_muerto": dead_stock[dead_cols],
        "ofertas": offer_df[offer_cols],
        "detalle_completo": view[export_detail_cols],
    }
)
st.download_button(
    "Descargar análisis completo",
    data=excel_bytes,
    file_name="ali_inventory_pedido_inteligente.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

st.success("Listo. Ahora tenés compra inteligente con capital limitado, ABC, stock muerto y ofertas sugeridas.")
