from dash import html
import dash_bootstrap_components as dbc

layout = html.Div(
    className="login-container",
    children=[
        html.Div(className="login-box", children=[
            html.Div(style={"marginBottom": "24px"}, children=[
                html.Div(className="logo", style={"margin": "0 auto 16px"}, children="A"),
                html.H2("Assaí Atacadista", className="login-title"),
                html.P("Dashboard de Análise", className="login-subtitle"),
            ]),
            dbc.Input(
                id="user",
                placeholder="E-mail",
                type="email",
                className="login-input",
                style={"marginBottom": "12px"}
            ),
            dbc.Input(
                id="password",
                placeholder="Senha",
                type="password",
                className="login-input",
                style={"marginBottom": "20px"}
            ),
            html.Button(
                "Entrar",
                id="login-btn",
                className="login-btn"
            ),
            html.Div(
                "Sistema de análise e monitoramento",
                style={
                    "fontSize": "12px",
                    "color": "#666",
                    "marginTop": "24px"
                }
            ),
            html.Div(id="login-alert", className="login-alert"),
        ])
    ]
)
