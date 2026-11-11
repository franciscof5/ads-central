import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def _carregar_env():
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            for linha in arquivo:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                chave, valor = linha.split("=", 1)
                os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_carregar_env()

BASE_URL = os.environ.get("POCKETBASE_URL", "http://pocket:8090").rstrip("/")
EMAIL = os.environ.get("POCKETBASE_EMAIL", "")
PASSWORD = os.environ.get("POCKETBASE_PASSWORD", "")
PRODUTOS = os.environ.get("POCKETBASE_COLLECTION", "products")
LOCALIZACOES = os.environ.get("POCKETBASE_LOCATIONS", "locations")
TIMEOUT = float(os.environ.get("POCKETBASE_TIMEOUT", "10"))

_lock = threading.Lock()
_token = {"valor": None, "expira": 0.0}


class PocketBaseError(Exception):
    pass


def configurado():
    return bool(EMAIL and PASSWORD)


def testar_auth():
    _autenticar(forcar=True)
    return True


def _autenticar(forcar=False):
    if not configurado():
        raise PocketBaseError("PocketBase não configurado (POCKETBASE_EMAIL/POCKETBASE_PASSWORD)")
    agora = time.time()
    with _lock:
        if not forcar and _token["valor"] and _token["expira"] > agora:
            return _token["valor"]
        corpo = json.dumps({"identity": EMAIL, "password": PASSWORD}).encode()
        req = urllib.request.Request(
            BASE_URL + "/api/collections/_superusers/auth-with-password",
            data=corpo,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                dados = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detalhe = e.read().decode(errors="replace")[:300]
            raise PocketBaseError(f"Falha ao autenticar no PocketBase: {e.code} {detalhe}")
        except urllib.error.URLError as e:
            raise PocketBaseError(f"PocketBase indisponível: {e.reason}")
        _token["valor"] = dados.get("token")
        _token["expira"] = agora + 1800
        return _token["valor"]


def _requisicao(metodo, caminho, params=None, corpo=None):
    url = BASE_URL + caminho
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for tentativa in range(2):
        token = _autenticar(forcar=tentativa > 0)
        dados = None
        headers = {"Authorization": token, "Accept": "application/json"}
        if corpo is not None:
            dados = json.dumps(corpo).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=dados, method=metodo, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                texto = resp.read().decode()
                return json.loads(texto) if texto else None
        except urllib.error.HTTPError as e:
            detalhe = e.read().decode(errors="replace")[:300]
            if e.code == 401 and tentativa == 0:
                continue
            raise PocketBaseError(f"{metodo} {caminho}: {e.code} {detalhe}")
        except urllib.error.URLError as e:
            raise PocketBaseError(f"PocketBase indisponível: {e.reason}")
    raise PocketBaseError("Falha inesperada no PocketBase")


def listar_todos(colecao=PRODUTOS, por_pagina=200):
    itens = []
    pagina = 1
    while True:
        dados = _requisicao(
            "GET",
            f"/api/collections/{colecao}/records",
            params={"page": pagina, "perPage": por_pagina},
        )
        itens.extend(dados.get("items", []))
        if pagina >= dados.get("totalPages", 0):
            break
        pagina += 1
    return itens


def obter(record_id, colecao=PRODUTOS):
    return _requisicao("GET", f"/api/collections/{colecao}/records/{record_id}")


def criar(dados, colecao=PRODUTOS):
    return _requisicao("POST", f"/api/collections/{colecao}/records", corpo=dados)


def atualizar(record_id, dados, colecao=PRODUTOS):
    return _requisicao("PATCH", f"/api/collections/{colecao}/records/{record_id}", corpo=dados)


def excluir(record_id, colecao=PRODUTOS):
    return _requisicao("DELETE", f"/api/collections/{colecao}/records/{record_id}")


def _escapar(valor):
    return str(valor).replace("\\", "\\\\").replace('"', '\\"')


def procurar_produto(folder):
    filtro = f'folder="{_escapar(folder)}"'
    dados = _requisicao(
        "GET",
        f"/api/collections/{PRODUTOS}/records",
        params={"filter": filtro, "perPage": 1},
    )
    itens = dados.get("items", [])
    return itens[0] if itens else None


def produtos_por_pasta():
    return {r["folder"]: r for r in listar_todos() if r.get("folder")}


def localizacoes():
    return listar_todos(LOCALIZACOES)


def criar_localizacao(dados):
    return criar(dados, LOCALIZACOES)


def atualizar_localizacao(record_id, dados):
    return atualizar(record_id, dados, LOCALIZACOES)


def excluir_localizacao(record_id):
    return excluir(record_id, LOCALIZACOES)


def listar_colecoes():
    return _requisicao("GET", "/api/collections", params={"perPage": 200})


def criar_colecao(dados):
    return _requisicao("POST", "/api/collections", corpo=dados)


def atualizar_colecao(colecao, dados):
    return _requisicao("PATCH", f"/api/collections/{colecao}", corpo=dados)


def disponivel():
    try:
        req = urllib.request.Request(BASE_URL + "/api/health")
        with urllib.request.urlopen(req, timeout=min(TIMEOUT, 5)) as resp:
            return resp.status == 200
    except Exception:
        return False
