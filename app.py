import dash
from dash import html, dcc, Input, Output, State
import dash_bootstrap_components as dbc
import requests
import pandas as pd
from datetime import datetime, timedelta 
import pytz
import time
import plotly.express as px
import plotly.graph_objects as go
import concurrent.futures
from db import upsert_daily_from_visitors

# =======================================================
# CONFIGURAÇÕES
# =======================================================
brazil_tz = pytz.timezone('America/Sao_Paulo')

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        'https://use.fontawesome.com/releases/v5.15.4/css/all.css'
    ],
    suppress_callback_exceptions=True
)
server = app.server
graph_config = {'displayModeBar': False, 'showTips': False}

API_VISITORS = "https://api.displayforce.ai/public/v1/stats/visitor/list"
API_DEVICES = "https://api.displayforce.ai/public/v1/device/list"
API_TOKEN = "4AUH-BX6H-G2RJ-G7PB"

# Cache otimizado
devices_cache = []
last_devices_update = None
CACHE_EXPIRATION = 300

# =======================================================
# FUNÇÕES UTILITÁRIAS OTIMIZADAS
# =======================================================
def api_post_with_backoff(url, headers, payload, timeout=10, max_retries=3, backoff_factor=2):
    """Chamada de API com retentativas e backoff exponencial."""
    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return None
            elif r.status_code in (429, 500, 502, 503, 504):
                wait = backoff_factor ** attempt
                print(f"[WARN] {url} -> tentativa {attempt+1}/{max_retries} status={r.status_code}, aguardando {wait}s")
                time.sleep(wait)
            else:
                print(f"[ERROR] {url} -> status {r.status_code}")
                r.raise_for_status()
        except requests.RequestException as e:
            wait = backoff_factor ** attempt
            print(f"[ERROR] {url} -> tentativa {attempt+1}/{max_retries} falhou: {e}")
            if attempt < max_retries - 1:
                time.sleep(wait)
    return None

def get_devices(force_update=False):
    """Busca lista de dispositivos com cache em memória."""
    global devices_cache, last_devices_update
    current_time = time.time()

    if not force_update and devices_cache and last_devices_update and (current_time - last_devices_update) < CACHE_EXPIRATION:
        return devices_cache

    headers = {"X-API-Token": API_TOKEN}
    payload = {"pagination": {"offset": 0, "limit": 100}}

    try:
        data = api_post_with_backoff(API_DEVICES, headers, payload, timeout=8, max_retries=2)
        if not data:
            return devices_cache if devices_cache else []

        devices = data.get("data", []) or []
        devices_cache = [{"id": str(d["id"]), "name": d.get("name") or f"Loja {d.get('id')}"} for d in devices]
        last_devices_update = time.time()
        return devices_cache
    except Exception as e:
        print(f"Erro ao buscar dispositivos: {e}")
        return devices_cache if devices_cache else []

def fetch_visitors_page(headers, payload, page_index, limit):
    """Busca uma página específica de visitantes."""
    offset = page_index * limit
    page_payload = {**payload, "pagination": {"offset": offset, "limit": limit}}

    page_data = api_post_with_backoff(API_VISITORS, headers, page_payload, timeout=10, max_retries=2)
    if not page_data:
        return []

    return page_data.get("payload", []) or page_data.get("data", []) or []

