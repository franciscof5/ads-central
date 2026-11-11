import os
import pocketbase

PRODUTOS = pocketbase.PRODUTOS
LOCALIZACOES = pocketbase.LOCALIZACOES


def _existente(nome):
    for colecao in pocketbase.listar_colecoes().get("items", []):
        if colecao.get("name") == nome:
            return colecao
    return None


def _regras_bloqueadas():
    return {
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
    }


def setup():
    if not pocketbase.configurado():
        print("Defina POCKETBASE_EMAIL e POCKETBASE_PASSWORD no ambiente/.env.")
        return 1

    locais = _existente(LOCALIZACOES)
    if not locais:
        dados = {
            "name": LOCALIZACOES,
            "type": "base",
            "fields": [
                {"name": "name", "type": "text", "required": True, "max": 200},
                {"name": "parent", "type": "text", "max": 50},
                {"name": "order", "type": "number"},
                {"name": "notes", "type": "text", "max": 1000},
            ],
            **_regras_bloqueadas(),
        }
        locais = pocketbase.criar_colecao(dados)
        print(f"Coleção criada: {LOCALIZACOES} ({locais['id']})")
    else:
        print(f"Coleção existente: {LOCALIZACOES} ({locais['id']})")

    produtos = _existente(PRODUTOS)
    if not produtos:
        dados = {
            "name": PRODUTOS,
            "type": "base",
            "fields": [
                {"name": "folder", "type": "text", "required": True, "max": 500},
                {"name": "data", "type": "json"},
            ],
            "indexes": [
                f"CREATE UNIQUE INDEX `idx_{PRODUTOS}_folder` ON `{PRODUTOS}` (`folder`)"
            ],
            **_regras_bloqueadas(),
        }
        produtos = pocketbase.criar_colecao(dados)
        print(f"Coleção criada: {PRODUTOS} ({produtos['id']})")
    else:
        print(f"Coleção existente: {PRODUTOS} ({produtos['id']})")

    endereco = (os.environ.get("POCKETBASE_SEED_ADDRESS") or "").strip()
    if endereco:
        ja_existe = any(
            (l.get("name") == endereco and not (l.get("parent") or ""))
            for l in pocketbase.localizacoes()
        )
        if not ja_existe:
            rec = pocketbase.criar_localizacao(
                {"name": endereco, "parent": "", "order": 0, "notes": ""}
            )
            print(f"Endereço criado: {endereco} ({rec['id']})")
        else:
            print(f"Endereço existente: {endereco}")

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(setup())
