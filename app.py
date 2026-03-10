import os
import re
import time
import unicodedata
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

TZ = ZoneInfo("America/Sao_Paulo")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

EVENTS_TAB = "EVENTS"
EVENTS_HEADER = [
    "ts_iso",
    "data",
    "hora",
    "dia_semana",
    "user_login",
    "user_nome",
    "area_id",
    "turno",
    "item_id",
    "texto",
    "status",
    "obs",
]

WS_AREAS_CANDIDATES = ["AREAS", "Areas", "areas"]
WS_ITENS_CANDIDATES = ["ITENS", "Itens", "itens"]
WS_USERS_CANDIDATES = ["USUARIOS", "Usuarios", "usuarios", "USERS", "Users", "users"]


def _get_cfg(name: str, default: str = "") -> str:
    if hasattr(st, "secrets") and name in st.secrets:
        return str(st.secrets[name]).strip()
    if hasattr(st, "secrets") and "app" in st.secrets and name in st.secrets["app"]:
        return str(st.secrets["app"][name]).strip()
    return os.getenv(name, default).strip()


def normalize_sheet_id(value: str) -> str:
    v = (value or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", v)
    return m.group(1) if m else v


CONFIG_SHEET_ID = normalize_sheet_id(_get_cfg("CONFIG_SHEET_ID", ""))
RULES_SHEET_ID = normalize_sheet_id(_get_cfg("RULES_SHEET_ID", ""))
LOGS_SHEET_ID = normalize_sheet_id(_get_cfg("LOGS_SHEET_ID", ""))


def require_ids():
    missing = [
        k for k, v in [
            ("CONFIG_SHEET_ID", CONFIG_SHEET_ID),
            ("RULES_SHEET_ID", RULES_SHEET_ID),
            ("LOGS_SHEET_ID", LOGS_SHEET_ID),
        ]
        if not v
    ]
    if missing:
        raise RuntimeError(f"Secrets faltando: {', '.join(missing)}")


def retryable(fn, tries=6, base_sleep=0.8, max_sleep=10.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except APIError as e:
            last = e
            msg = str(e)
            is_quota = ("429" in msg) or ("Quota exceeded" in msg) or ("RESOURCE_EXHAUSTED" in msg)
            if (not is_quota) and i >= 1:
                raise
            time.sleep(min(max_sleep, base_sleep * (2 ** i)))
    raise last


@st.cache_resource
def gs_client():
    if "gcp_service_account" not in st.secrets:
        raise RuntimeError("Secrets precisa ter [gcp_service_account].")
    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet(sheet_id: str):
    client = gs_client()
    return retryable(lambda: client.open_by_key(sheet_id))


def list_tabs(sheet_id: str) -> List[str]:
    sh = open_sheet(sheet_id)
    return [ws.title for ws in sh.worksheets()]


def pick_tab(sheet_id: str, candidates: List[str]) -> str:
    titles = list_tabs(sheet_id)
    s = set(titles)
    for c in candidates:
        if c in s:
            return c
    lower = {t.lower(): t for t in titles}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    raise RuntimeError(f"Nenhuma aba encontrada em {sheet_id}. Candidatas: {candidates}. Existentes: {titles}")


def get_or_create_tab(sheet_id: str, title: str, rows=5000, cols=30):
    sh = open_sheet(sheet_id)

    def _do():
        try:
            return sh.worksheet(title)
        except Exception:
            return sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))

    return retryable(_do)


def read_all_values(sheet_id: str, tab: str) -> List[List[str]]:
    sh = open_sheet(sheet_id)
    ws = sh.worksheet(tab)
    return retryable(lambda: ws.get_all_values())


def write_header_if_empty(ws, header: List[str]):
    first = retryable(lambda: ws.row_values(1))
    if (not first) or all(str(x).strip() == "" for x in first):
        retryable(lambda: ws.append_row(header, value_input_option="RAW"))