def get_visitors_all(start_date=None, end_date=None, selected_store=None):
    """Busca todos os visitantes de um período (usado para persistência em banco)."""
    if not start_date:
        start_date = datetime.now(brazil_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if not end_date:
        end_date = datetime.now(brazil_tz).replace(hour=23, minute=59, second=59, microsecond=999999)

    start_date = brazil_tz.localize(start_date) if start_date.tzinfo is None else start_date.astimezone(brazil_tz)
    end_date = brazil_tz.localize(end_date) if end_date.tzinfo is None else end_date.astimezone(brazil_tz)

    start_utc = start_date.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_date.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {"X-API-Token": API_TOKEN}
    limit = 100
    all_visitors = []

    base_payload = {
        "start": start_utc,
        "end": end_utc,
        "tracks": True,
        "pagination": {"offset": 0, "limit": limit}
    }

    data = api_post_with_backoff(API_VISITORS, headers, base_payload, timeout=8, max_retries=2)
    if not data:
        return []

    first_items = data.get("payload", []) or data.get("data", []) or []
    all_visitors.extend(first_items)

    total_reported = int((data.get("pagination", {}) or {}).get("total") or len(first_items))
    pages_count = (total_reported + limit - 1) // limit

    for page_index in range(1, pages_count):
        page_items = fetch_visitors_page(headers, base_payload, page_index, limit)
        all_visitors.extend(page_items)

    # Filtro por loja (dispositivo)
    if selected_store and selected_store != "all":
        sid = str(selected_store)

        def visitor_has_device(v):
            if "device_id" in v and str(v["device_id"]) == sid:
                return True
            if "device" in v and isinstance(v["device"], dict) and str(v["device"].get("id")) == sid:
                return True
            devices_field = v.get("devices") or v.get("device_ids") or []
            for d in devices_field:
                try:
                    if isinstance(d, dict) and str(d.get("id")) == sid:
                        return True
                    if str(d) == sid:
                        return True
                except Exception:
                    continue
            tracks = v.get("tracks") or []
            for t in tracks:
                try:
                    if str(t.get("device_id")) == sid:
                        return True
                except Exception:
                    continue
            return False

        all_visitors = [v for v in all_visitors if visitor_has_device(v)]

    return all_visitors

def get_visitors_fast(start_date=None, end_date=None, selected_store=None, sample_size=200):
    """
    Busca visitantes de forma otimizada para atualização de tela.
    Usa amostragem quando o volume é muito grande para não travar o dashboard.
    """
    if not start_date:
        start_date = datetime.now(brazil_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if not end_date:
        end_date = datetime.now(brazil_tz).replace(hour=23, minute=59, second=59, microsecond=999999)

    start_date = brazil_tz.localize(start_date) if start_date.tzinfo is None else start_date.astimezone(brazil_tz)
    end_date = brazil_tz.localize(end_date) if end_date.tzinfo is None else end_date.astimezone(brazil_tz)

    start_utc = start_date.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_date.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {"X-API-Token": API_TOKEN}
    limit = 100
    all_visitors = []

    base_payload = {
        "start": start_utc,
        "end": end_utc,
        "tracks": True,
        "pagination": {"offset": 0, "limit": limit}
    }

    start_time = time.time()
    data = api_post_with_backoff(API_VISITORS, headers, base_payload, timeout=8, max_retries=2)

    if not data:
        print(f"[API] Erro ao buscar visitantes")
        return [], 0

    first_items = data.get("payload", []) or data.get("data", []) or []
    all_visitors.extend(first_items)

    total_reported = int((data.get("pagination", {}) or {}).get("total") or len(first_items))

    print(f"[API] Total reportado: {total_reported} visitantes")

    # Se o volume é pequeno, busca tudo. Se for grande, usa amostra.
    if total_reported <= 500:
        pages_count = (total_reported + limit - 1) // limit
        print(f"[API] Buscando todos os {total_reported} visitantes em {pages_count} páginas...")

        for page_index in range(1, pages_count):
            page_items = fetch_visitors_page(headers, base_payload, page_index, limit)
            all_visitors.extend(page_items)

    else:
        print(f"[API] Volume grande ({total_reported}), usando amostra de {sample_size} visitantes...")

        sample_pages = min(5, (total_reported // limit) + 1)
        pages_to_fetch = [i for i in range(1, sample_pages)]

        if total_reported > limit * sample_pages:
            pages_to_fetch.extend([i for i in range((total_reported // limit) - 2, (total_reported // limit))])

        for page_index in pages_to_fetch:
            page_items = fetch_visitors_page(headers, base_payload, page_index, limit)
            all_visitors.extend(page_items)

            if len(all_visitors) >= sample_size:
                all_visitors = all_visitors[:sample_size]
                break

    # Filtro por loja (dispositivo)
    if selected_store and selected_store != "all":
        sid = str(selected_store)

        def visitor_has_device(v):
            if "device_id" in v and str(v["device_id"]) == sid:
                return True
            if "device" in v and isinstance(v["device"], dict) and str(v["device"].get("id")) == sid:
                return True
            devices_field = v.get("devices") or v.get("device_ids") or []
            for d in devices_field:
                try:
                    if isinstance(d, dict) and str(d.get("id")) == sid:
                        return True
                    if str(d) == sid:
                        return True
                except Exception:
                    continue
            tracks = v.get("tracks") or []
            for t in tracks:
                try:
                    if str(t.get("device_id")) == sid:
                        return True
                except Exception:
                    continue
            return False

        all_visitors = [v for v in all_visitors if visitor_has_device(v)]
        print(f"[FILTER] Filtrado por loja {sid}: {len(all_visitors)} visitantes")

    elapsed_time = time.time() - start_time
    print(f"[API] Busca concluída em {elapsed_time:.2f}s - {len(all_visitors)} visitantes processados")
    return all_visitors, total_reported

def process_visitor_data(visitors, total_reported=None):
    """
    Processa os visitantes e gera métricas agregadas.
    As contagens por horário passam a usar a amostra real, sem redistribuir pelo total_reported.
    """
    if not visitors:
        return empty_metrics()

    df = pd.DataFrame(visitors)

    # Garante que as colunas usadas existam
    for col in ["sex", "age", "start"]:
        if col not in df.columns:
            df[col] = pd.NA

    total_processed = len(df)

    # Fator de escala para métricas que desejam aproximar o total reportado
    scale_factor = 1.0
    if total_reported and total_reported > total_processed:
        scale_factor = total_reported / total_processed
        print(f"[SCALE] Escalonando dados: {total_processed} -> {total_reported} (fator: {scale_factor:.2f})")

    # Total de visitantes apresentado nos cards
    total_visitors = int(total_reported) if (total_reported is not None) else total_processed

    # Média de idade (inteiro)
    try:
        ages = pd.to_numeric(df["age"], errors="coerce").dropna()
        avg_age = int(round(float(ages.mean()))) if not ages.empty else 0
    except Exception:
        avg_age = 0

    # Normalização de gênero
    def normalize_sex(val):
        s = str(val).strip().lower()
        if s in {"1", "male", "m"}:
            return "male"
        if s in {"2", "female", "f"}:
            return "female"
        return None

    df["sex_norm"] = df["sex"].apply(normalize_sex)
    men_raw = int(df[df["sex_norm"] == "male"].shape[0])
    women_raw = int(df[df["sex_norm"] == "female"].shape[0])
    known_raw = men_raw + women_raw

    if known_raw > 0:
        ratio_m = men_raw / known_raw
        total_men = int(round(total_visitors * ratio_m))
        total_women = total_visitors - total_men
    else:
        total_men = 0
        total_women = 0

    # Recalcula média de idade caso o bloco anterior falhe
    try:
        ages = pd.to_numeric(df["age"], errors="coerce").dropna()
        avg_age = int(round(float(ages.mean()))) if not ages.empty else 0
    except Exception:
        avg_age = 0

    weekday_counts = {}
    hourly_data = {}
    hourly_gender_visits = {"male": {}, "female": {}}

    # Helper para alocação proporcional (usado em dias da semana)
    def allocate_proportional(total, raw_counts, keys):
        base = {k: 0 for k in keys}
        s = sum(raw_counts.values())
        if s <= 0 or total <= 0:
            return base
        quotas = {k: (raw_counts.get(k, 0) / s) * total for k in keys}
        ints = {k: int(quotas[k]) for k in keys}
        remainder = total - sum(ints.values())
        # Distribui o resto pelas maiores frações
        order = sorted(keys, key=lambda k: quotas[k] - ints[k], reverse=True)
        for i in range(remainder):
            ints[order[i]] += 1
        return ints

    try:
        # Conversão de datas para timezone Brasil
        df["start_dt_utc"] = pd.to_datetime(df["start"], errors="coerce", utc=True)
        df = df.dropna(subset=["start_dt_utc"])

        df["start_dt_brt"] = df["start_dt_utc"].dt.tz_convert(brazil_tz)

        # Dias da semana (aqui ainda usa proporcional ao total_visitors)
        df["weekday_en"] = df["start_dt_brt"].dt.day_name()
        weekday_raw = df["weekday_en"].value_counts().to_dict()
        order_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekday_counts = allocate_proportional(total_visitors, weekday_raw, order_en)

        # Horários (total) – agora baseado somente na contagem real da amostra,
        # sem escalonar para o total_reported para não distorcer o gráfico.
        df["hour_brt"] = df["start_dt_brt"].dt.hour
        hourly_counts_raw = df["hour_brt"].value_counts().to_dict()
        all_hours = [f"{h:02d}:00" for h in range(24)]
        hourly_data = {
            f"{h:02d}:00": int(hourly_counts_raw.get(h, 0))
            for h in range(24)
        }

        # Horários por gênero – usa a mesma base de contagem real
        male_df = df[df["sex_norm"] == "male"]
        female_df = df[df["sex_norm"] == "female"]

        male_hourly_raw = {
            f"{int(h):02d}:00": int(c) for h, c in male_df["hour_brt"].value_counts().items()
        }
        female_hourly_raw = {
            f"{int(h):02d}:00": int(c) for h, c in female_df["hour_brt"].value_counts().items()
        }

        hourly_gender_visits = {"male": {}, "female": {}}

        for h in all_hours:
            total_h = int(hourly_data.get(h, 0))
            raw_m = int(male_hourly_raw.get(h, 0))
            raw_f = int(female_hourly_raw.get(h, 0))
            raw_sum = raw_m + raw_f

            if raw_sum > 0:
                male_ratio_h = raw_m / raw_sum
            else:
                # Se não houver informação de gênero naquele horário, aplica razão diária
                male_ratio_h = (total_men / total_visitors) if total_visitors > 0 else 0.0

            male_h = int(round(total_h * male_ratio_h))
            female_h = total_h - male_h

            hourly_gender_visits["male"][h] = male_h
            hourly_gender_visits["female"][h] = female_h

    except Exception as e:
        print(f"Erro ao processar datas: {e}")
        weekday_counts = {}
        hourly_data = {}
        hourly_gender_visits = {"male": {}, "female": {}}

    # Distribuição por faixa etária
    age_ranges = {"18-25": 0, "26-35": 0, "36-45": 0, "46-60": 0, "60+": 0}

    try:
        age_counts = {}
        for a in pd.to_numeric(df["age"], errors="coerce").dropna().astype(int):
            if a < 18:
                key = "18-25"
            elif a <= 25:
                key = "18-25"
            elif a <= 35:
                key = "26-35"
            elif a <= 45:
                key = "36-45"
            elif a <= 60:
                key = "46-60"
            else:
                key = "60+"

            age_counts[key] = age_counts.get(key, 0) + 1

        # Aplica escala apenas nas contagens de faixa etária, se houver necessidade
        if scale_factor > 1.0:
            age_ranges = {key: int(count * scale_factor) for key, count in age_counts.items()}
        else:
            age_ranges = {key: int(count) for key, count in age_counts.items()}

    except Exception:
        pass

    gender_distribution = {"male": total_men, "female": total_women}

    return {
        "total_visitors": total_visitors,
        "total_men": total_men,
        "total_women": total_women,
        "avg_age": avg_age,
        "weekday_visits": weekday_counts,
        "gender_distribution": gender_distribution,
        "age_distribution": age_ranges,
        "hourly_visits": hourly_data,
        "hourly_gender_visits": hourly_gender_visits
    }

def empty_metrics():
    """Métrica vazia padrão para períodos sem dados."""
    return {
        "total_visitors": 0, "total_men": 0, "total_women": 0, "avg_age": 0, "weekday_visits": {},
        "gender_distribution": {"male": 0, "female": 0},
        "age_distribution": {"18-25": 0, "26-35": 0, "36-45": 0, "46-60": 0, "60+": 0},
        "hourly_visits": {},
        "hourly_gender_visits": {"male": {}, "female": {}}
    }

def create_hourly_flow_chart(hourly_data):
    """Cria gráfico de fluxo de pessoas por horário."""
    if not hourly_data:
        fig = go.Figure()
        fig.add_annotation(
            text="Nenhum dado disponível para o período selecionado",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            xanchor='center',
            yanchor='middle',
            showarrow=False
        )
        fig.update_layout(
            xaxis_title="Horário",
            yaxis_title="Número de Visitantes",
            margin=dict(l=20, r=20, t=40, b=20),
            plot_bgcolor='white'
        )
        return fig

    all_hours = [f"{h:02d}:00" for h in range(24)]
    counts = [hourly_data.get(hour, 0) for hour in all_hours]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=all_hours,
        y=counts,
        fill='tozeroy',
        fillcolor='rgba(0, 74, 173, 0.3)',
        line=dict(color='#004AAD', width=3),
        mode='lines',
        name='Visitantes',
        hovertemplate='<b>%{x}</b><br>%{y} visitantes<extra></extra>'
    ))

    max_count = max(counts) if counts else 0
    if max_count > 0:
        max_index = counts.index(max_count)
        fig.add_trace(go.Scatter(
            x=[all_hours[max_index]],
            y=[max_count],
            mode='markers+text',
            marker=dict(size=12, color='#FF6B00'),
            text=[f'Pico: {max_count}'],
            textposition='top center',
            name='Pico do Dia',
            hovertemplate='<b>Pico: %{x}</b><br>%{y} visitantes<extra></extra>'
        ))

    fig.update_layout(
        title="Fluxo de Visitantes por Horário (Horário de Brasília)",
        xaxis_title="Horário do Dia",
        yaxis_title="Número de Visitantes",
        plot_bgcolor='white',
        paper_bgcolor='white',
        font=dict(color='#004AAD'),
        margin=dict(l=20, r=20, t=50, b=20),
        xaxis=dict(
            tickmode='array',
            tickvals=[f"{h:02d}:00" for h in range(0, 24, 3)],
            gridcolor='#f0f0f0'
        ),
        yaxis=dict(
            gridcolor='#f0f0f0',
            zerolinecolor='#f0f0f0'
        ),
        showlegend=False,
        hovermode='x unified'
    )

    return fig

def create_gender_hourly_flow_chart(hourly_gender_data):
    """Cria gráfico de fluxo por horário segmentado por gênero (intervalos de 1h)."""
    if not hourly_gender_data or (not hourly_gender_data.get("male") and not hourly_gender_data.get("female")):
        fig = go.Figure()
        fig.add_annotation(
            text="Nenhum dado disponível para o período selecionado",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            xanchor='center',
            yanchor='middle',
            showarrow=False
        )
        fig.update_layout(
            xaxis_title="Horário",
            yaxis_title="Número de Visitantes",
            margin=dict(l=20, r=20, t=40, b=20),
            plot_bgcolor='white',
            paper_bgcolor='white',
            hovermode='x unified'
        )
        return fig

    labels = [f"{h:02d}:00 — {((h+1) % 24):02d}:00" for h in range(24)]
    hours_keys = [f"{h:02d}:00" for h in range(24)]

    male_counts = [int(hourly_gender_data.get("male", {}).get(k, 0)) for k in hours_keys]
    female_counts = [int(hourly_gender_data.get("female", {}).get(k, 0)) for k in hours_keys]

    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=labels, y=female_counts, name="Feminino",
        line=dict(color="#DC3545", width=3),
        fill='tozeroy', fillcolor='rgba(220, 53, 69, 0.15)',
        mode='lines',
        hovertemplate='<b>%{x}</b><br>• Feminino: %{y}<extra></extra>'
    ))

    fig.add_trace(go.Scatter(
        x=labels, y=male_counts, name="Masculino",
        line=dict(color="#004AAD", width=3),
        fill='tozeroy', fillcolor='rgba(0, 74, 173, 0.15)',
        mode='lines',
        hovertemplate='<b>%{x}</b><br>• Masculino: %{y}<extra></extra>'
    ))

    fig.update_layout(
        title="Fluxo por Gênero (Horário de Brasília)",
        xaxis_title="Horário do Dia",
        yaxis_title="Número de Visitantes",
        plot_bgcolor='white', paper_bgcolor='white',
        margin=dict(l=20, r=20, t=50, b=20),
        xaxis=dict(tickmode='array', tickvals=[labels[h] for h in range(0, 24, 3)], gridcolor='#f0f0f0'),
        yaxis=dict(gridcolor='#f0f0f0', zerolinecolor='#f0f0f0'),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1.0),
        hovermode='x unified'
    )
    return fig

# =======================================================
# COMPONENTES DE LAYOUT
# =======================================================
MENU_PANEL_BASE_STYLE = {
    "transform": "translateX(-100%)", "position": "fixed", "top": "72px", "left": "0",
    "height": "calc(100% - 72px)", "width": "260px", "backgroundColor": "#004AAD", "zIndex": "1050",
    "overflowY": "auto", "boxShadow": "2px 0 12px rgba(0,0,0,0.2)", "transition": "transform 0.25s ease",
    "padding": "16px", "boxSizing": "border-box"
}

def create_side_menu():
    return html.Div([
        html.Div([
            html.H5("Menu", className="text-white mb-3"),
            dbc.Nav([
                dbc.NavLink([html.I(className="fas fa-chart-line me-2"), "Dashboard"], href="/", active="exact", className="text-white"),
                dbc.NavLink([html.I(className="fas fa-users me-2"), "Ver lista completa"], href="/visitors", active="exact", className="text-white"),
            ], vertical=True)
        ], id="side-menu-panel", style=MENU_PANEL_BASE_STYLE)
    ], className="side-menu-container")

def metric_card(title, value_id, icon_class, color_class):
    return dbc.Col([
        dbc.Card([
            dbc.CardBody([
                html.Div([
                    html.H3(title, className="text-white h6 mb-2"),
                    html.H2("", id=value_id, className="text-white mb-0")
                ]),
                html.Div([
                    html.I(className=f"{icon_class} fa-2x text-white")
                ],
                style={"position": "absolute", "right": "20px", "top": "50%", "transform": "translateY(-50%)"})
            ], style={"height": "100px", "position": "relative"})
        ], style={
            "backgroundColor": color_class,
            "border": "none"
        })
    ], width=3)

def create_navbar():
    return html.Div([
        dbc.Navbar([
            dbc.Container([
                dbc.Row([
                    dbc.Col([
                        dbc.Button(
                            html.I(className="fas fa-bars"),
                            id="menu-toggle",
                            className="menu-button me-2",
                            n_clicks=0,
                            style={"border": "none", "background": "transparent"}
                        ),
                        html.Img(src="/assets/img/logo.png", height="50px", className="me-2"),
                        html.Div([
                            html.H4("Assaí Atacadista", className="text-white mb-0"),
                            html.P("Dashboard de Análise", className="text-white-50 mb-0")
                        ])
                    ], className="d-flex align-items-center"),
                    dbc.Col([
                        dbc.Button([
                            html.I(className="fas fa-sign-out-alt me-2"),
                            html.Span("Sair")
                        ], href="/login", className="btn-sair float-end")
                    ])
                ], align="center", className="flex-nowrap w-100")
            ], fluid=True)
        ], color="#004AAD", dark=True, className="py-2 navbar",
           style={"backgroundColor": "#004AAD", "margin": "0", "border": "none"})
    ], className="w-100 mb-0")

def create_filters(devices_cache):
    return dbc.Card([
        dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.Div([
                        html.I(className="fas fa-store me-2"),
                        html.Label("Loja", className="fw-bold me-2"),
                        dcc.Dropdown(
                            id='store-selector',
                            options=[{'label': 'Todas as Lojas', 'value': 'all'}] +
                                   [{'label': d['name'], 'value': d['id']} for d in devices_cache],
                            value='all', className='filter-dropdown'
                        )
                    ], className="filter-group")
                ], width=6),
                dbc.Col([
                    html.Div([
                        html.I(className="far fa-calendar-alt me-2"),
                        html.Label("Período", className="fw-bold me-2"),
                        html.Div([
                            dcc.DatePickerRange(
                                id='date-picker',
                                start_date=datetime.now(brazil_tz).replace(hour=0, minute=0, second=0, microsecond=0),
                                end_date=datetime.now(brazil_tz).replace(hour=23, minute=59, second=59, microsecond=999999),
                                display_format='DD/MM/YYYY', first_day_of_week=0, className="date-picker"
                            ),
                            dbc.Button("Aplicar Filtros", id="apply-filters", color="primary", className="ms-3")
                        ], className="d-flex align-items-center")
                    ], className="filter-group")
                ], width=6)
            ])
        ])
    ], className="mb-1", style={"marginTop": "72px"})

