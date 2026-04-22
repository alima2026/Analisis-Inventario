import hashlib
import math
import re
import sqlite3
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:
    plt = None


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "pedidos_v1.db"

DEFAULT_TARGET_MONTHS = 6
DEFAULT_LEAD_TIME_MONTHS = 6
DEFAULT_CAPITAL = 500000.0
DEFAULT_COMPANY = "Magna"
AUTO_ORDER_FOLDER = APP_DIR / "Pedidos Solicitados"
EDITABLE_ORDER_SOURCE_TYPE = "pedido_editable_mazda"
FINAL_MAZDA_ORDER_SOURCE_TYPE = "pedido_final_mazda"
ORDER_DRAFT_STATUS = "BORRADOR"
ORDER_CONFIRMED_STATUS = "ABIERTO"
LOCKED_ORDER_STATUSES = {"ABIERTO", "PARCIAL", "RECIBIDO_INFERIDO", "VACIO"}
ORDER_EDITOR_COLUMNS = ["PART NO", "PCS", "DESCRIPCION", "MARCA"]


# =========================================================
# Utilidades
# =========================================================
def normalize_part(value) -> str:
    return parse_part_code(value)["display"]


def _clean_part_text(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip().upper()
    text = re.sub(r"\s+", "", text)
    return text


def _revision_rank(revision: str) -> int:
    if isinstance(revision, str) and re.fullmatch(r"[A-Z]", revision):
        return ord(revision) - ord("A") + 1
    return 0


def parse_part_code(value, allow_mazda_compact: bool = False) -> dict:
    raw_text = _clean_part_text(value)
    if not raw_text:
        return {
            "raw": "",
            "display": "",
            "key": "",
            "revision": "",
            "is_mazda": False,
            "is_plaza": False,
            "formatted": False,
        }

    text = raw_text
    is_plaza = text.endswith("*")
    if is_plaza:
        text = text[:-1]

    explicit_revision = ""
    revision_match = re.search(r"\(([A-Z])\)$", text)
    if revision_match:
        explicit_revision = revision_match.group(1)
        text = text[: revision_match.start()]

    text = text.strip("-")
    hyphen_match = re.fullmatch(r"([A-Z0-9]{4})-([A-Z0-9]{2})-([A-Z0-9]{3,5})", text)
    if hyphen_match:
        group_1, group_2, group_3 = hyphen_match.groups()
        revision = explicit_revision
        core_tail = group_3
        if not revision and len(core_tail) >= 4 and core_tail[-1].isalpha():
            revision = core_tail[-1]
            core_tail = core_tail[:-1]

        if len(core_tail) in (3, 4):
            code_key = f"{group_1}-{group_2}-{core_tail}"
            display = f"{code_key}{revision}{'*' if is_plaza else ''}"
            return {
                "raw": raw_text,
                "display": display,
                "key": code_key,
                "revision": revision,
                "is_mazda": True,
                "is_plaza": is_plaza,
                "formatted": raw_text != display,
            }

    compact_text = text.replace("-", "")
    if explicit_revision or allow_mazda_compact:
        compact_match = re.fullmatch(r"[A-Z0-9]{9,11}", compact_text)
        if compact_match:
            revision = explicit_revision
            core_compact = compact_text
            if not revision and len(core_compact) in (10, 11) and core_compact[-1].isalpha():
                revision = core_compact[-1]
                core_compact = core_compact[:-1]

            if len(core_compact) in (9, 10):
                code_key = f"{core_compact[:4]}-{core_compact[4:6]}-{core_compact[6:]}"
                display = f"{code_key}{revision}{'*' if is_plaza else ''}"
                return {
                    "raw": raw_text,
                    "display": display,
                    "key": code_key,
                    "revision": revision,
                    "is_mazda": True,
                    "is_plaza": is_plaza,
                    "formatted": raw_text != display,
                }

    return {
        "raw": raw_text,
        "display": raw_text,
        "key": raw_text,
        "revision": "",
        "is_mazda": False,
        "is_plaza": is_plaza,
        "formatted": False,
    }


def normalize_part_key(value, allow_mazda_compact: bool = False) -> str:
    return parse_part_code(value, allow_mazda_compact=allow_mazda_compact)["key"]


def normalize_part_display(value, allow_mazda_compact: bool = False) -> str:
    return parse_part_code(value, allow_mazda_compact=allow_mazda_compact)["display"]


def choose_latest_part_code(values, allow_mazda_compact: bool = False) -> str:
    infos = [
        parse_part_code(value, allow_mazda_compact=allow_mazda_compact)
        for value in values
        if _clean_part_text(value)
    ]
    if not infos:
        return ""

    infos = sorted(
        infos,
        key=lambda item: (
            1 if item["is_mazda"] else 0,
            _revision_rank(item["revision"]),
            item["display"],
        ),
    )
    return infos[-1]["display"]


def first_non_empty(values) -> str:
    for value in values:
        if pd.notna(value) and str(value).strip():
            return str(value).strip()
    return ""


def add_part_identity(df: pd.DataFrame, source_col: str, allow_mazda_compact: bool = False) -> pd.DataFrame:
    out = df.copy()
    parsed = out[source_col].map(lambda value: parse_part_code(value, allow_mazda_compact=allow_mazda_compact))
    out["part_no"] = parsed.map(lambda item: item["display"])
    out["part_key"] = parsed.map(lambda item: item["key"])
    out["_part_raw_clean"] = parsed.map(lambda item: item["raw"])
    out["_part_revision"] = parsed.map(lambda item: item["revision"])
    out["_part_formatted"] = parsed.map(lambda item: item["formatted"])
    return out


def ensure_part_identity_columns(df: pd.DataFrame, allow_mazda_compact: bool = False) -> pd.DataFrame:
    out = df.copy()
    if "part_no" not in out.columns:
        out["part_no"] = ""
    has_part_key = "part_key" in out.columns
    if not has_part_key:
        out["part_key"] = out["part_no"].map(lambda value: normalize_part_key(value, allow_mazda_compact=allow_mazda_compact))
        out["part_no"] = out["part_no"].map(lambda value: normalize_part_display(value, allow_mazda_compact=allow_mazda_compact))
    else:
        out["part_key"] = out["part_key"].fillna("").astype(str).str.strip()
        missing_key = out["part_key"].eq("")
        if missing_key.any():
            out.loc[missing_key, "part_key"] = out.loc[missing_key, "part_no"].map(
                lambda value: normalize_part_key(value, allow_mazda_compact=allow_mazda_compact)
            )
            out.loc[missing_key, "part_no"] = out.loc[missing_key, "part_no"].map(
                lambda value: normalize_part_display(value, allow_mazda_compact=allow_mazda_compact)
            )
        out["part_no"] = out["part_no"].fillna("").astype(str).str.strip()
    return out


def _format_qty(value) -> str:
    qty = float(value or 0)
    if qty.is_integer():
        return str(int(qty))
    return f"{qty:g}"


def build_code_unification_report(
    df: pd.DataFrame,
    source_label: str,
    qty_col: str,
    allow_mazda_compact: bool = True,
) -> pd.DataFrame:
    columns = ["origen", "codigo_base", "codigo_unificado", "variantes_detectadas", "cantidad_total"]
    if df.empty or "part_key" not in df.columns or qty_col not in df.columns:
        return pd.DataFrame(columns=columns)

    rows = []
    tmp = df[df["part_key"].astype(str).str.strip() != ""].copy()
    for part_key, group in tmp.groupby("part_key", dropna=False):
        display_qty = (
            group.groupby("part_no", dropna=False)[qty_col]
            .sum()
            .reset_index()
            .sort_values("part_no")
        )
        displays = [value for value in display_qty["part_no"].astype(str).tolist() if value]
        if len(set(displays)) <= 1:
            continue

        unified_code = choose_latest_part_code(displays, allow_mazda_compact=allow_mazda_compact)
        detail = ", ".join(
            f"{row['part_no']}={_format_qty(row[qty_col])}"
            for _, row in display_qty.iterrows()
            if str(row["part_no"]).strip()
        )
        rows.append(
            {
                "origen": source_label,
                "codigo_base": part_key,
                "codigo_unificado": unified_code,
                "variantes_detectadas": detail,
                "cantidad_total": float(display_qty[qty_col].sum()),
            }
        )

    return pd.DataFrame(rows, columns=columns)


def collect_code_unification_reports(*frames: pd.DataFrame) -> pd.DataFrame:
    reports = []
    for frame in frames:
        report = frame.attrs.get("code_unifications") if isinstance(frame, pd.DataFrame) else None
        if isinstance(report, pd.DataFrame) and not report.empty:
            reports.append(report)

    if not reports:
        return pd.DataFrame(
            columns=["origen", "codigo_base", "codigo_unificado", "variantes_detectadas", "cantidad_total"]
        )
    return pd.concat(reports, ignore_index=True)


def count_formatted_codes(*frames: pd.DataFrame) -> int:
    total = 0
    for frame in frames:
        if isinstance(frame, pd.DataFrame):
            total += int(frame.attrs.get("formatted_code_count", 0) or 0)
    return total


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

    if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{2}-[A-Z0-9]{3,4}[A-Z]?", part):
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


def normalize_order_number(value) -> str:
    return re.sub(r"\s+", "", safe_text(value).upper())


def classify_order_number(order_number: str) -> dict:
    code = normalize_order_number(order_number)
    if re.fullmatch(r"HC1[A-Z0-9]", code):
        return {
            "order_code": code,
            "transport_type": "AEREO",
            "lead_time_days": 30,
            "label": "Aereo - demora estimada 30 dias",
        }
    if re.fullmatch(r"HC[A-Z][A-Z0-9]", code):
        return {
            "order_code": code,
            "transport_type": "MARITIMO",
            "lead_time_days": 180,
            "label": "Maritimo - demora estimada 6 meses",
        }
    return {
        "order_code": code,
        "transport_type": "SIN_CLASIFICAR",
        "lead_time_days": 0,
        "label": "Numero no reconocido: usa formato HCCA/HCJV para maritimo o HC1A/HC1D para aereo",
    }


def estimate_order_eta(created_at: str, lead_time_days: int) -> str:
    if lead_time_days <= 0:
        return ""
    created_ts = pd.to_datetime(created_at, errors="coerce")
    if pd.isna(created_ts):
        created_ts = pd.Timestamp.now()
    return (created_ts + pd.Timedelta(days=int(lead_time_days))).date().isoformat()


def empty_order_editor_df() -> pd.DataFrame:
    return pd.DataFrame(columns=ORDER_EDITOR_COLUMNS)


def normalize_order_items_df(items_df: pd.DataFrame) -> pd.DataFrame:
    columns = ["part_key", "part_no", "description", "brand", "quantity"]
    if items_df is None or items_df.empty:
        return pd.DataFrame(columns=columns)

    source = items_df.copy()
    part_col = "PART NO" if "PART NO" in source.columns else "part_no"
    qty_col = "PCS" if "PCS" in source.columns else "quantity"
    desc_col = "DESCRIPCION" if "DESCRIPCION" in source.columns else "description"
    brand_col = "MARCA" if "MARCA" in source.columns else "brand"

    if part_col not in source.columns:
        source[part_col] = ""
    if qty_col not in source.columns:
        source[qty_col] = 0
    if desc_col not in source.columns:
        source[desc_col] = ""
    if brand_col not in source.columns:
        source[brand_col] = ""

    source = add_part_identity(source, part_col, allow_mazda_compact=True)
    source["quantity"] = pd.to_numeric(source[qty_col], errors="coerce").fillna(0.0)
    source["description"] = source[desc_col].fillna("").astype(str).str.strip()
    source["brand"] = source[brand_col].fillna("").astype(str).str.strip()
    source = source[(source["part_key"] != "") & (source["quantity"] > 0)].copy()
    if source.empty:
        return pd.DataFrame(columns=columns)

    grouped = (
        source.groupby("part_key", as_index=False)
        .agg(
            part_no=("part_no", lambda values: choose_latest_part_code(values, allow_mazda_compact=True)),
            description=("description", first_non_empty),
            brand=("brand", first_non_empty),
            quantity=("quantity", "sum"),
        )
        .sort_values("part_no")
        .reset_index(drop=True)
    )
    grouped["brand"] = grouped.apply(
        lambda row: row["brand"] if safe_text(row["brand"]) else detect_brand(row["part_no"], row["description"]),
        axis=1,
    )
    return grouped[columns]


def order_items_to_editor_df(items_df: pd.DataFrame) -> pd.DataFrame:
    normalized = normalize_order_items_df(items_df)
    if normalized.empty:
        return empty_order_editor_df()

    out = pd.DataFrame(
        {
            "PART NO": normalized["part_no"],
            "PCS": normalized["quantity"].apply(lambda qty: int(qty) if float(qty).is_integer() else float(qty)),
            "DESCRIPCION": normalized["description"],
            "MARCA": normalized["brand"],
        }
    )
    return out[ORDER_EDITOR_COLUMNS]


def format_order_for_factory_download(items_df: pd.DataFrame, order_code: str = "") -> pd.DataFrame:
    normalized = normalize_order_items_df(items_df)
    if normalized.empty:
        return pd.DataFrame(columns=["ORDER NO", "LINE NO", "PART NO", "PCS"])

    export_df = pd.DataFrame(
        {
            "ORDER NO": safe_text(order_code),
            "LINE NO": range(1, len(normalized) + 1),
            "PART NO": normalized["part_no"].astype(str),
            "PCS": normalized["quantity"].apply(lambda qty: int(math.ceil(float(qty)))),
        }
    )
    return export_df


def build_editable_order_from_intelligent(pedido_inteligente: pd.DataFrame) -> pd.DataFrame:
    if pedido_inteligente.empty:
        return empty_order_editor_df()

    order_df = pedido_inteligente.copy()
    mazda_mask = order_df["brand"].astype(str).str.upper().str.contains("MAZDA", na=False)
    if mazda_mask.any():
        order_df = order_df[mazda_mask].copy()

    order_df["PCS"] = pd.to_numeric(order_df["intelligent_buy_qty"], errors="coerce").fillna(0).apply(math.ceil)
    order_df = order_df[order_df["PCS"] > 0].copy()
    if order_df.empty:
        return empty_order_editor_df()

    order_df = order_df.sort_values(["smart_score", "sales_units"], ascending=[False, False]).reset_index(drop=True)
    return order_items_to_editor_df(
        pd.DataFrame(
            {
                "PART NO": order_df["part_no"].astype(str),
                "PCS": order_df["PCS"].astype(int),
                "DESCRIPCION": order_df["description"].astype(str),
                "MARCA": order_df["brand"].astype(str),
            }
        )
    )


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

    expected_markers = {"PRODUCTO", "UNIDADES", "VENTAS", "COSTO"}
    header_text = " ".join(
        str(part).strip().upper()
        for col in df.columns
        for part in (col if isinstance(col, tuple) else [col])
    )
    if not all(marker in header_text for marker in expected_markers):
        file_name = getattr(uploaded_file, "name", "archivo cargado")
        raise ValueError(
            f"El archivo cargado como Ventas 3 anios no parece ser el archivo de ventas. "
            f"Revisa que no hayas cargado Inventario, Backorder o Pedido a fabrica en ese casillero. Archivo: {file_name}"
        )

    sales_columns = [
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
    if df.shape[1] < len(sales_columns):
        raise ValueError("El archivo de ventas no tiene el formato esperado.")

    df = df.iloc[:, : len(sales_columns)].copy()
    df.columns = sales_columns

    df = df.copy()
    df = add_part_identity(df, "part_no", allow_mazda_compact=False)
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

    df = df[df["part_key"] != ""].copy()
    if df.empty:
        file_name = getattr(uploaded_file, "name", "archivo cargado")
        raise ValueError(f"No se encontraron codigos validos en el archivo de ventas: {file_name}")

    df["brand"] = df.apply(lambda row: detect_brand(row["part_no"], row["description"]), axis=1)
    df = (
        df.groupby("part_key", as_index=False)
        .agg(
            part_no=("part_no", lambda values: choose_latest_part_code(values)),
            description=("description", first_non_empty),
            brand=("brand", first_non_empty),
            sales_units=("sales_units", "sum"),
            sales_uyu=("sales_uyu", "sum"),
            cost_uyu=("cost_uyu", "sum"),
        )
    )
    df["avg_monthly_units"] = df["sales_units"] / 36.0
    df["avg_annual_units"] = df["sales_units"] / 3.0
    df["avg_monthly_sales_uyu"] = df["sales_uyu"] / 36.0
    return df


def load_inventory(uploaded_file) -> pd.DataFrame:
    raw = pd.read_excel(uploaded_file, header=None)
    df = raw.iloc[5:, [2, 8, 16, 20]].copy()
    df.columns = ["part_no", "description", "unit", "stock"]
    df = df.dropna(subset=["part_no"]).copy()
    df = add_part_identity(df, "part_no", allow_mazda_compact=False)
    df["description"] = df["description"].astype(str).str.strip()
    df["stock"] = safe_numeric(df["stock"])
    df = df[df["part_key"] != ""].copy()
    df["brand"] = df.apply(lambda row: detect_brand(row["part_no"], row["description"]), axis=1)
    return (
        df.groupby("part_key", as_index=False)
        .agg(
            part_no=("part_no", lambda values: choose_latest_part_code(values)),
            description=("description", first_non_empty),
            brand=("brand", first_non_empty),
            stock=("stock", "sum"),
        )
    )


def load_backorder(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file)
    df = df.copy()

    part_col = "Buyer Part" if "Buyer Part" in df.columns else "Seller Part"
    desc_col = "Description" if "Description" in df.columns else None

    df = add_part_identity(df, part_col, allow_mazda_compact=True)
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

    df = df[df["part_key"] != ""].copy()
    df["brand"] = df.apply(lambda row: detect_brand(row["part_no"], row["description"]), axis=1)
    report = build_code_unification_report(df, "Backorder", "backorder_qty", allow_mazda_compact=True)
    formatted_count = int(df["_part_formatted"].sum())
    out = (
        df.groupby("part_key", as_index=False)
        .agg(
            part_no=("part_no", lambda values: choose_latest_part_code(values, allow_mazda_compact=True)),
            description=("description", first_non_empty),
            brand=("brand", first_non_empty),
            backorder_qty=("backorder_qty", "sum"),
        )
    )
    out.attrs["code_unifications"] = report
    out.attrs["formatted_code_count"] = formatted_count
    return out


def load_monthly_order(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file)
    df = df.copy()

    part_col = "PART NO" if "PART NO" in df.columns else df.columns[0]
    qty_col = "PCS" if "PCS" in df.columns else df.columns[1]
    order_no_col = "ORDER NO" if "ORDER NO" in df.columns else None

    df = add_part_identity(df, part_col, allow_mazda_compact=True)
    df["monthly_order_qty"] = safe_numeric(df[qty_col])
    df = df[df["part_key"] != ""].copy()
    report = build_code_unification_report(df, "Pedido a fabrica", "monthly_order_qty", allow_mazda_compact=True)
    formatted_count = int(df["_part_formatted"].sum())
    order_summary = (
        df.groupby("part_key", as_index=False)
        .agg(
            part_no=("part_no", lambda values: choose_latest_part_code(values, allow_mazda_compact=True)),
            monthly_order_qty=("monthly_order_qty", "sum"),
        )
    )

    order_code = ""
    if order_no_col and order_no_col in df.columns:
        order_values = [safe_text(value) for value in df[order_no_col].dropna().tolist() if safe_text(value)]
        if order_values:
            order_code = order_values[0]

    order_summary.attrs["order_code"] = order_code if order_code else Path(uploaded_file.name).stem
    order_summary.attrs["source_file_name"] = uploaded_file.name
    order_summary.attrs["code_unifications"] = report
    order_summary.attrs["formatted_code_count"] = formatted_count
    return order_summary


def load_final_mazda_order_file(uploaded_file) -> pd.DataFrame:
    df = pd.read_excel(uploaded_file)
    if df.empty:
        return empty_order_editor_df()

    df = df.copy()
    column_lookup = {str(col).strip().upper(): col for col in df.columns}
    part_col = column_lookup.get("PART NO") or column_lookup.get("PART_NO") or column_lookup.get("PARTNO")
    qty_col = column_lookup.get("PCS") or column_lookup.get("QTY") or column_lookup.get("CANTIDAD")
    desc_col = column_lookup.get("DESCRIPCION") or column_lookup.get("DESCRIPTION")
    brand_col = column_lookup.get("MARCA") or column_lookup.get("BRAND")

    if part_col is None:
        usable_cols = [col for col in df.columns if str(col).strip().upper() not in {"ORDER NO", "LINE NO"}]
        part_col = usable_cols[0] if usable_cols else df.columns[0]
    if qty_col is None:
        usable_cols = [col for col in df.columns if col != part_col and str(col).strip().upper() not in {"ORDER NO", "LINE NO"}]
        qty_col = usable_cols[0] if usable_cols else df.columns[-1]

    editor_df = pd.DataFrame(
        {
            "PART NO": df[part_col],
            "PCS": df[qty_col],
            "DESCRIPCION": df[desc_col] if desc_col is not None else "",
            "MARCA": df[brand_col] if brand_col is not None else "",
        }
    )
    return order_items_to_editor_df(editor_df)


def empty_monthly_order(source_name: str = "Sin pedido a fabrica") -> pd.DataFrame:
    df = pd.DataFrame(columns=["part_key", "part_no", "monthly_order_qty"])
    df.attrs["order_code"] = ""
    df.attrs["source_file_name"] = source_name
    return df


def build_mazda_order_to_request(pedido_inteligente: pd.DataFrame) -> pd.DataFrame:
    editable_df = build_editable_order_from_intelligent(pedido_inteligente)
    return format_order_for_factory_download(editable_df)


# =========================================================
# Motor principal
# =========================================================
def merge_all(sales: pd.DataFrame, stock: pd.DataFrame, backorder: pd.DataFrame, order: pd.DataFrame) -> pd.DataFrame:
    sales = ensure_part_identity_columns(sales, allow_mazda_compact=False)
    stock = ensure_part_identity_columns(stock, allow_mazda_compact=False)
    backorder = ensure_part_identity_columns(backorder, allow_mazda_compact=True)
    order = ensure_part_identity_columns(order, allow_mazda_compact=True)

    sales_base = sales[
        [
            "part_key",
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
    ].rename(columns={"part_no": "part_no_sales"}).copy()

    stock_base = stock[["part_key", "part_no", "description", "brand", "stock"]].rename(
        columns={"part_no": "part_no_stock", "description": "description_stock", "brand": "brand_stock"}
    )
    backorder_base = backorder[["part_key", "part_no", "description", "brand", "backorder_qty"]].rename(
        columns={"part_no": "part_no_backorder", "description": "description_backorder", "brand": "brand_backorder"}
    )
    order_base = order[["part_key", "part_no", "monthly_order_qty"]].rename(columns={"part_no": "part_no_order"}).copy()

    merged = (
        sales_base.merge(stock_base, on="part_key", how="outer")
        .merge(backorder_base, on="part_key", how="outer")
        .merge(order_base, on="part_key", how="outer")
    )

    part_source_cols = ["part_no_sales", "part_no_stock", "part_no_backorder", "part_no_order"]
    merged["part_no"] = merged[part_source_cols].apply(
        lambda row: choose_latest_part_code(row.tolist(), allow_mazda_compact=True),
        axis=1,
    )
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

    merged = merged.drop(
        columns=[
            "part_no_sales",
            "part_no_stock",
            "part_no_backorder",
            "part_no_order",
            "description_stock",
            "brand_stock",
            "description_backorder",
            "brand_backorder",
        ]
    )
    return merged


def add_inventory_logic(df: pd.DataFrame, target_months: int, lead_time_months: int) -> pd.DataFrame:
    out = df.copy()
    out["months_of_stock"] = out["stock"] / out["avg_monthly_units"].replace(0, pd.NA)
    out["months_of_stock"] = pd.to_numeric(out["months_of_stock"], errors="coerce").fillna(999.0)

    out["target_stock_qty"] = (out["avg_monthly_units"] * target_months).apply(math.ceil)
    out["lead_time_need_qty"] = (out["avg_monthly_units"] * lead_time_months).apply(math.ceil)
    out["suggested_order_qty"] = (out["target_stock_qty"] - out["available_plus_pipeline"]).clip(lower=0).apply(math.ceil)

    def define_status(row):
        if row["backorder_qty"] > 0:
            return "Backorder"
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
        "Backorder": 70,
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
        open_orders_base = ensure_part_identity_columns(open_orders_df, allow_mazda_compact=True)[
            ["part_key", "part_no", "open_order_qty_db"]
        ].rename(columns={"part_no": "part_no_open"})
        final_df = final_df.merge(open_orders_base, on="part_key", how="left")
        final_df["part_no"] = final_df[["part_no", "part_no_open"]].apply(
            lambda row: choose_latest_part_code(row.tolist(), allow_mazda_compact=True),
            axis=1,
        )
        final_df = final_df.drop(columns=["part_no_open"])
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
                transport_type TEXT,
                lead_time_days INTEGER DEFAULT 0,
                eta_date TEXT,
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
        ensure_column(conn, "factory_order_batches", "transport_type", "TEXT")
        ensure_column(conn, "factory_order_batches", "lead_time_days", "INTEGER DEFAULT 0")
        ensure_column(conn, "factory_order_batches", "eta_date", "TEXT")
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

    order_base = ensure_part_identity_columns(order_df, allow_mazda_compact=True)
    final_base = ensure_part_identity_columns(final_df, allow_mazda_compact=True)
    order_enriched = order_base.merge(
        final_base[["part_key", "part_no", "description", "brand"]].drop_duplicates("part_key"),
        on="part_key",
        how="left",
        suffixes=("_order", ""),
    ).copy()
    order_enriched["part_no"] = order_enriched[["part_no", "part_no_order"]].apply(
        lambda row: choose_latest_part_code(row.tolist(), allow_mazda_compact=True),
        axis=1,
    )
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


def replace_order_items(conn, batch_id: int, items_df: pd.DataFrame, status: str):
    normalized = normalize_order_items_df(items_df)
    if normalized.empty:
        raise ValueError("El pedido no tiene lineas con codigo y cantidad mayor a cero.")

    conn.execute("DELETE FROM factory_order_items WHERE batch_id = ?", (batch_id,))
    open_when_confirmed = status != ORDER_DRAFT_STATUS
    item_rows = []
    for _, row in normalized.iterrows():
        qty = float(row["quantity"])
        item_rows.append(
            (
                batch_id,
                safe_text(row["part_no"]),
                safe_text(row["description"]),
                safe_text(row["brand"]),
                qty,
                0.0,
                qty if open_when_confirmed else 0.0,
                None,
                ORDER_CONFIRMED_STATUS if open_when_confirmed else ORDER_DRAFT_STATUS,
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
    conn.execute(
        """
        UPDATE factory_order_batches
        SET total_items = ?, total_qty = ?
        WHERE id = ?
        """,
        (int(len(normalized)), float(normalized["quantity"].sum()), batch_id),
    )


def create_editable_order_draft(
    empresa: str,
    analysis_month: str,
    order_name: str,
    items_df: pd.DataFrame,
    notes: str = "",
) -> int:
    order_name = safe_text(order_name) or f"Pedido Mazda {analysis_month}"
    created_at = datetime.now().replace(microsecond=0).isoformat()
    source_seed = f"{empresa}|{analysis_month}|{order_name}|{datetime.now().isoformat(timespec='microseconds')}"
    source_hash = "editable:" + hashlib.sha256(source_seed.encode("utf-8")).hexdigest()

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO factory_order_batches (
                run_id, empresa, analysis_month, created_at, source_type, order_name, order_code,
                order_file_hash, file_name, total_items, total_qty, status, source_hash, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                None,
                empresa,
                analysis_month,
                created_at,
                EDITABLE_ORDER_SOURCE_TYPE,
                order_name,
                order_name,
                "",
                "pedido_editable",
                ORDER_DRAFT_STATUS,
                source_hash,
                notes,
            ),
        )
        batch_id = int(cursor.lastrowid)
        replace_order_items(conn, batch_id, items_df, ORDER_DRAFT_STATUS)
        conn.commit()
        return batch_id


def save_final_mazda_order(
    empresa: str,
    analysis_month: str,
    order_number: str,
    items_df: pd.DataFrame,
    file_name: str = "",
    notes: str = "",
) -> tuple[int, bool]:
    classification = classify_order_number(order_number)
    order_code = classification["order_code"]
    if not order_code:
        raise ValueError("Ingresa el numero de pedido Mazda.")
    if classification["transport_type"] == "SIN_CLASIFICAR":
        raise ValueError(classification["label"])

    normalized = normalize_order_items_df(items_df)
    if normalized.empty:
        raise ValueError("El pedido final no tiene lineas con codigo y cantidad mayor a cero.")

    created_at = datetime.now().replace(microsecond=0).isoformat()
    eta_date = estimate_order_eta(created_at, classification["lead_time_days"])
    source_hash = f"mazda-final:{empresa}:{order_code}"

    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM factory_order_batches
            WHERE empresa = ?
              AND source_type = ?
              AND order_code = ?
              AND status <> 'CANCELADO'
            """,
            (empresa, FINAL_MAZDA_ORDER_SOURCE_TYPE, order_code),
        ).fetchone()
        if existing:
            return int(existing["id"]), True

        cursor = conn.execute(
            """
            INSERT INTO factory_order_batches (
                run_id, empresa, analysis_month, created_at, source_type, order_name, order_code,
                order_file_hash, file_name, transport_type, lead_time_days, eta_date,
                total_items, total_qty, status, source_hash, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                None,
                empresa,
                analysis_month,
                created_at,
                FINAL_MAZDA_ORDER_SOURCE_TYPE,
                order_code,
                order_code,
                "",
                file_name,
                classification["transport_type"],
                int(classification["lead_time_days"]),
                eta_date,
                ORDER_CONFIRMED_STATUS,
                source_hash,
                notes,
            ),
        )
        batch_id = int(cursor.lastrowid)
        replace_order_items(conn, batch_id, normalized, ORDER_CONFIRMED_STATUS)
        conn.commit()
        return batch_id, False


def load_editable_order_batches(empresa: str, limit: int = 50) -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                id,
                analysis_month,
                created_at,
                COALESCE(order_code, order_name) AS order_code,
                order_name,
                total_items,
                total_qty,
                status,
                notes
            FROM factory_order_batches
            WHERE empresa = ?
              AND source_type = ?
              AND status <> 'CANCELADO'
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            conn,
            params=(empresa, EDITABLE_ORDER_SOURCE_TYPE, limit),
        )
    return df


def load_order_batch(batch_id: int):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM factory_order_batches
            WHERE id = ?
            """,
            (int(batch_id),),
        ).fetchone()
    return row


def load_order_items(batch_id: int) -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                part_no,
                description,
                brand,
                quantity
            FROM factory_order_items
            WHERE batch_id = ?
            ORDER BY id
            """,
            conn,
            params=(int(batch_id),),
        )
    return df


def update_editable_order_draft(batch_id: int, order_name: str, items_df: pd.DataFrame, notes: str = ""):
    order_name = safe_text(order_name) or f"Pedido editable #{batch_id}"
    with get_connection() as conn:
        batch = conn.execute(
            "SELECT status FROM factory_order_batches WHERE id = ? AND source_type = ?",
            (int(batch_id), EDITABLE_ORDER_SOURCE_TYPE),
        ).fetchone()
        if batch is None:
            raise ValueError("No se encontro el pedido editable.")
        if batch["status"] != ORDER_DRAFT_STATUS:
            raise ValueError("Este pedido ya fue confirmado y no se puede modificar.")

        replace_order_items(conn, batch_id, items_df, ORDER_DRAFT_STATUS)
        conn.execute(
            """
            UPDATE factory_order_batches
            SET order_name = ?, order_code = ?, notes = ?
            WHERE id = ?
            """,
            (order_name, order_name, notes, int(batch_id)),
        )
        conn.commit()


def confirm_editable_order(batch_id: int, order_name: str, items_df: pd.DataFrame, notes: str = ""):
    order_name = safe_text(order_name) or f"Pedido editable #{batch_id}"
    with get_connection() as conn:
        batch = conn.execute(
            "SELECT status FROM factory_order_batches WHERE id = ? AND source_type = ?",
            (int(batch_id), EDITABLE_ORDER_SOURCE_TYPE),
        ).fetchone()
        if batch is None:
            raise ValueError("No se encontro el pedido editable.")
        if batch["status"] != ORDER_DRAFT_STATUS:
            raise ValueError("Este pedido ya fue confirmado y no se puede modificar.")

        replace_order_items(conn, batch_id, items_df, ORDER_CONFIRMED_STATUS)
        conn.execute(
            """
            UPDATE factory_order_batches
            SET order_name = ?, order_code = ?, notes = ?, status = ?
            WHERE id = ?
            """,
            (order_name, order_name, notes, ORDER_CONFIRMED_STATUS, int(batch_id)),
        )
        conn.commit()


def load_order_export_df(batch_id: int) -> pd.DataFrame:
    batch = load_order_batch(batch_id)
    if batch is None:
        return pd.DataFrame(columns=["ORDER NO", "LINE NO", "PART NO", "PCS"])
    items_df = load_order_items(batch_id)
    return format_order_for_factory_download(items_df, safe_text(batch["order_code"] or batch["order_name"]))


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
                COALESCE(b.transport_type, '') AS tipo_envio,
                COALESCE(b.lead_time_days, 0) AS demora_dias,
                COALESCE(b.eta_date, '') AS llegada_estimada,
                b.total_items,
                b.total_qty,
                ROUND(COALESCE(SUM(i.open_qty), 0), 2) AS qty_abierta,
                b.status
            FROM factory_order_batches b
            LEFT JOIN factory_order_items i ON i.batch_id = b.id
            WHERE b.empresa = ?
            GROUP BY b.id, b.analysis_month, b.created_at, b.source_type, b.order_code, b.order_name, b.file_name, b.transport_type, b.lead_time_days, b.eta_date, b.total_items, b.total_qty, b.status
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
    if df.empty:
        df["part_key"] = []
        return df

    df = ensure_part_identity_columns(df, allow_mazda_compact=False)
    df = (
        df.groupby("part_key", as_index=False)
        .agg(
            part_no=("part_no", lambda values: choose_latest_part_code(values, allow_mazda_compact=True)),
            description=("description", first_non_empty),
            brand=("brand", first_non_empty),
            sales_units=("sales_units", "sum"),
            avg_monthly_units=("avg_monthly_units", "sum"),
            stock=("stock", "sum"),
            backorder_qty=("backorder_qty", "sum"),
            monthly_order_qty=("monthly_order_qty", "sum"),
        )
    )
    return df


def load_open_factory_orders_by_part(empresa: str, exclude_order_file_hash: Optional[str] = None) -> pd.DataFrame:
    params = [empresa]
    query = """
        SELECT
            i.part_no,
            COALESCE(i.open_qty, 0) AS open_order_qty_db
        FROM factory_order_items i
        INNER JOIN factory_order_batches b ON b.id = i.batch_id
        WHERE b.empresa = ?
          AND b.status NOT IN ('CANCELADO', 'BORRADOR')
          AND COALESCE(i.open_qty, 0) > 0
    """

    if exclude_order_file_hash:
        query += " AND COALESCE(b.order_file_hash, '') <> ?"
        params.append(exclude_order_file_hash)

    with get_connection() as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return pd.DataFrame(columns=["part_key", "part_no", "open_order_qty_db"])

    df = ensure_part_identity_columns(df, allow_mazda_compact=True)
    df = df[df["part_key"] != ""].copy()
    return (
        df.groupby("part_key", as_index=False)
        .agg(
            part_no=("part_no", lambda values: choose_latest_part_code(values, allow_mazda_compact=True)),
            open_order_qty_db=("open_order_qty_db", "sum"),
        )
    )


def load_order_history_by_part(empresa: str) -> pd.DataFrame:
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT
                i.part_no,
                COALESCE(i.quantity, 0) AS ordered_total_db,
                COALESCE(i.received_qty, 0) AS received_total_db,
                COALESCE(i.open_qty, 0) AS open_order_qty_db,
                b.created_at AS last_order_at,
                COALESCE(b.order_code, b.order_name) AS last_order_code,
                b.id AS batch_id
            FROM factory_order_items i
            INNER JOIN factory_order_batches b ON b.id = i.batch_id
            WHERE b.empresa = ?
              AND b.status NOT IN ('CANCELADO', 'BORRADOR')
            """,
            conn,
            params=(empresa,),
        )
    if df.empty:
        return pd.DataFrame(
            columns=[
                "part_key",
                "part_no",
                "ordered_total_db",
                "received_total_db",
                "open_order_qty_db",
                "last_order_at",
                "last_order_code",
                "order_batches_db",
            ]
        )

    df = ensure_part_identity_columns(df, allow_mazda_compact=True)
    df = df[df["part_key"] != ""].copy()
    latest_codes = df.groupby("part_key")["part_no"].agg(
        lambda values: choose_latest_part_code(values, allow_mazda_compact=True)
    )
    grouped = (
        df.groupby("part_key", as_index=False)
        .agg(
            ordered_total_db=("ordered_total_db", "sum"),
            received_total_db=("received_total_db", "sum"),
            open_order_qty_db=("open_order_qty_db", "sum"),
            last_order_at=("last_order_at", "max"),
            last_order_code=("last_order_code", first_non_empty),
            order_batches_db=("batch_id", "nunique"),
        )
    )
    grouped["part_no"] = grouped["part_key"].map(latest_codes)
    return grouped[
        [
            "part_key",
            "part_no",
            "ordered_total_db",
            "received_total_db",
            "open_order_qty_db",
            "last_order_at",
            "last_order_code",
            "order_batches_db",
        ]
    ]


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

    open_items = conn.execute(
        """
        SELECT
            i.id,
            i.batch_id,
            i.part_no,
            COALESCE(i.received_qty, 0) AS received_qty,
            COALESCE(i.open_qty, i.quantity) AS open_qty
        FROM factory_order_items i
        INNER JOIN factory_order_batches b ON b.id = i.batch_id
        WHERE b.empresa = ?
          AND b.status NOT IN ('CANCELADO', 'BORRADOR')
          AND COALESCE(i.open_qty, 0) > 0
        ORDER BY b.created_at ASC, i.id ASC
        """,
        (empresa,),
    ).fetchall()

    open_items_by_key = {}
    for item in open_items:
        part_key = normalize_part_key(item["part_no"], allow_mazda_compact=True)
        if not part_key:
            continue
        open_items_by_key.setdefault(part_key, []).append(
            {
                "id": item["id"],
                "batch_id": item["batch_id"],
                "received_qty": float(item["received_qty"] or 0),
                "open_qty": float(item["open_qty"] or 0),
            }
        )

    for _, row in receipts_df.iterrows():
        remaining_qty = float(row["estimated_receipts_qty"])
        if remaining_qty <= 0:
            continue

        row_part_key = normalize_part_key(row["part_no"], allow_mazda_compact=True)
        open_items = open_items_by_key.get(row_part_key, [])

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
            item["open_qty"] = new_open_qty
            item["received_qty"] = new_received_qty
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

    out = ensure_part_identity_columns(out, allow_mazda_compact=True)
    order_history = ensure_part_identity_columns(order_history, allow_mazda_compact=True)
    out = out.merge(
        order_history.drop(columns=["part_no"], errors="ignore"),
        on="part_key",
        how="left",
    )
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

    previous_items = ensure_part_identity_columns(previous_items, allow_mazda_compact=True)
    out = out.merge(
        previous_items[["part_key", "stock_prev", "backorder_prev", "avg_monthly_units_prev"]],
        on="part_key",
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


def render_saved_factory_orders(empresa: str):
    st.subheader("Pedidos a fabrica guardados en base")
    batches_df = load_recent_order_batches(empresa)
    if batches_df.empty:
        st.info("Todavia no hay pedidos a fabrica registrados en la base.")
        return

    display_df = batches_df.rename(
        columns={
            "lote_id": "ID",
            "mes_analisis": "MES",
            "fecha_carga": "FECHA",
            "origen": "ORIGEN",
            "nombre_lote": "PEDIDO",
            "archivo": "ARCHIVO",
            "tipo_envio": "TIPO",
            "demora_dias": "DEMORA DIAS",
            "llegada_estimada": "LLEGADA ESTIMADA",
            "total_items": "ITEMS",
            "total_qty": "TOTAL PCS",
            "qty_abierta": "PCS ABIERTAS",
            "status": "ESTADO",
        }
    )
    st.dataframe(display_df, use_container_width=True, height=240, hide_index=True)

    def option_label(row) -> str:
        tipo = safe_text(row.get("tipo_envio", "")) or "SIN TIPO"
        qty = _format_qty(row.get("total_qty", 0))
        return f"#{int(row['lote_id'])} - {row['nombre_lote']} - {tipo} - {row['status']} - {qty} pcs"

    labels = [option_label(row) for _, row in batches_df.iterrows()]
    selected_label = st.selectbox("Ver pedido guardado", labels, key="saved_factory_order_selector")
    selected_index = labels.index(selected_label)
    selected_batch = batches_df.iloc[selected_index]
    selected_batch_id = int(selected_batch["lote_id"])
    export_df = load_order_export_df(selected_batch_id)
    items_df = order_items_to_editor_df(load_order_items(selected_batch_id))

    detail_col_1, detail_col_2 = st.columns([2, 1])
    with detail_col_1:
        st.caption("Lineas guardadas")
        st.dataframe(items_df, use_container_width=True, height=260, hide_index=True)
    with detail_col_2:
        st.metric("Pedido", safe_text(selected_batch["nombre_lote"]))
        st.metric("Estado", safe_text(selected_batch["status"]))
        tipo = safe_text(selected_batch.get("tipo_envio", "")) or "Sin clasificar"
        llegada = safe_text(selected_batch.get("llegada_estimada", "")) or "-"
        st.write(f"Tipo: {tipo}")
        st.write(f"Llegada estimada: {llegada}")
        if safe_text(selected_batch["status"]) != ORDER_DRAFT_STATUS:
            st.info("Pedido confirmado. No se modifica desde el sistema.")
        st.download_button(
            "Descargar pedido guardado",
            data=dataframe_to_excel_bytes(export_df, sheet_name="Pedido Mazda"),
            file_name=f"pedido_mazda_{safe_text(selected_batch['nombre_lote']) or selected_batch_id}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            disabled=export_df.empty,
            key=f"download_saved_order_{selected_batch_id}",
        )


def render_history_sections(empresa: str):
    render_saved_factory_orders(empresa)

    st.subheader("Historial guardado")
    history_df = load_recent_runs(empresa)
    if history_df.empty:
        st.info("Todavia no hay corridas historicas guardadas para esta empresa.")
    else:
        st.dataframe(history_df, use_container_width=True, height=260)


def render_final_order_upload_manager(
    empresa: str,
    analysis_month: str,
    suggested_order_df: pd.DataFrame,
    default_note: str = "",
):
    st.subheader("Enviar pedido a fabrica")
    st.caption("Ajusta el pedido sugerido en la tabla, o carga un Excel final modificado. Al enviarlo queda guardado y bloqueado.")

    input_col_1, input_col_2 = st.columns([1, 2])
    order_number = input_col_1.text_input(
        "Numero de pedido Mazda",
        placeholder="HCCA, HCJV, HC1A...",
        key=f"final_mazda_order_number_{analysis_month}",
    )
    classification = classify_order_number(order_number)
    if classification["order_code"]:
        if classification["transport_type"] == "SIN_CLASIFICAR":
            input_col_1.warning(classification["label"])
        else:
            input_col_1.success(classification["label"])

    final_upload = input_col_2.file_uploader(
        "Excel final si ya lo modificaste afuera",
        type=["xlsx", "xls"],
        key=f"final_mazda_order_file_{analysis_month}",
    )

    if final_upload is not None:
        try:
            final_items_df = load_final_mazda_order_file(final_upload)
            final_file_name = final_upload.name
        except Exception as exc:
            st.error(f"No se pudo leer el Excel final: {exc}")
            return
    else:
        final_items_df = suggested_order_df.copy()
        final_file_name = "pedido_sugerido_ajustado.xlsx"

    st.caption("Pedido a enviar. Podes cambiar PCS, eliminar lineas o agregar nuevos codigos antes de enviar.")
    editor_key_suffix = final_upload.name if final_upload is not None else "pedido_sugerido"
    edited_items_df = st.data_editor(
        order_items_to_editor_df(final_items_df),
        hide_index=True,
        use_container_width=True,
        num_rows="dynamic",
        key=f"final_mazda_order_editor_{analysis_month}_{editor_key_suffix}",
        column_config={
            "PART NO": st.column_config.TextColumn("PART NO", required=True),
            "PCS": st.column_config.NumberColumn("PCS", min_value=0.0, step=1.0, required=True),
            "DESCRIPCION": st.column_config.TextColumn("DESCRIPCION"),
            "MARCA": st.column_config.TextColumn("MARCA"),
        },
    )

    export_df = format_order_for_factory_download(edited_items_df, classification["order_code"])
    if export_df.empty:
        st.info("No hay lineas para guardar en el pedido final.")
        return

    st.caption("Vista previa del Excel que se guardara y enviara.")
    st.dataframe(export_df, use_container_width=True, height=260)
    action_col_1, action_col_2 = st.columns([1, 2])
    action_col_1.download_button(
        "Descargar final",
        data=dataframe_to_excel_bytes(export_df, sheet_name="Pedido Mazda"),
        file_name=f"pedido_mazda_{classification['order_code'] or analysis_month}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"download_final_mazda_order_{analysis_month}",
    )
    if action_col_2.button("Enviar pedido a fabrica", type="primary", key=f"save_final_mazda_order_{analysis_month}"):
        try:
            batch_id, duplicated = save_final_mazda_order(
                empresa=empresa,
                analysis_month=analysis_month,
                order_number=order_number,
                items_df=edited_items_df,
                file_name=final_file_name,
                notes=default_note,
            )
            if duplicated:
                st.warning(f"El pedido {classification['order_code']} ya estaba guardado como lote #{batch_id}.")
            else:
                st.success(f"Pedido {classification['order_code']} enviado a fabrica y guardado como lote #{batch_id}.")
            st.rerun()
        except Exception as exc:
            st.error(f"No se pudo enviar el pedido a fabrica: {exc}")


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
        register_current_order = False
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
        code_unification_report = collect_code_unification_reports(backorder_df, order_df)
        formatted_code_count = count_formatted_codes(backorder_df, order_df)
    except Exception as exc:
        st.error(f"Error procesando archivos: {exc}")
        st.stop()

    if formatted_code_count:
        st.info(
            f"Se normalizaron {formatted_code_count} codigos de Backorder/Pedido a fabrica "
            "con guiones o letras de modificacion."
        )

    if not code_unification_report.empty:
        st.warning(
            "Se detectaron codigos equivalentes con distinta letra de modificacion. "
            "El sistema sumo las cantidades y dejo la letra mas nueva."
        )
        st.dataframe(code_unification_report, use_container_width=True, hide_index=True)

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
        term_key = normalize_part_key(term, allow_mazda_compact=True)
        term_display = normalize_part_display(term, allow_mazda_compact=True)
        view = view[
            view["part_no"].astype(str).str.upper().str.contains(term, na=False, regex=False)
            | view["part_no"].astype(str).str.upper().str.contains(term_display, na=False, regex=False)
            | view["part_key"].astype(str).str.upper().str.contains(term_key, na=False, regex=False)
            | view["description"].astype(str).str.upper().str.contains(term, na=False, regex=False)
        ]

    stock_dead_units = int(view.loc[view["stock_muerto"], "stock"].sum())

    metric_top_1, metric_top_2, metric_top_3, metric_top_4 = st.columns(4)
    metric_top_1.metric("Articulos", f"{len(view):,}")
    metric_top_2.metric("Art. stock muerto", f"{int(view['stock_muerto'].sum()):,}")
    metric_top_3.metric("Unid. stock muerto", f"{stock_dead_units:,}")
    metric_top_4.metric("Art. ofertas", f"{int(view['oferta_sugerida'].sum()):,}")

    metric_bottom_1, metric_bottom_2, metric_bottom_3, metric_bottom_4 = st.columns(4)
    metric_bottom_1.metric("Unid. pedido archivo", f"{int(view['monthly_order_qty'].sum()):,}")
    metric_bottom_2.metric("Art. sugeridos compra", f"{int((view['suggested_order_qty'] > 0).sum()):,}")
    metric_bottom_3.metric("Unid. sugeridas compra", f"{int(view['suggested_order_qty'].sum()):,}")
    metric_bottom_4.metric("Unid. abierto DB", f"{int(view['open_order_qty_db'].sum()):,}")

    st.subheader("Detalle compra sugerida")
    suggested_detail = view[view["suggested_order_qty"] > 0].copy()
    if suggested_detail.empty:
        st.info("No hay articulos con compra sugerida para los filtros actuales.")
    else:
        suggested_detail = suggested_detail.sort_values(
            ["smart_score", "suggested_order_qty", "sales_units"],
            ascending=[False, False, False],
        )
        suggested_cols = [
            "part_no",
            "description",
            "brand",
            "abc",
            "status",
            "sales_units",
            "stock",
            "backorder_qty",
            "monthly_order_qty",
            "open_order_qty_db",
            "available_plus_pipeline",
            "target_stock_qty",
            "suggested_order_qty",
        ]
        suggested_view = suggested_detail[suggested_cols].rename(
            columns={
                "part_no": "codigo",
                "description": "descripcion",
                "brand": "marca",
                "status": "estado",
                "sales_units": "ventas_3_anios",
                "backorder_qty": "backorder",
                "monthly_order_qty": "pedido_archivo",
                "open_order_qty_db": "abierto_db",
                "available_plus_pipeline": "stock_mas_pedidos",
                "target_stock_qty": "stock_objetivo",
                "suggested_order_qty": "cantidad_sugerida",
            }
        )
        st.dataframe(suggested_view, use_container_width=True, height=360)
        st.download_button(
            "Descargar detalle compra sugerida",
            data=dataframe_to_excel_bytes(suggested_view),
            file_name=f"detalle_compra_sugerida_{analysis_month}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

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
        plot_df = top_sales.head(15)
        if plt is not None:
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.bar(plot_df["part_no"], plot_df["sales_units"])
            ax.set_title("Top 15 por unidades vendidas")
            ax.set_xlabel("Codigo")
            ax.set_ylabel("Unidades")
            ax.tick_params(axis="x", rotation=60)
            fig.tight_layout()
            st.pyplot(fig)
        else:
            st.bar_chart(plot_df.set_index("part_no")["sales_units"])

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
    pedido_editor_df = build_editable_order_from_intelligent(pedido_inteligente)
    pedido_mazda_df = format_order_for_factory_download(pedido_editor_df)
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

    render_final_order_upload_manager(
        empresa=empresa_activa,
        analysis_month=analysis_month,
        suggested_order_df=pedido_editor_df,
        default_note=save_note,
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
            "codigos_unificados": code_unification_report,
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