def append_row(sheet_id: str, tab: str, row: List[str], header_if_empty: Optional[List[str]] = None):
    sh = open_sheet(sheet_id)
    ws = sh.worksheet(tab)
    if header_if_empty:
        write_header_if_empty(ws, header_if_empty)
    retryable(lambda: ws.append_row(row, value_input_option="RAW"))


def strip_accents(text: str) -> str:
    text = str(text or "").strip()
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def norm_cols(cols: List[str]) -> List[str]:
    out = []
    for c in cols:
        c2 = str(c).strip().lower()
        c2 = strip_accents(c2)
        c2 = re.sub(r"[^a-z0-9]+", "_", c2)
        c2 = re.sub(r"_+", "_", c2).strip("_")
        out.append(c2)
    return out


def to_df(values: List[List[str]]) -> pd.DataFrame:
    if not values:
        return pd.DataFrame()
    header = values[0]
    body = values[1:]
    df = pd.DataFrame(body, columns=header)
    df.columns = norm_cols(list(df.columns))
    return df


def as_bool(x) -> bool:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return False
    s = str(x).strip().lower()
    return s in ["1", "true", "sim", "yes", "y", "ativo"]


def weekday_pt(d: date) -> str:
    names = ["Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo"]
    return names[d.weekday()]


def normalize_weekday_name(value: str) -> str:
    s = str(value or "").strip().lower()
    s = strip_accents(s)
    s = s.replace("-feira", "")
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()

    mapping = {
        "segunda": "Segunda",
        "segunda feira": "Segunda",
        "seg": "Segunda",
        "terca": "Terca",
        "terca feira": "Terca",
        "ter": "Terca",
        "quarta": "Quarta",
        "quarta feira": "Quarta",
        "qua": "Quarta",
        "quinta": "Quinta",
        "quinta feira": "Quinta",
        "qui": "Quinta",
        "sexta": "Sexta",
        "sexta feira": "Sexta",
        "sex": "Sexta",
        "sabado": "Sabado",
        "sab": "Sabado",
        "domingo": "Domingo",
        "dom": "Domingo",
    }
    return mapping.get(s, "")


def pick_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def require_cols(df: pd.DataFrame, required: List[str], friendly: str):
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"{friendly} faltando colunas: {missing}. Colunas atuais: {list(df.columns)}")


def map_areas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ren = {}
    c_id = pick_col(df, ["area_id", "area", "id_area"])
    c_nm = pick_col(df, ["area_nome", "nome", "area_name"])
    if c_id and c_id != "area_id":
        ren[c_id] = "area_id"
    if c_nm and c_nm != "area_nome":
        ren[c_nm] = "area_nome"
    if ren:
        df = df.rename(columns=ren)

    require_cols(df, ["area_id", "area_nome"], "Aba AREAS")
    df["area_id"] = df["area_id"].astype(str).str.strip()
    df["area_nome"] = df["area_nome"].astype(str).str.strip()

    if "ativo" in df.columns:
        df = df[df["ativo"].apply(as_bool) | (df["ativo"].astype(str).str.strip() == "")]
    if "ordem" in df.columns:
        df["ordem"] = pd.to_numeric(df["ordem"], errors="coerce")
        df = df.sort_values(["ordem", "area_nome"], na_position="last")
    else:
        df = df.sort_values(["area_nome"])
    return df.reset_index(drop=True)


def _clean_hhmm(x: str) -> str:
    s = str(x or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{1,2})[:h]?(\d{2})$", s.lower())
    if not m:
        return ""
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return ""
    return f"{hh:02d}:{mm:02d}"