def create_chart_card(title, chart_id):
    return dbc.Card([
        dbc.CardHeader([
            html.H5(title, style={"color": "#004AAD", "fontWeight": "500", "margin": "0"})
        ]),
        dbc.CardBody([
            dcc.Graph(id=chart_id, config=graph_config)
        ])
    ])

def dashboard_layout(devices_cache, side_menu):
    return html.Div([
        side_menu,
        create_navbar(),
        dbc.Container([
            dbc.Row([
                metric_card("Total de Visitantes", "total-visitors", "fas fa-users", "#004AAD"),
                metric_card("Total de Homens", "total-men", "fas fa-male", "#DC3545"),
                metric_card("Total de Mulheres", "total-women", "fas fa-female", "#FFC107"),
                metric_card("Média de Idade", "avg-age", "fas fa-user-clock", "#FF6B00")
            ], className="mb-4"),
            dbc.Row([
                dbc.Col([create_chart_card("Visitas por Dia da Semana", "weekly-visits-chart")], width=6),
                dbc.Col([create_chart_card("Distribuição por Gênero", "gender-distribution-chart")], width=6)
            ], className="mb-4"),
            dbc.Row([
                dbc.Col([create_chart_card("Distribuição por Faixa Etária", "age-distribution-chart")], width=6),
                dbc.Col([create_chart_card("Fluxo de Visitantes por Horário", "hourly-flow-chart")], width=6)
            ], className="mb-4"),
            dbc.Row([
                dbc.Col([create_chart_card("Gênero", "gender-hourly-flow-chart")], width=12)
            ], className="mb-4")
        ], fluid=True, className="p-4")
    ])

