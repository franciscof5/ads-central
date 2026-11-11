import json
from pathlib import Path

import pocketbase

BASE_DIR = Path(__file__).parent
META_FILE = "meta.json"
IMAGENS_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}


def ler_meta(pasta):
    try:
        dados = json.loads((Path(pasta) / META_FILE).read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def produto_id(pasta):
    return ler_meta(pasta).get("product_id")


def escrever_meta(pasta, record_id):
    arquivo = Path(pasta) / META_FILE
    atual = ler_meta(pasta)
    if atual.get("product_id") == record_id:
        return
    dados = dict(atual)
    dados["product_id"] = record_id
    arquivo.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _parece_produto(pasta):
    try:
        return any(
            p.is_file() and p.suffix.lower() in IMAGENS_EXT for p in pasta.iterdir()
        )
    except OSError:
        return False


def listar_pastas(dirs):
    pastas = []
    for nome in dirs:
        base = BASE_DIR / nome
        if not base.exists():
            continue
        for pasta in sorted(base.iterdir()):
            if not pasta.is_dir() or pasta.name.startswith("."):
                continue
            if _parece_produto(pasta):
                pastas.append(pasta)
    return pastas


def rel(pasta):
    return str(Path(pasta).relative_to(BASE_DIR))


def garantir_registro(pasta):
    rel_path = rel(pasta)
    pid = produto_id(pasta)
    if pid:
        try:
            return pocketbase.obter(pid)
        except pocketbase.PocketBaseError:
            pass
    rec = pocketbase.procurar_produto(rel_path)
    if not rec:
        rec = pocketbase.criar({"folder": rel_path, "data": {"price": None}})
    escrever_meta(pasta, rec["id"])
    return rec


def sincronizar(dirs, dry_run=False):
    if not pocketbase.configurado():
        return {"ok": False, "motivo": "PocketBase não configurado"}

    pastas = listar_pastas(dirs)
    registros = pocketbase.listar_todos()
    por_id = {r["id"]: r for r in registros}
    por_folder = {r.get("folder"): r for r in registros if r.get("folder")}

    ids_por_pasta = {rel(p): produto_id(p) for p in pastas}
    pastas_por_id = {}
    for caminho, pid in ids_por_pasta.items():
        if pid:
            pastas_por_id.setdefault(pid, []).append(caminho)

    criados = atualizados = duplicados = 0
    rel_para_id = {}

    for pasta in pastas:
        caminho = rel(pasta)
        pid = ids_por_pasta.get(caminho)
        rec = por_id.get(pid) if pid else None

        if rec and len(pastas_por_id.get(pid, [])) > 1:
            grupo = pastas_por_id[pid]
            canonico = rec.get("folder")
            if canonico not in grupo:
                canonico = grupo[0]
            if caminho == canonico:
                if rec.get("folder") != caminho:
                    if not dry_run:
                        pocketbase.atualizar(pid, {"folder": caminho})
                    atualizados += 1
                rel_para_id[caminho] = pid
                if not dry_run and produto_id(pasta) != pid:
                    escrever_meta(pasta, pid)
                continue
            if not dry_run:
                novo = pocketbase.criar(
                    {"folder": caminho, "data": rec.get("data") or {}}
                )
                escrever_meta(pasta, novo["id"])
                por_id[novo["id"]] = novo
                rel_para_id[caminho] = novo["id"]
            duplicados += 1
            continue

        if rec:
            if rec.get("folder") != caminho:
                if not dry_run:
                    pocketbase.atualizar(pid, {"folder": caminho})
                atualizados += 1
            rel_para_id[caminho] = pid
            if not dry_run and produto_id(pasta) != pid:
                escrever_meta(pasta, pid)
            continue

        rec_folder = por_folder.get(caminho)
        if rec_folder:
            if not dry_run:
                escrever_meta(pasta, rec_folder["id"])
            rel_para_id[caminho] = rec_folder["id"]
            atualizados += 1
            continue

        if not dry_run:
            novo = pocketbase.criar(
                {"folder": caminho, "data": {"price": None}}
            )
            escrever_meta(pasta, novo["id"])
            por_id[novo["id"]] = novo
            rel_para_id[caminho] = novo["id"]
        criados += 1

    caminhos = {rel(p) for p in pastas}
    referenciados = set(rel_para_id.values())
    orfaos = [
        r.get("folder") or r["id"]
        for r in registros
        if r["id"] not in referenciados and r.get("folder") not in caminhos
    ]

    return {
        "ok": True,
        "dry_run": dry_run,
        "pastas": len(pastas),
        "criados": criados,
        "atualizados": atualizados,
        "duplicados": duplicados,
        "orfaos": orfaos,
    }


if __name__ == "__main__":
    import sys

    dirs = ["ads-classificados", "ads-fabricado", "ads-revenda", "ads-sold/vendido"]
    print(sincronizar(dirs, dry_run="--dry-run" in sys.argv[1:]))
