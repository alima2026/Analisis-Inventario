# ===============================
# ALI INVENTORY JIT - VERSION PRO
# ===============================

import math
import re
from io import BytesIO

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Ali Inventory JIT", layout="wide")

# =========================================================
# UTILIDADES
# =========================================================
def normalize_part(value):
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", "", str(value).upper().strip())


def safe_numeric(series):
    return pd.to_numeric(
        series.astype(str)
        .str.replace("$", "")
        .str.replace(",", "")
        .str.replace(" ", ""),
        errors="coerce"
    ).fillna(0)


def detect_brand(part_no, desc=""):
    p = normalize_part(part_no)
    d = str(desc).upper()

    if re.match(r"[A-Z0-9]{4}-", p):
        return "Mazda"
    if re.match(r"\d{5}[A-Z]\d{3}", p):
        return "Kia"
    if "." in p:
        return "Multimarca"

    if "MAZDA" in d:
        return "Mazda"
    if "KIA" in d:
        return "Kia"

    return "Multimarca"


# =========================================================
# LECTURA ARCHIVOS
# =========================================================
def load_sales(file):
    df = pd.read_excel(file)

    df["part_no"] = df.iloc[:, 0].map(normalize_part)
    df["description"] = df.iloc[:, 1]
    df["sales_units"] = safe_numeric(df.iloc[:, 3])
    df["sales_uyu"] = safe_numeric(df.iloc[:, 7])
    df["cost_uyu"] = safe_numeric(df.iloc[:, 11])

    df["brand"] = df.apply(lambda r: detect_brand(r["part_no"], r["description"]), axis=1)

    df["avg_monthly_units"] = df["sales_units"] / 36

    return df


def load_inventory(file):
    df = pd.read_excel(file)

    df["part_no"] = df.iloc[:, 0].map(normalize_part)
    df["stock"] = safe_numeric(df.iloc[:, -1])

    return df.groupby("part_no", as_index=False)["stock"].sum()


def load_backorder(file):
    df = pd.read_excel(file)

    df["part_no"] = df.iloc[:, 0].map(normalize_part)
    df["backorder_qty"] = safe_numeric(df.iloc[:, -1])

    return df.groupby("part_no", as_index=False)["backorder_qty"].sum()


def load_orders(file):
    df = pd.read_excel(file)

    df["part_no"] = df.iloc[:, 0].map(normalize_part)
    df["monthly_order_qty"] = safe_numeric(df.iloc[:, 1])

    return df.groupby("part_no", as_index=False)["monthly_order_qty"].sum()


# =========================================================
# LOGICA PRINCIPAL
# =========================================================
def process_all(sales, stock, bo, orders):

    df = sales.merge(stock, on="part_no", how="left")
    df = df.merge(bo, on="part_no", how="left")
    df = df.merge(orders, on="part_no", how="left")

    df.fillna(0, inplace=True)

    df["pipeline"] = df["backorder_qty"] + df["monthly_order_qty"]
    df["available"] = df["stock"] + df["pipeline"]

    # -------------------------
    # SMART SCORE (FIX ERROR)
    # -------------------------
    df["smart_score"] = (
        df["avg_monthly_units"] * 1000
        / (df["stock"] + 1)
    )

    # -------------------------
    # ABC
    # -------------------------
    df = df.sort_values("sales_uyu", ascending=False)
    df["acum"] = df["sales_uyu"].cumsum() / df["sales_uyu"].sum()

    df["abc"] = df["acum"].apply(
        lambda x: "A" if x <= 0.8 else ("B" if x <= 0.95 else "C")
    )

    return df


def calculate_jit(df, meses_objetivo, lead_time):

    df["stock_meses"] = df["stock"] / df["avg_monthly_units"].replace(0, 1)

    df["target_stock"] = df["avg_monthly_units"] * meses_objetivo
    df["lead_stock"] = df["avg_monthly_units"] * lead_time

    df["pedido_sugerido"] = (
        df["target_stock"] - df["available"]
    ).clip(lower=0)

    df["status"] = df.apply(lambda r:
        "Crítico" if r["available"] <= 0 else
        "Comprar ya" if r["available"] < r["lead_stock"] else
        "Comprar" if r["available"] < r["target_stock"] else
        "OK"
    , axis=1)

    return df


# =========================================================
# UI
# =========================================================
st.title("🚀 Ali Inventory JIT")

empresa = st.radio("Empresa", ["Magna", "Alimatico SRL"])

col1, col2 = st.columns(2)

ventas = col1.file_uploader("Ventas")
inventario = col2.file_uploader("Inventario")

backorder = col1.file_uploader("Backorder")
pedido = col2.file_uploader("Pedidos")

if not all([ventas, inventario, backorder, pedido]):
    st.stop()

# Parámetros
meses = st.slider("Cobertura meses", 1, 12, 6)
lead = st.slider("Lead time", 1, 12, 6)

# Procesar
sales_df = load_sales(ventas)
stock_df = load_inventory(inventario)
bo_df = load_backorder(backorder)
order_df = load_orders(pedido)

df = process_all(sales_df, stock_df, bo_df, order_df)
df = calculate_jit(df, meses, lead)

# =========================================================
# RESULTADOS
# =========================================================
st.subheader("📊 KPIs")

c1, c2, c3 = st.columns(3)
c1.metric("Items", len(df))
c2.metric("Stock muerto", len(df[df["sales_units"] == 0]))
c3.metric("Compra sugerida", int(df["pedido_sugerido"].sum()))

# TOP
st.subheader("🔥 Top ventas")
st.dataframe(df.sort_values("sales_units", ascending=False).head(20))

# PEDIDO
st.subheader("📦 Pedido inteligente")

pedido_df = df[df["pedido_sugerido"] > 0]
pedido_df = pedido_df.sort_values("smart_score", ascending=False)

st.dataframe(pedido_df[[
    "part_no", "description", "brand",
    "stock", "pipeline",
    "pedido_sugerido", "status", "smart_score"
]])

# GRAFICO
fig, ax = plt.subplots()
top = df.head(15)
ax.bar(top["part_no"], top["sales_units"])
plt.xticks(rotation=60)
st.pyplot(fig)

st.success("Sistema funcionando correctamente ✅")