def visitors_layout(devices_cache, side_menu):
    """Layout simples para a página de lista de visitantes."""
    return html.Div([
        side_menu,
        create_navbar(),
        dbc.Container([
            dbc.Row([
                dbc.Col([
                    html.H2("Lista Completa de Visitantes"),
                    html.P("Funcionalidade em desenvolvimento...", className="text-muted")
                ])
            ])
        ], fluid=True, className="p-4")
    ])

def login_layout():
    """Layout simples da página de login."""
    return html.Div([
        dbc.Container([
            dbc.Row([
                dbc.Col([
                    html.H2("Login"),
                    html.P("Página de login em desenvolvimento...", className="text-muted")
                ], width=6)
            ], className="justify-content-center")
        ], fluid=True, className="p-4")
    ])

# =======================================================
# LAYOUT PRINCIPAL
# =======================================================
devices_cache = get_devices(force_update=True)

app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    dcc.Store(id='dashboard-data'),
    dcc.Store(id='selected-store'),
    dcc.Store(id='current-page', data=0),
    dcc.Store(id='shared-devices', data=devices_cache),
    dcc.Store(id='menu-state', data={'open': False}),
    dcc.Interval(id='interval-component', interval=30*1000, n_intervals=0),

    html.Div(
        id='top-filters-container',
        children=create_filters(devices_cache),
        style={"display": "block"}
    ),

    html.Div(id='page-content'),
    html.Div(id="backdrop", style={"display": "none"}),

    # Botão flutuante do assistente
    html.Button(
        html.I(className="fas fa-robot"),
        id="ai-fab",
        className="ai-fab",
        title="Assistente IA - Pergunte sobre os dados"
    ),

    # Painel de chat do assistente
    dbc.Offcanvas(
        [
            html.Div([
                html.H5("Assistente IA", className="mb-2", style={"color": "#004AAD"}),
                html.P("Pergunte sobre os dados do dashboard", className="text-muted small mb-3"),
            ]),
            html.Div(
                id="ai-chat-messages",
                style={
                    "height": "300px",
                    "overflowY": "auto",
                    "border": "1px solid #e0e0e0",
                    "borderRadius": "8px",
                    "padding": "10px",
                    "backgroundColor": "#f8f9fa",
                    "marginBottom": "10px"
                }
            ),
            dbc.InputGroup([
                dbc.Input(
                    id="ai-chat-input",
                    placeholder="Digite sua pergunta...",
                    type="text",
                    size="sm",
                    style={"borderRadius": "20px 0 0 20px", "fontSize": "14px"}
                ),
                dbc.Button(
                    html.I(className="fas fa-paper-plane"),
                    id="ai-chat-send",
                    color="primary",
                    size="sm",
                    style={"borderRadius": "0 20px 20px 0"}
                )
            ], size="sm")
        ],
        id="ai-offcanvas",
        title="",
        is_open=False,
        placement="end",
        style={"width": "350px"}
    )
], className='app-container')

