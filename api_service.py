import requests
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== CONFIGURAÇÕES =====================
API_BASE = "https://api.displayforce.ai/public/v1"
API_VISITORS = f"{API_BASE}/stats/visitor/list"
API_TOKEN = "4AUH-BX6H-G2RJ-G7PB"

# ===================== FUNÇÕES =====================

def get_devices():
    """Lista de dispositivos (mock, pode ser substituído pela API real se disponível)."""
    return [
        {"id": "store1", "name": "Assaí - Loja 1"},
        {"id": "store2", "name": "Assaí - Loja 2"},
        {"id": "store3", "name": "Assaí - Loja 3"},
    ]


def fetch_visitors_page(offset, headers, base_payload):
    """Busca uma página específica de visitantes com retry."""
    from copy import deepcopy
    payload = deepcopy(base_payload)
    payload["pagination"]["offset"] = offset
    retries = 2
    for attempt in range(retries):
        try:
            r = requests.post(API_VISITORS, headers=headers, json=payload, timeout=10)
            if r.status_code == 429:
                wait = 2 * (attempt + 1)
                print(f"[WAIT] 429 recebido, aguardando {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("payload", [])
        except Exception as e:
            print(f"[ERRO] Página offset={offset}, tentativa {attempt+1}: {e}")
            time.sleep(2)
    return []


def get_visitors(start_date=None, end_date=None, selected_store=None):
    """
    Busca visitantes reais da API DisplayForce.
    start_date, end_date -> datetime
    selected_store -> string
    """
    try:
        if not start_date or not end_date:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=30)

        headers = {"X-API-Token": API_TOKEN}

        base_payload = {
            "start": start_date.strftime("%Y-%m-%dT00:00:00Z"),
            "end": end_date.strftime("%Y-%m-%dT23:59:59Z"),
            "tracks": True,
            "face_quality": True,
            "glasses": True,
            "facial_hair": True,
            "hair_color": True,
            "hair_type": True,
            "headwear": True,
            "additional_attributes": [
                "smile",
                "pitch",
                "yaw",
                "x",
                "y",
                "height"
            ]
        }

        # Requisição inicial para pegar total de registros
        first_resp = requests.post(API_VISITORS, headers=headers, json=base_payload, timeout=20)
        first_resp.raise_for_status()
        data = first_resp.json()

        total = data.get("pagination", {}).get("total", 0)
        print(f"[OK] Total de registros na API: {total}")

        visitors = data.get("payload", [])

        if total > 100:
            offsets = list(range(100, total, 100))
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(fetch_visitors_page, offset, headers, base_payload) for offset in offsets]
                for future in as_completed(futures):
                    visitors.extend(future.result())

        # Filtrar por loja se selecionado
        if selected_store and selected_store != "all":
            visitors = [v for v in visitors if v.get("device_id") == selected_store]

        print(f"[FINALIZADO] Total carregado após filtro: {len(visitors)} registros")
        return visitors

    except Exception as e:
        print(f"[ERRO] get_visitors: {e}")
        return []