def map_itens(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ren = {}

    raw_cols = list(df.columns)

    c_area = pick_col(df, ["area_id", "area", "id_area"])
    c_turno = pick_col(df, ["turno", "shift"])
    c_item = pick_col(df, ["item_id", "id_item", "id", "codigo"])
    c_text = pick_col(df, ["texto", "item", "descricao", "descrição", "atividade", "tarefa", "nome"])
    c_dead = pick_col(df, ["deadline_hhmm", "deadline", "horario", "hora", "prazo", "horario_hhmm"])
    c_dia = pick_col(df, ["dia_semana", "dias_semana", "dia_da_semana", "dias_da_semana", "dia", "weekday"])

    if c_area and c_area != "area_id":
        ren[c_area] = "area_id"
    if c_turno and c_turno != "turno":
        ren[c_turno] = "turno"
    if c_item and c_item != "item_id":
        ren[c_item] = "item_id"
    if c_text and c_text != "texto":
        ren[c_text] = "texto"
    if c_dead and c_dead != "deadline_hhmm":
        ren[c_dead] = "deadline_hhmm"
    if c_dia and c_dia != "dia_semana":
        ren[c_dia] = "dia_semana"

    if ren:
        df = df.rename(columns=ren)

    require_cols(df, ["area_id", "turno", "item_id", "texto"], "Aba ITENS")

    df["area_id"] = df["area_id"].astype(str).str.strip()
    df["turno"] = df["turno"].astype(str).str.strip()
    df["item_id"] = df["item_id"].astype(str).str.strip()
    df["texto"] = df["texto"].astype(str).str.strip()

    if "deadline_hhmm" in df.columns:
        df["deadline_hhmm"] = df["deadline_hhmm"].apply(_clean_hhmm)
    else:
        df["deadline_hhmm"] = ""

    if "dia_semana" not in df.columns:
        if len(raw_cols) >= 16:
            col_p = raw_cols[15]
            df["dia_semana"] = df[col_p].astype(str)
        else:
            df["dia_semana"] = ""

    df["dia_semana"] = df["dia_semana"].astype(str).apply(normalize_weekday_name)

    if "ativo" in df.columns:
        df = df[df["ativo"].apply(as_bool) | (df["ativo"].astype(str).str.strip() == "")]

    if "ordem" in df.columns:
        df["ordem"] = pd.to_numeric(df["ordem"], errors="coerce")
        df = df.sort_values(["area_id", "turno", "ordem", "item_id"], na_position="last")
    else:
        df = df.sort_values(["area_id", "turno", "item_id"])

    return df.reset_index(drop=True)


def map_users(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ren = {}
    c_login = pick_col(df, ["login", "user", "usuario"])
    c_pass = pick_col(df, ["senha", "password", "pass"])
    c_nome = pick_col(df, ["nome", "name"])
    if c_login and c_login != "login":
        ren[c_login] = "login"
    if c_pass and c_pass != "senha":
        ren[c_pass] = "senha"
    if c_nome and c_nome != "nome":
        ren[c_nome] = "nome"
    if ren:
        df = df.rename(columns=ren)

    require_cols(df, ["login", "senha"], "Aba USUARIOS")
    df["login"] = df["login"].astype(str).str.strip()
    df["senha"] = df["senha"].astype(str).str.strip()
    if "ativo" in df.columns:
        df = df[df["ativo"].apply(as_bool) | (df["ativo"].astype(str).str.strip() == "")]
    return df.reset_index(drop=True)


def filter_items_by_weekday(df: pd.DataFrame, weekday_name: str) -> pd.DataFrame:
    weekday_name = normalize_weekday_name(weekday_name)
    if "dia_semana" not in df.columns:
        return df.copy()

    df2 = df.copy()
    df2["dia_semana"] = df2["dia_semana"].fillna("").astype(str).apply(normalize_weekday_name)
    return df2[(df2["dia_semana"] == "") | (df2["dia_semana"] == weekday_name)].copy()


@st.cache_data(ttl=300)
def load_config_tables(cache_buster: int) -> Dict[str, pd.DataFrame]:
    require_ids()
    ws_areas = pick_tab(CONFIG_SHEET_ID, WS_AREAS_CANDIDATES)
    ws_itens = pick_tab(CONFIG_SHEET_ID, WS_ITENS_CANDIDATES)

    areas_raw = to_df(read_all_values(CONFIG_SHEET_ID, ws_areas))
    itens_raw = to_df(read_all_values(CONFIG_SHEET_ID, ws_itens))

    areas = map_areas(areas_raw)
    itens = map_itens(itens_raw)

    return {"areas": areas, "itens": itens}


@st.cache_data(ttl=300)
def load_users_table(cache_buster: int) -> pd.DataFrame:
    require_ids()
    ws_users = pick_tab(RULES_SHEET_ID, WS_USERS_CANDIDATES)
    users_raw = to_df(read_all_values(RULES_SHEET_ID, ws_users))
    return map_users(users_raw)


@st.cache_data(ttl=30)
def load_events_last(cache_buster: int, last_rows: int = 2000) -> pd.DataFrame:
    require_ids()
    ws = get_or_create_tab(LOGS_SHEET_ID, EVENTS_TAB, rows=20000, cols=30)
    write_header_if_empty(ws, EVENTS_HEADER)

    values = retryable(lambda: ws.get_all_values())
    df = to_df(values)
    if df.empty:
        return df
    if len(df) > last_rows:
        df = df.tail(last_rows).reset_index(drop=True)
    return df


def latest_status_map_for_day(events_df: pd.DataFrame, day_iso: str) -> Dict[Tuple[str, str, str], str]:
    if events_df.empty:
        return {}
    needed = {"data", "area_id", "turno", "item_id", "status", "ts_iso"}
    if any(c not in events_df.columns for c in needed):
        return {}

    df = events_df.copy()
    df["data"] = df["data"].astype(str).str.strip()
    df = df[df["data"] == day_iso].copy()
    if df.empty:
        return {}

    df["ts_dt"] = pd.to_datetime(df["ts_iso"], errors="coerce")
    df = df.dropna(subset=["ts_dt"]).sort_values("ts_dt")
    latest = df.groupby(["area_id", "turno", "item_id"], as_index=False).tail(1)

    mp = {}
    for _, r in latest.iterrows():
        key = (str(r["area_id"]).strip(), str(r["turno"]).strip(), str(r["item_id"]).strip())
        mp[key] = str(r["status"]).strip().upper()
    return mp


def parse_deadline_for_day(day_iso: str, hhmm: str) -> Optional[datetime]:
    s = str(hhmm or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{2}):(\d{2})$", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    d = datetime.fromisoformat(day_iso).date()
    return datetime(d.year, d.month, d.day, hh, mm, 0, tzinfo=TZ)


def compute_item_effective_status_for_day(day_iso: str, raw_status: str, deadline_hhmm: str) -> str:
    s = (raw_status or "").strip().upper()
    if s in ["OK", "NAO_OK", "NÃO_OK", "NÃO OK", "NAO OK"]:
        return "NAO_OK" if ("NAO" in s or "NÃO" in s) else "OK"

    dl = parse_deadline_for_day(day_iso, deadline_hhmm)

    if dl is None:
        return "PENDENTE"

    now = datetime.now(TZ)
    day_d = datetime.fromisoformat(day_iso).date()
    today_d = now.date()

    if day_d > today_d:
        return "PENDENTE"

    if day_d < today_d:
        return "ATRASADO"

    if now > dl:
        return "ATRASADO"
    return "PENDENTE"


def card_palette(effective_status: str) -> Tuple[str, str]:
    s = (effective_status or "").strip().upper()
    if s == "OK":
        return "#d1fae5", "Concluido"
    if s == "NAO_OK":
        return "#fee2e2", "Nao OK"
    if s == "ATRASADO":
        return "#ffedd5", "Atrasado"
    return "#f3f4f6", "Pendente"


def _norm_tipo_resposta(x: str) -> str:
    s = str(x or "").strip().upper()
    s = s.replace("NÃO", "NAO")
    if "NUMERO" in s:
        return "NUMERO"
    if "TEXTO" in s:
        return "TEXTO"
    return "OK_NAOOK"


def _safe_float(x: str) -> Optional[float]:
    s = str(x or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def write_event(
    user_login: str,
    user_nome: str,
    area_id: str,
    turno: str,
    item_id: str,
    texto: str,
    status: str,
    obs: str = "",
):
    now = datetime.now(TZ)
    row = [
        now.isoformat(),
        now.date().isoformat(),
        now.strftime("%H:%M:%S"),
        weekday_pt(now.date()),
        user_login,
        user_nome,
        area_id,
        turno,
        item_id,
        texto,
        status,
        obs,
    ]
    append_row(LOGS_SHEET_ID, EVENTS_TAB, row, header_if_empty=EVENTS_HEADER)
    st.session_state["cache_buster"] += 1


def authenticate(users_df: pd.DataFrame) -> Optional[Dict[str, str]]:
    st.session_state.setdefault("logged_in", False)
    st.session_state.setdefault("user_login", "")
    st.session_state.setdefault("user_nome", "")

    if st.session_state["logged_in"]:
        return {"login": st.session_state["user_login"], "nome": st.session_state["user_nome"]}

    st.title("Login")
    st.caption("Acesso protegido por usuario e senha.")

    u = st.text_input("Usuario", key="u")
    p = st.text_input("Senha", type="password", key="p")

    if st.button("Entrar", type="primary"):
        u2 = str(u).strip()
        p2 = str(p).strip()
        hit = users_df[(users_df["login"] == u2) & (users_df["senha"] == p2)]
        if hit.empty:
            st.error("Usuario ou senha invalidos.")
            return None

        nome = u2
        if "nome" in hit.columns:
            nome = str(hit.iloc[0]["nome"]).strip() or u2

        st.session_state["logged_in"] = True
        st.session_state["user_login"] = u2
        st.session_state["user_nome"] = nome
        st.rerun()

    return None


def page_dashboard(cfg: Dict[str, pd.DataFrame], events_df: pd.DataFrame):
    st.subheader("Dashboard operacional")

    areas = cfg["areas"]
    itens = cfg["itens"]

    today_d = datetime.now(TZ).date()
    st.session_state.setdefault("dash_date", today_d)

    colA, colB, colC = st.columns([1.2, 1, 1])
    with colA:
        dash_date = st.date_input("Data do dashboard", value=st.session_state["dash_date"])
        st.session_state["dash_date"] = dash_date
    with colB:
        if st.button("Hoje"):
            st.session_state["dash_date"] = today_d
            st.session_state["cache_buster"] += 1
            st.rerun()
    with colC:
        if st.button("Atualizar agora"):
            st.session_state["cache_buster"] += 1
            st.rerun()

    day_iso = st.session_state["dash_date"].isoformat()
    day_weekday = weekday_pt(st.session_state["dash_date"])
    itens_dia = filter_items_by_weekday(itens, day_weekday)
    mp = latest_status_map_for_day(events_df, day_iso)

    st.info(f"Resumo considerando: {day_weekday} | {day_iso}")

    turnos = sorted(itens_dia["turno"].dropna().astype(str).str.strip().unique().tolist())

    for _, a in areas.iterrows():
        area_id = str(a["area_id"]).strip()
        area_nome = str(a["area_nome"]).strip()
        st.markdown(f"### {area_nome}")

        cols = st.columns(2 if len(turnos) >= 2 else 1)
        for i, turno in enumerate(turnos):
            df_items = itens_dia[(itens_dia["area_id"] == area_id) & (itens_dia["turno"] == turno)].copy()
            total = len(df_items)

            ok = 0
            nok = 0
            atraso = 0

            for _, it in df_items.iterrows():
                item_id = str(it["item_id"]).strip()
                key = (area_id, turno, item_id)
                raw_status = mp.get(key, "PENDENTE")
                deadline = str(it["deadline_hhmm"]).strip() if "deadline_hhmm" in df_items.columns else ""
                eff = compute_item_effective_status_for_day(day_iso, raw_status, deadline)
                if eff == "OK":
                    ok += 1
                elif eff == "NAO_OK":
                    nok += 1
                elif eff == "ATRASADO":
                    atraso += 1

            pct = int(round((ok / total) * 100, 0)) if total else 0

            bg = "#0b6a5a" if (total > 0 and ok == total) else "#1f2937"
            if atraso > 0:
                bg = "#b45309"
            elif nok > 0:
                bg = "#7a1f2b"
            elif ok > 0 and ok < total:
                bg = "#8b6b12"

            with cols[i % len(cols)]:
                st.markdown(
                    f"""
                    <div style="border-radius:16px;padding:14px;margin:8px 0;background:{bg};color:white;">
                      <div style="font-size:16px;font-weight:800;">{turno}</div>
                      <div style="font-size:13px;opacity:0.95;margin-top:6px;">
                        OK {ok}/{total} | Nao OK {nok} | Atrasado {atraso}
                      </div>
                      <div style="font-size:22px;font-weight:900;margin-top:10px;">{pct}%</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )


def page_checklist(cfg: Dict[str, pd.DataFrame], events_df: pd.DataFrame, user: Dict[str, str]):
    st.subheader("Checklist")

    areas = cfg["areas"]
    itens = cfg["itens"]

    today_date = datetime.now(TZ).date()
    today_iso = today_date.isoformat()
    today_weekday = weekday_pt(today_date)

    itens = filter_items_by_weekday(itens, today_weekday)
    mp = latest_status_map_for_day(events_df, today_iso)

    st.info(f"Checklist do dia: {today_weekday} | {today_iso}")

    areas_labels = [f"{r['area_nome']} ({r['area_id']})" for _, r in areas.iterrows()]
    area_sel = st.selectbox("Area", areas_labels, index=0)
    area_id = area_sel.split("(")[-1].replace(")", "").strip()

    turnos = sorted(itens[itens["area_id"] == area_id]["turno"].dropna().astype(str).str.strip().unique().tolist())
    if not turnos:
        st.warning(f"Sem turnos para esta area em {today_weekday}.")
        return

    turno_sel = st.selectbox("Turno", turnos, index=0)

    if st.button("Atualizar lista"):
        st.session_state["cache_buster"] += 1
        st.rerun()

    df_items = itens[(itens["area_id"] == area_id) & (itens["turno"] == turno_sel)].copy()
    if df_items.empty:
        st.warning(f"Sem itens para esta combinacao em {today_weekday}.")
        return

    st.caption("Tudo comeca PENDENTE. Clique OK, Nao OK ou Desmarcar. Para itens NUMERO/TEXTO, preencha o campo e clique OK ou Nao OK.")

    for _, it in df_items.iterrows():
        item_id = str(it["item_id"]).strip()
        texto = str(it["texto"]).strip()
        key = (area_id, turno_sel, item_id)
        raw_status = mp.get(key, "PENDENTE").upper()

        deadline = str(it["deadline_hhmm"]).strip() if "deadline_hhmm" in df_items.columns else ""
        eff = compute_item_effective_status_for_day(today_iso, raw_status, deadline)
        bg, label = card_palette(eff)

        tipo_resposta = _norm_tipo_resposta(it.get("tipo_resposta", ""))
        min_hint = _safe_float(it.get("min", ""))

        deadline_line = ""
        if deadline:
            deadline_line = f"<div style='font-size:12px;margin-top:4px;opacity:0.85;'><b>Deadline:</b> {deadline}</div>"

        tipo_line = ""
        if tipo_resposta in ["NUMERO", "TEXTO"]:
            tipo_line = f"<div style='font-size:12px;margin-top:4px;opacity:0.85;'><b>Entrada:</b> {tipo_resposta}</div>"

        dia_line = f"<div style='font-size:12px;margin-top:4px;opacity:0.85;'><b>Dia:</b> {today_weekday}</div>"

        st.markdown(
            f"""
            <div style="border-radius:14px;padding:12px;background:{bg};margin:10px 0;">
              <div style="font-size:15px;font-weight:900;">{texto}</div>
              <div style="font-size:12px;margin-top:6px;"><b>Status:</b> {label}</div>
              {dia_line}
              {deadline_line}
              {tipo_line}
            </div>
            """,
            unsafe_allow_html=True,
        )

        ok_label = "OK" + (" ✓" if eff == "OK" else "")
        nok_label = "Nao OK" + (" ✗" if eff == "NAO_OK" else "")
        rst_label = "Desmarcar"

        obs_value: str = ""
        obs_key = f"obs_{area_id}_{turno_sel}_{item_id}"

        if tipo_resposta == "NUMERO":
            st.session_state.setdefault(obs_key, "")
            default_num = _safe_float(st.session_state.get(obs_key, ""))
            if default_num is None:
                default_num = min_hint if min_hint is not None else 0.0

            min_value = min_hint if min_hint is not None else None

            val = st.number_input(
                "Valor (numero)",
                value=float(default_num),
                min_value=min_value,
                key=f"in_{obs_key}",
                step=0.5,
            )
            obs_value = str(val)
            st.session_state[obs_key] = obs_value

        elif tipo_resposta == "TEXTO":
            st.session_state.setdefault(obs_key, "")
            val = st.text_input(
                "Observacao (texto)",
                value=str(st.session_state.get(obs_key, "")),
                key=f"in_{obs_key}",
                placeholder="Digite aqui...",
            )
            obs_value = str(val).strip()
            st.session_state[obs_key] = obs_value

        c1, c2, c3 = st.columns([1, 1, 1])

        with c1:
            if st.button(ok_label, key=f"ok_{area_id}_{turno_sel}_{item_id}", type="secondary"):
                if tipo_resposta in ["NUMERO", "TEXTO"] and not str(obs_value).strip():
                    st.warning("Preencha o campo antes de marcar OK.")
                else:
                    write_event(user["login"], user["nome"], area_id, turno_sel, item_id, texto, "OK", obs=str(obs_value).strip())
                    st.rerun()

        with c2:
            if st.button(nok_label, key=f"nok_{area_id}_{turno_sel}_{item_id}", type="secondary"):
                write_event(user["login"], user["nome"], area_id, turno_sel, item_id, texto, "NAO_OK", obs=str(obs_value).strip())
                st.rerun()

        with c3:
            if st.button(rst_label, key=f"rst_{area_id}_{turno_sel}_{item_id}", type="secondary"):
                write_event(user["login"], user["nome"], area_id, turno_sel, item_id, texto, "PENDENTE", obs="")
                st.session_state[obs_key] = ""
                st.rerun()


def page_events(events_df: pd.DataFrame):
    st.subheader("EVENTS")
    if st.button("Atualizar EVENTS"):
        st.session_state["cache_buster"] += 1
        st.rerun()
    st.dataframe(events_df, use_container_width=True, height=560)


def main():
    st.set_page_config(page_title="Checklist Operacional", layout="wide")

    st.session_state.setdefault("cache_buster", 1)
    require_ids()

    with st.sidebar:
        st.markdown("## Checklist Operacional")

        if st.button("Logout"):
            for k in list(st.session_state.keys()):
                if k not in ["cache_buster", "dash_date"]:
                    st.session_state.pop(k, None)
            st.session_state["cache_buster"] += 1
            st.rerun()

        st.markdown("### Status")
        try:
            _ = gs_client()
            st.success("Google Sheets conectado")
        except Exception as e:
            st.error(f"Falha ao conectar: {e}")

        st.markdown("### Navegacao")
        st.session_state.setdefault("nav", "Dashboard")
        st.radio("Ir para", ["Dashboard", "Checklist", "EVENTS"], key="nav", label_visibility="collapsed")

    cfg = load_config_tables(st.session_state["cache_buster"])
    users_df = load_users_table(st.session_state["cache_buster"])
    events_df = load_events_last(st.session_state["cache_buster"], last_rows=2000)

    user = authenticate(users_df)
    if not user:
        return

    nav = st.session_state.get("nav", "Dashboard")
    if nav == "Checklist":
        page_checklist(cfg, events_df, user)
    elif nav == "EVENTS":
        page_events(events_df)
    else:
        page_dashboard(cfg, events_df)


if __name__ == "__main__":
    main()