app.validation_layout = html.Div([
    app.layout,
    dashboard_layout(devices_cache, create_side_menu()),
    visitors_layout(devices_cache, create_side_menu()),
    login_layout()
])

# =======================================================
# CALLBACKS PRINCIPAIS
# =======================================================
@app.callback(
    Output('menu-state', 'data', allow_duplicate=True),
    Input('menu-toggle', 'n_clicks'),
    State('menu-state', 'data'),
    prevent_initial_call=True
)
def toggle_menu(menu_clicks, state):
    """Abre/fecha o menu lateral."""
    if not menu_clicks:
        raise dash.exceptions.PreventUpdate
    is_open = bool((state or {}).get('open', False))
    return {'open': not is_open}

@app.callback(
    Output('menu-state', 'data', allow_duplicate=True),
    Input('url', 'pathname'),
    prevent_initial_call=True
)
def close_menu_on_navigation(_pathname):
    """Fecha o menu ao navegar para outra página."""
    return {'open': False}

@app.callback(
    Output('page-content', 'children'),
    [Input('url', 'pathname')]
)
def display_page(pathname):
    """Renderiza o conteúdo de acordo com a rota."""
    side_menu = create_side_menu()
    if pathname == '/visitors':
        return visitors_layout(devices_cache, side_menu)
    if pathname in ('/login', '/pages/login'):
        return login_layout()
    return dashboard_layout(devices_cache, side_menu)

