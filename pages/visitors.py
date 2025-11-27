from dash import html, dcc, Input, Output, State, callback, ctx
import dash_bootstrap_components as dbc
from datetime import datetime
import pytz
import requests
import math

# Configurações iniciais
brazil_tz = pytz.timezone('America/Sao_Paulo')
API_VISITORS = "https://api.displayforce.ai/public/v1/stats/visitor/list"
API_TOKEN = "4AUH-BX6H-G2RJ-G7PB"


# ==============================
# LAYOUT
# ==============================
def visitors_layout(devices_cache, side_menu):
    return html.Div([
        # menu lateral presente
        side_menu,

        # navbar fixa no topo igual ao dashboard
        dbc.Navbar(
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
            ], fluid=True),
            color="#004AAD", dark=True, className="py-2 navbar",
            style={"margin": "0", "border": "none"}
        ),
   
        # conteúdo com padding para não ser coberto
        html.Div([
            # Container principal
            dbc.Container([
                html.H2("Listagem de Visitantes", className="mb-2 mt-2", style={"color": "#004AAD"}),

                # Filtros
                dbc.Card([
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col([
                                html.Div([
                                    html.I(className="fas fa-store me-2", style={"color": "#004AAD"}),
                                    html.Label("Loja", className="fw-bold me-2", style={"color": "#004AAD"}),
                                    dcc.Dropdown(
                                        id='visitors-store-filter',
                                        options=[{'label': 'Todas as Lojas', 'value': 'all'}] + [
                                            {'label': d['name'], 'value': d['id']} for d in devices_cache
                                        ],
                                        value='all',
                                        className='filter-dropdown'
                                    )
                                ])
                            ], width=6),
                            dbc.Col([
                                html.Div([
                                    html.I(className="far fa-calendar-alt me-2", style={"color": "#004AAD"}),
                                    html.Label("Período", className="fw-bold me-2", style={"color": "#004AAD"}),
                                    dcc.DatePickerRange(
                                        id='visitors-date-filter',
                                        start_date=datetime.now(brazil_tz).replace(hour=0, minute=0, second=0, microsecond=0),
                                        end_date=datetime.now(brazil_tz).replace(hour=23, minute=59, second=59, microsecond=999999),
                                        display_format='DD/MM/YYYY',
                                        first_day_of_week=0,
                                        className="date-picker"
                                    )
                                ])
                            ], width=6)
                        ])
                    ])
                ], className="mb-3", style={"marginTop": "12px"}),

                # Lista de Visitantes
                dbc.Card([
                    dbc.CardBody([
                        html.Div(id='visitors-table', className='visitors-table'),
                        dbc.Pagination(
                            id='visitors-pagination',
                            max_value=1,
                            active_page=1,
                            first_last=True,
                            previous_next=True,
                            fully_expanded=False,
                            size="md",
                            className="mt-3 justify-content-center"
                        ),
                        dcc.Store(id='devices-cache', data=devices_cache),
                        dcc.Loading(id="loading-visitors", type="circle", children=[])
                    ])
                ])
            ], fluid=True, className="p-4")
        ], style={"paddingTop": "64px"})  # pequeno espaço entre navbar e conteúdo
    ])


# ==============================
# CALLBACK
# ==============================
@callback(
    Output('visitors-table', 'children'),
    Output('visitors-pagination', 'max_value'),
    Output('visitors-pagination', 'active_page'),
    Input('visitors-store-filter', 'value'),
    Input('visitors-date-filter', 'start_date'),
    Input('visitors-date-filter', 'end_date'),
    Input('visitors-pagination', 'active_page'),
    State('devices-cache', 'data'),
    prevent_initial_call=False
)
def update_visitors_table(selected_store, start_date, end_date, active_page, devices_cache_state):
    # Período padrão
    if not start_date or not end_date:
        start_date = datetime.now(brazil_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = datetime.now(brazil_tz).replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        start_date = datetime.fromisoformat(str(start_date))
        end_date = datetime.fromisoformat(str(end_date))
        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=0)

    # Página atual: reseta para 1 quando filtros mudarem
    page = (active_page or 1)
    trigger = ctx.triggered_id
    if trigger in ('visitors-store-filter', 'visitors-date-filter'):
        page = 1

    limit = 60
    offset = (page - 1) * limit

    headers = {"X-API-Token": API_TOKEN}
    payload = {
        "start": start_date.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_date.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tracks": True,
        "pagination": {"offset": offset, "limit": limit}
    }

    try:
        r = requests.post(API_VISITORS, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        visitors = data.get("payload", []) or data.get("data", []) or []
        total = data.get("pagination", {}).get("total", 0)
        total_pages = max(1, math.ceil(total / limit))

        # Filtro por loja
        if selected_store and selected_store != "all":
            visitors = [v for v in visitors if str(selected_store) in [str(d) for d in v.get("devices", [])]]

        header = html.Thead([
            html.Tr([
                html.Th("ID do Visitante", style={"width": "160px"}),
                html.Th("Loja"),
                html.Th("Gênero"),
                html.Th("Idade"),
                html.Th("Entrada"),
                html.Th("Saída")
            ])
        ])

        rows = []
        for v in visitors:
            devices = v.get("devices", [])
            device_names = [
                next((d["name"] for d in devices_cache_state if str(d["id"]) == str(dev)), f"Loja {dev}")
                for dev in devices
            ]
            gender = "Masculino" if v.get("sex") == 1 else "Feminino"
            age = v.get("age", "N/A")
            start_time = datetime.fromisoformat(v["start"].replace('Z', '+00:00')).astimezone(brazil_tz) if v.get("start") else None
            end_time = datetime.fromisoformat(v["end"].replace('Z', '+00:00')).astimezone(brazil_tz) if v.get("end") else None

            rows.append(html.Tr([
                html.Td(v.get("visitor_id", "N/A")),
                html.Td(", ".join(device_names) if device_names else "N/A"),
                html.Td(gender),
                html.Td(f"{age} anos" if isinstance(age, int) else str(age)),
                html.Td(start_time.strftime("%d/%m/%Y %H:%M:%S") if start_time else "N/A"),
                html.Td(end_time.strftime("%d/%m/%Y %H:%M:%S") if end_time else "N/A")
            ]))

        table = dbc.Table([header, html.Tbody(rows)], striped=True, bordered=True, hover=True, responsive=True, className="mb-3")
        return [table], total_pages, page

    except Exception as e:
        print(f"Erro ao buscar visitantes: {e}")
        return [html.Div("Erro ao carregar visitantes", className="text-danger p-3")], 1, 1