@app.callback(
    Output('top-filters-container', 'style'),
    Input('url', 'pathname'),
    prevent_initial_call=False
)
def toggle_filters_visibility(pathname):
    """Mostra os filtros apenas na página principal do dashboard."""
    if pathname in ('/', '/dashboard', ''):
        return {"display": "block"}
    return {"display": "none"}

@app.callback(
    [
        Output('total-visitors', 'children'),
        Output('total-men', 'children'),
        Output('total-women', 'children'),
        Output('avg-age', 'children'),
        Output('weekly-visits-chart', 'figure'),
        Output('gender-distribution-chart', 'figure'),
        Output('age-distribution-chart', 'figure'),
        Output('hourly-flow-chart', 'figure'),
        Output('gender-hourly-flow-chart', 'figure'),
        Output('dashboard-data', 'data')
    ],
    [
        Input('store-selector', 'value'),
        Input('date-picker', 'start_date'),
        Input('date-picker', 'end_date'),
        Input('interval-component', 'n_intervals'),
        Input('url', 'pathname'),
        Input('apply-filters', 'n_clicks')
    ]
)
def update_dashboard_metrics_and_charts(selected_store, start_date, end_date, n_intervals, pathname, apply_clicks):
    """
    Atualiza cards e gráficos do dashboard.
    Usa get_visitors_fast para a tela e dispara persistência completa em background.
    """
    if pathname not in ('/', '/dashboard', ''):
        raise dash.exceptions.PreventUpdate

    start_time = time.time()

    try:
        if not start_date or not end_date:
            start_dt = datetime.now(brazil_tz).replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt = datetime.now(brazil_tz).replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            try:
                start_dt = datetime.fromisoformat(str(start_date))
                end_dt = datetime.fromisoformat(str(end_date))
            except ValueError:
                start_dt = datetime.strptime(str(start_date), "%Y-%m-%d")
                end_dt = datetime.strptime(str(end_date), "%Y-%m-%d")

            start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            start_dt = brazil_tz.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(brazil_tz)
            end_dt = brazil_tz.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(brazil_tz)
    except Exception as e:
        print(f"Erro ao processar datas: {e}")
        start_dt = datetime.now(brazil_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = datetime.now(brazil_tz).replace(hour=23, minute=59, second=59, microsecond=999999)

    store_key = selected_store or "all"

    try:
        print(f"[UI] Buscando dados otimizados...")
        all_visitors, api_total = get_visitors_fast(start_dt, end_dt, store_key)
        metrics = process_visitor_data(all_visitors, api_total)

        # Persistência em banco em uma thread separada, para não travar o dashboard
        try:
            if all_visitors:
                def _persist(s_key, s_dt, e_dt):
                    try:
                        full_visitors = get_visitors_all(s_dt, e_dt, s_key)
                        if full_visitors:
                            upsert_daily_from_visitors(full_visitors, s_key)
                    except Exception as e:
                        print(f"[ERROR] Falha na persistência: {e}")

                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                executor.submit(_persist, store_key, start_dt, end_dt)
        except Exception as e:
            print(f"[WARN] Falha ao disparar persistência no DB: {e}")

        result = format_output(metrics)

    except Exception as e:
        print(f"[UI] Falha na API: {e}. Usando fallback.")
        try:
            # Exemplo: aqui entraria a leitura de agregados do banco
            # agg = get_aggregated_stats(store_key, start_dt.date(), end_dt.date())
            agg = empty_metrics()
        except Exception as e2:
            print(f"[ERROR] Falha ao buscar agregados do DB: {e2}")
            agg = empty_metrics()
        result = format_output_from_db(agg)

    elapsed_time = time.time() - start_time
    print(f"[PERFORMANCE] Dashboard atualizado em {elapsed_time:.2f} segundos")

    return result

def format_output(metrics):
    """Formata a saída para os componentes do dashboard a partir das métricas em memória."""
    try:
        days_map = {
            "Monday": "Seg", "Tuesday": "Ter", "Wednesday": "Qua",
            "Thursday": "Qui", "Friday": "Sex", "Saturday": "Sáb", "Sunday": "Dom"
        }
        order_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        ordered_pt = [days_map.get(d, d) for d in order_en]
        ordered_vals = [int(metrics["weekday_visits"].get(d, 0)) for d in order_en]

        weekly_fig = px.bar(x=ordered_pt, y=ordered_vals, labels={'x': 'Dia da semana', 'y': 'Visitas'})
        weekly_fig.update_traces(marker_color='#004AAD')
        weekly_fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis_title=None,
            yaxis_title=None,
            plot_bgcolor='white',
            paper_bgcolor='white'
        )

        total = int(metrics.get("total_visitors", 0) or 0)
        men_raw = int(metrics.get("total_men", 0) or 0)
        women_raw = int(metrics.get("total_women", 0) or 0)

        if total > 0 and (men_raw + women_raw) != total and (men_raw + women_raw) > 0:
            ratio_m = men_raw / (men_raw + women_raw)
            men_display = int(round(total * ratio_m))
            women_display = total - men_display
        else:
            men_display = men_raw
            women_display = women_raw

        gender_fig = px.pie(
            names=["Masculino", "Feminino"],
            values=[men_display, women_display],
            color_discrete_sequence=["#004AAD", "#DC3545"]
        )
        gender_fig.update_layout(margin=dict(l=20, r=20, t=20, b=20))

        age_order = ["18-25", "26-35", "36-45", "46-60", "60+"]
        age_vals = [int(metrics["age_distribution"].get(cat, 0)) for cat in age_order]
        age_fig = px.bar(x=age_order, y=age_vals, labels={'x': 'Faixa etária', 'y': 'Visitas'})
        age_fig.update_traces(marker_color='#004AAD')
        age_fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            plot_bgcolor='white',
            paper_bgcolor='white'
        )

        hourly_fig = create_hourly_flow_chart(metrics.get("hourly_visits", {}))
        gender_hourly_fig = create_gender_hourly_flow_chart(metrics.get("hourly_gender_visits", {"male": {}, "female": {}}))

        avg_age_display = int(round(float(metrics.get("avg_age", 0))))

        return (
            str(total),
            str(men_display),
            str(women_display),
            f"{avg_age_display} anos",
            weekly_fig,
            gender_fig,
            age_fig,
            hourly_fig,
            gender_hourly_fig,
            metrics
        )
    except Exception as e:
        print(f"Erro no format_output: {e}")
        return fallback_output()

def format_output_from_db(agg):
    """
    Formata saída quando os dados vêm do banco.
    Agora o gráfico por horário usa hourly_visits do agregado, se existir.
    """
    try:
        days_map = {
            "Monday": "Seg", "Tuesday": "Ter", "Wednesday": "Qua",
            "Thursday": "Qui", "Friday": "Sex", "Saturday": "Sáb", "Sunday": "Dom"
        }
        order_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekday_counts = agg.get("weekday_visits", {}) or {}
        ordered_pt = [days_map.get(d, d) for d in order_en]
        ordered_vals = [int(weekday_counts.get(d, 0)) for d in order_en]

        weekly_fig = px.bar(x=ordered_pt, y=ordered_vals, labels={'x': 'Dia da semana', 'y': 'Visitas'})
        weekly_fig.update_traces(marker_color='#004AAD')
        weekly_fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            xaxis_title=None,
            yaxis_title=None,
            plot_bgcolor='white',
            paper_bgcolor='white'
        )

        total = int(agg.get("total_visitors", 0) or 0)
        men_raw = int(agg.get("male", 0) or 0)
        women_raw = int(agg.get("female", 0) or 0)

        if total > 0 and (men_raw + women_raw) != total and (men_raw + women_raw) > 0:
            ratio_m = men_raw / (men_raw + women_raw)
            men_display = int(round(total * ratio_m))
            women_display = total - men_display
        else:
            men_display = men_raw
            women_display = women_raw

        gender_fig = px.pie(
            names=["Masculino", "Feminino"],
            values=[men_display, women_display],
            color_discrete_sequence=["#004AAD", "#DC3545"]
        )
        gender_fig.update_layout(margin=dict(l=20, r=20, t=20, b=20))

        age_order = ["18-25", "26-35", "36-45", "46-60", "60+"]
        age = agg.get("age_distribution", {}) or {}
        age_vals = [int(age.get(cat, 0)) for cat in age_order]
        age_fig = px.bar(x=age_order, y=age_vals, labels={'x': 'Faixa etária', 'y': 'Visitas'})
        age_fig.update_traces(marker_color='#004AAD')
        age_fig.update_layout(
            margin=dict(l=20, r=20, t=20, b=20),
            plot_bgcolor='white',
            paper_bgcolor='white'
        )

        # Ajuste importante: usa hourly_visits do agregado, se existir.
        hourly_fig = create_hourly_flow_chart(agg.get("hourly_visits", {}))
        gender_hourly_fig = create_gender_hourly_flow_chart(
            agg.get("hourly_gender_visits", {"male": {}, "female": {}})
        )

        avg_age_int = int(round(float(agg.get("avg_age", 0.0))))
        return (
            str(total),
            str(men_display),
            str(women_display),
            f"{avg_age_int} anos",
            weekly_fig,
            gender_fig,
            age_fig,
            hourly_fig,
            gender_hourly_fig,
            agg
        )
    except Exception as e:
        print(f"Erro no format_output_from_db: {e}")
        return fallback_output()

def fallback_output():
    """Saída padrão usada quando não há dados nem da API nem do banco."""
    empty_fig = go.Figure()
    empty_fig.add_annotation(
        text="Nenhum dado disponível",
        x=0.5, y=0.5,
        xref="paper", yref="paper",
        showarrow=False
    )
    empty_fig.update_layout(plot_bgcolor='white', paper_bgcolor='white')

    empty_metrics = {
        "total_visitors": 0, "total_men": 0, "total_women": 0, "avg_age": 0
    }

    return ("0", "0", "0", "0 anos", empty_fig, empty_fig, empty_fig, empty_fig, empty_fig, empty_metrics)

@app.callback(
    Output('side-menu-panel', 'style'),
    Output('backdrop', 'style'),
    Input('menu-state', 'data'),
    prevent_initial_call=True
)
def apply_menu_styles(state):
    """Atualiza estilos do menu lateral e do backdrop conforme o estado aberto/fechado."""
    base = MENU_PANEL_BASE_STYLE.copy()
    open_state = bool((state or {}).get('open', False))

    if open_state:
        base['transform'] = 'translateX(0)'
        backdrop = {
            "display": "block", "position": "fixed", "top": "0", "left": "0", "width": "100%", "height": "100%",
            "background": "rgba(0,0,0,0.35)", "zIndex": "1040", "pointerEvents": "none"
        }
    else:
        base['transform'] = 'translateX(-100%)'
        backdrop = {"display": "none"}

    return base, backdrop

# =======================================================
# CALLBACKS DO CHAT
# =======================================================
@app.callback(
    Output("ai-offcanvas", "is_open"),
    Input("ai-fab", "n_clicks"),
    State("ai-offcanvas", "is_open"),
    prevent_initial_call=True
)
def toggle_ai_chat(n_clicks, is_open):
    """Abre/fecha o painel do assistente de dados."""
    return not is_open

def generate_ai_response(question, metrics):
    """Gera resposta baseada nas métricas atuais do dashboard."""
    if not metrics or metrics.get("total_visitors", 0) == 0:
        return "📊 Não há dados disponíveis no momento. Por favor, aplique os filtros primeiro para carregar as informações."
    
    question_lower = question.lower().strip()
    total = metrics.get("total_visitors", 0)
    men = metrics.get("total_men", 0)
    women = metrics.get("total_women", 0)
    avg_age = metrics.get("avg_age", 0)
    weekdays = metrics.get("weekday_visits", {})
    hourly = metrics.get("hourly_visits", {})
    ages = metrics.get("age_distribution", {})
    
    days_map = {
        "Monday": "Segunda-feira", "Tuesday": "Terça-feira", "Wednesday": "Quarta-feira",
        "Thursday": "Quinta-feira", "Friday": "Sexta-feira", "Saturday": "Sábado", "Sunday": "Domingo"
    }
    
    if any(word in question_lower for word in ["total", "visitantes", "quantos", "pessoas", "quantidade"]):
        return f"👥 **Total de visitantes:** {total} pessoas"
    
    elif any(word in question_lower for word in ["homem", "homens", "masculino"]):
        percent = (men / total * 100) if total > 0 else 0
        return f"👨 **Homens:** {men} pessoas ({percent:.1f}% do total)"
    
    elif any(word in question_lower for word in ["mulher", "mulheres", "feminino"]):
        percent = (women / total * 100) if total > 0 else 0
        return f"👩 **Mulheres:** {women} pessoas ({percent:.1f}% do total)"
    
    elif any(word in question_lower for word in ["idade", "média", "anos", "idade média"]):
        return f"🎂 **Média de idade:** {avg_age} anos"
    
    elif any(word in question_lower for word in ["gênero", "gêneros", "distribuição", "proporção"]):
        men_pct = (men / total * 100) if total > 0 else 0
        women_pct = (women / total * 100) if total > 0 else 0
        return f"⚧️ **Distribuição de gênero:**\n- Homens: {men} ({men_pct:.1f}%)\n- Mulheres: {women} ({women_pct:.1f}%)"
    
    elif any(word in question_lower for word in ["semana", "dia", "dias", "movimento"]):
        if weekdays:
            busiest_day = max(weekdays.items(), key=lambda x: x[1])
            slowest_day = min(weekdays.items(), key=lambda x: x[1])
            busiest_pt = days_map.get(busiest_day[0], busiest_day[0])
            slowest_pt = days_map.get(slowest_day[0], slowest_day[0])
            return f"📅 **Movimento por dia:**\n- Maior movimento: {busiest_pt} ({busiest_day[1]} visitantes)\n- Menor movimento: {slowest_pt} ({slowest_day[1]} visitantes)"
        else:
            return "📅 Não há dados de dias da semana disponíveis"
    
    elif any(word in question_lower for word in ["horário", "hora", "pico", "fluxo"]):
        if hourly:
            peak_hour = max(hourly.items(), key=lambda x: x[1])
            return f"⏰ **Horário de pico:** {peak_hour[0]}h com {peak_hour[1]} visitantes"
        else:
            return "⏰ Não há dados de horários disponíveis"
    
    elif any(word in question_lower for word in ["faixa etária", "idade", "jovem", "idoso", "adulto"]):
        if ages:
            dominant_age = max(ages.items(), key=lambda x: x[1])
            dominant_pct = (dominant_age[1] / total * 100) if total > 0 else 0
            return f"👥 **Faixa etária predominante:** {dominant_age[0]} anos com {dominant_pct:.1f}% dos visitantes"
        else:
            return "👥 Não há dados de faixa etária disponíveis"
    
    elif any(word in question_lower for word in ["resumo", "resumir", "resuma", "dados"]):
        men_pct = (men / total * 100) if total > 0 else 0
        women_pct = (women / total * 100) if total > 0 else 0
        return f"""📊 **Resumo dos dados:**
• Total: {total} visitantes
• Homens: {men} ({men_pct:.1f}%)
• Mulheres: {women} ({women_pct:.1f}%)
• Média de idade: {avg_age} anos

Pode perguntar sobre total, gênero, idade, dias da semana, horários ou faixas etárias."""
    
    else:
        return """🤖 **Assistente de Dados**
        
Posso ajudar com:

• Total de visitantes
• Distribuição por gênero
• Média de idade
• Dias da semana com mais movimento
• Horários de pico
• Faixas etárias predominantes

Exemplos:
"Quantos visitantes hoje?"
"Qual a distribuição por gênero?"
"Qual horário de pico?\""""

@app.callback(
    Output("ai-chat-messages", "children"),
    Output("ai-chat-input", "value"),
    Input("ai-chat-send", "n_clicks"),
    State("ai-chat-input", "value"),
    State("ai-chat-messages", "children"),
    State("dashboard-data", "data"),
    prevent_initial_call=True
)
def handle_ai_message(n_clicks, user_input, current_messages, metrics):
    """Gerencia o histórico de mensagens do chat do assistente."""
    if not user_input or not user_input.strip():
        return current_messages or [], ""
    
    if not current_messages:
        welcome_msg = html.Div([
            html.Div([
                html.Small("Assistente", className="text-muted"),
                html.P(
                    "Olá! Sou seu assistente de dados. Posso ajudar com informações sobre visitantes, gênero, idade e muito mais. Como posso ajudar?",
                    className="mb-1 p-2 bg-light rounded",
                    style={"maxWidth": "85%", "fontSize": "14px"}
                )
            ], className="text-start")
        ], className="mb-2")
        current_messages = [welcome_msg]
    
    user_message = html.Div([
        html.Div([
            html.Small("Você", className="text-muted", style={"fontSize": "12px"}),
            html.P(
                user_input,
                className="mb-1 p-2 bg-primary text-white rounded",
                style={"maxWidth": "85%", "marginLeft": "auto", "fontSize": "14px"}
            )
        ], className="text-end")
    ], className="mb-2")
    
    ai_response = generate_ai_response(user_input, metrics or {})
    
    ai_message = html.Div([
        html.Div([
            html.Small("Assistente", className="text-muted", style={"fontSize": "12px"}),
            html.P(
                ai_response,
                className="mb-1 p-2 bg-light rounded",
                style={"maxWidth": "85%", "fontSize": "14px", "whiteSpace": "pre-line"}
            )
        ], className="text-start")
    ], className="mb-2")
    
    updated_messages = current_messages + [user_message, ai_message]
    
    return updated_messages, ""

# =======================================================
# EXECUÇÃO
# =======================================================
if __name__ == '__main__':
    print("🚀 Iniciando app em http://127.0.0.1:8050")
    app.run(host='0.0.0.0', port=8050, debug=True)
