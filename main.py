from fastapi import FastAPI, HTTPException, Request, Cookie, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers, MutableHeaders
from pathlib import Path
import shutil
import unicodedata
import hmac
import hashlib
import os
import gzip
import time
import tempfile
import zipfile
import subprocess
import threading

app = FastAPI(title="F5 Sites Anúncios")

# Permissões CORS (para desenvolvimento local)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Tipos de conteúdo que valem a pena comprimir (imagens/arquivos já são comprimidos)
COMPRESSIVEIS = ("text/html", "application/json", "text/css",
                 "application/javascript", "text/plain", "image/svg+xml")


class GzipSeletivoMiddleware:
    """Gzip puro-ASGI que só comprime tipos textuais (nunca imagens/binários)."""

    def __init__(self, app, minimum_size=500, compresslevel=6):
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return

        estado = {"passar": False, "start": None, "partes": []}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = Headers(raw=message["headers"])
                ctype = headers.get("content-type", "")
                if "content-encoding" in headers or not ctype.startswith(COMPRESSIVEIS):
                    estado["passar"] = True
                    await send(message)
                else:
                    estado["start"] = message
            elif message["type"] == "http.response.body":
                if estado["passar"]:
                    await send(message)
                    return
                estado["partes"].append(message.get("body", b""))
                if not message.get("more_body", False):
                    corpo = b"".join(estado["partes"])
                    if len(corpo) >= self.minimum_size:
                        comprimido = gzip.compress(corpo, self.compresslevel)
                        if len(comprimido) < len(corpo):
                            headers = MutableHeaders(raw=estado["start"]["headers"])
                            headers.add_vary_header("Accept-Encoding")
                            headers["Content-Encoding"] = "gzip"
                            headers["Content-Length"] = str(len(comprimido))
                            corpo = comprimido
                    await send(estado["start"])
                    await send({"type": "http.response.body", "body": corpo, "more_body": False})

        await self.app(scope, receive, send_wrapper)


app.add_middleware(GzipSeletivoMiddleware)


@app.middleware("http")
async def cache_api(request: Request, call_next):
    response = await call_next(request)
    # API sempre fresca (dados dinâmicos)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

BASE_DIR = Path(__file__).parent
IMAGENS_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif', '.bmp'}

# ============================================================
# AUTENTICAÇÃO (seção de vendidos)
# ============================================================
SENHA_VENDIDOS = os.environ.get("SENHA_VENDIDOS", "ehcampeao123")
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "f5sites-anuncios-token-secreto-v1")
COOKIE_NAME = "acesso_vendidos"
TOKEN_VALIDO = hmac.new(SECRET_TOKEN.encode(), SENHA_VENDIDOS.encode(), hashlib.sha256).hexdigest()


def tem_acesso_vendidos(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME, "")
    return hmac.compare_digest(token, TOKEN_VALIDO)

CATEGORIAS = {
    'classificados': {'dir': 'ads-classificados', 'nome': 'Classificado'},
    'fabricado':     {'dir': 'ads-fabricado',     'nome': 'Fabricado'},
    'sold':          {'dir': 'ads-sold/vendido',  'nome': 'Vendido'},
}

DESTINOS_MOVER = {
    'classificados': 'ads-classificados',
    'fabricado':     'ads-fabricado',
    'sold':          'ads-sold/vendido',
}

# ============================================================
# GIT (auto-commit + push a cada alteração)
# ============================================================
GIT_AUTOCOMMIT = os.environ.get("GIT_AUTOCOMMIT", "1").lower() not in ("0", "false", "no", "")
GIT_REMOTE = os.environ.get("GIT_REMOTE", "origin")
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")
GIT_TIMEOUT = int(os.environ.get("GIT_TIMEOUT", "120"))
_git_lock = threading.Lock()

# ============================================================
# HELPERS
# ============================================================

def slugify(text):
    text = unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII')
    text = text.lower()
    text = ''.join(c if c.isalnum() or c in '-_ ' else '-' for c in text)
    text = text.replace(' ', '-').replace('--', '-').strip('-')
    return text[:80]


def escanear_categoria(cat_slug):
    cfg = CATEGORIAS[cat_slug]
    caminho = BASE_DIR / cfg['dir']
    produtos = []
    if not caminho.exists():
        return produtos

    for pasta in sorted(caminho.iterdir()):
        if not pasta.is_dir() or pasta.name.startswith('.') or pasta.name == '.git':
            continue

        imagens = []
        descricao = ""
        versao = 0
        for arquivo in pasta.iterdir():
            if arquivo.suffix.lower() in IMAGENS_EXT:
                imagens.append(arquivo.name)
                try:
                    mt = int(arquivo.stat().st_mtime)
                    if mt > versao:
                        versao = mt
                except OSError:
                    pass
            elif arquivo.suffix.lower() == '.txt':
                try:
                    descricao = arquivo.read_text(encoding='utf-8')
                except:
                    pass

        if not imagens:
            continue

        produtos.append({
            'nome': pasta.name,
            'slug': slugify(pasta.name),
            'categoria': cat_slug,
            'categoria_nome': cfg['nome'],
            'imagens': sorted(imagens),
            'descricao': descricao,
            'pasta_relativa': str(pasta.relative_to(BASE_DIR)),
            'imagem_capa': sorted(imagens)[0],
            'versao': versao,
        })

    return produtos


def _git(args, timeout=None):
    """Executa um comando git no repositório do projeto."""
    timeout = timeout or GIT_TIMEOUT
    cmd = ["git", "-c", "safe.directory=*"]
    usuario = os.environ.get("GIT_USER_NAME")
    email = os.environ.get("GIT_USER_EMAIL")
    if usuario:
        cmd += ["-c", f"user.name={usuario}", "-c", f"user.email={email or usuario}"]
    cmd += ["-C", str(BASE_DIR), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def git_commit_push(mensagem):
    """Faz add/commit/push de todas as alterações. Nunca lança exceção."""
    if not GIT_AUTOCOMMIT:
        return {"ok": False, "motivo": "auto-commit desativado"}
    with _git_lock:
        try:
            add = _git(["add", "-A"])
            if add.returncode != 0:
                erro = (add.stderr or add.stdout).strip()
                print(f"[git] add falhou: {erro}", flush=True)
                return {"ok": False, "erro": erro}

            status = _git(["status", "--porcelain"])
            if not status.stdout.strip():
                return {"ok": True, "commit": False, "push": False, "motivo": "nada para commitar"}

            commit = _git(["commit", "-m", mensagem])
            if commit.returncode != 0:
                erro = (commit.stderr or commit.stdout).strip()
                print(f"[git] commit falhou: {erro}", flush=True)
                return {"ok": False, "commit": False, "erro": erro}

            push = _git(["push", GIT_REMOTE, GIT_BRANCH])
            if push.returncode != 0:
                erro = (push.stderr or push.stdout).strip()
                print(f"[git] push falhou ({GIT_REMOTE} {GIT_BRANCH}): {erro}", flush=True)
                return {"ok": False, "commit": True, "push": False, "erro": erro}

            print(f"[git] commit + push ok: {mensagem}", flush=True)
            return {"ok": True, "commit": True, "push": True}
        except subprocess.TimeoutExpired:
            print("[git] timeout ao executar git", flush=True)
            return {"ok": False, "erro": "timeout ao executar git"}
        except Exception as e:
            print(f"[git] erro inesperado: {e}", flush=True)
            return {"ok": False, "erro": str(e)}


# ============================================================
# API
# ============================================================

@app.get("/api/produtos")
def listar_produtos(request: Request):
    """Lista todos os produtos. Se não autenticado, oculta os vendidos."""
    acesso = tem_acesso_vendidos(request)
    todos = []
    for slug in CATEGORIAS:
        if slug == 'sold' and not acesso:
            continue
        todos.extend(escanear_categoria(slug))
    return {"produtos": todos, "total": len(todos), "autenticado": acesso}


@app.get("/api/produtos/{categoria}")
def listar_por_categoria(categoria: str, request: Request):
    if categoria not in CATEGORIAS:
        raise HTTPException(status_code=404, detail="Categoria não encontrada")
    if categoria == 'sold' and not tem_acesso_vendidos(request):
        raise HTTPException(status_code=401, detail="Seção de vendidos protegida por senha")
    produtos = escanear_categoria(categoria)
    return {"produtos": produtos, "total": len(produtos)}


@app.get("/api/produto/{categoria}/{slug}")
def detalhe_produto(categoria: str, slug: str, request: Request):
    if categoria not in CATEGORIAS:
        raise HTTPException(status_code=404, detail="Categoria não encontrada")
    if categoria == 'sold' and not tem_acesso_vendidos(request):
        raise HTTPException(status_code=401, detail="Seção de vendidos protegida por senha")

    for p in escanear_categoria(categoria):
        if p['slug'] == slug:
            return p
    raise HTTPException(status_code=404, detail="Produto não encontrado")


@app.post("/api/login")
def login(payload: dict, response: Response):
    """Valida a senha e grava um cookie de acesso."""
    senha = payload.get('senha', '')
    if hmac.compare_digest(senha, SENHA_VENDIDOS):
        response.set_cookie(
            COOKIE_NAME, TOKEN_VALIDO,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
        )
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Senha incorreta")


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/status")
def status(request: Request):
    return {"autenticado": tem_acesso_vendidos(request)}


@app.get("/api/git-status")
def git_status(request: Request):
    """Diagnóstico do auto-commit/push. Requer senha."""
    if not tem_acesso_vendidos(request):
        raise HTTPException(status_code=401, detail="Área restrita. Informe a senha.")

    def rodar(args):
        try:
            r = _git(args, timeout=40)
            return {"code": r.returncode, "out": r.stdout.strip(), "err": r.stderr.strip()}
        except Exception as e:
            return {"code": -1, "out": "", "err": str(e)}

    return {
        "autocommit": GIT_AUTOCOMMIT,
        "remote": GIT_REMOTE,
        "branch": GIT_BRANCH,
        "cwd": str(BASE_DIR),
        "usuario": os.environ.get("GIT_USER_NAME") or "(git config)",
        "rev_parse": rodar(["rev-parse", "--abbrev-ref", "HEAD"]),
        "remotes": rodar(["remote", "-v"]),
        "status": rodar(["status", "--porcelain"]),
        "last_commit": rodar(["log", "-1", "--oneline"]),
        "push_dry_run": rodar(["push", "--dry-run", GIT_REMOTE, GIT_BRANCH]),
    }


@app.post("/api/mover")
def mover_produto(payload: dict):
    """
    Move uma pasta de produto entre categorias (sem exigir senha).
    Body: {"origem": "ads-classificados/Nome Produto", "destino": "ads-sold/vendido"}
    """
    origem = BASE_DIR / payload.get('origem', '')
    destino_dir = BASE_DIR / payload.get('destino', '')
    
    if not origem.exists():
        raise HTTPException(status_code=404, detail="Pasta de origem não encontrada")
    
    if not destino_dir.exists():
        destino_dir.mkdir(parents=True, exist_ok=True)
    
    destino_final = destino_dir / origem.name
    
    # Se já existir, renomeia com sufixo
    if destino_final.exists():
        import time
        destino_final = destino_dir / f"{origem.name}_{int(time.time())}"
    
    shutil.move(str(origem), str(destino_final))
    
    origem_rel = str(origem.relative_to(BASE_DIR))
    destino_rel = str(destino_final.relative_to(BASE_DIR))
    git = git_commit_push(f"Mover anúncio: {origem_rel} -> {destino_rel}")
    return {"ok": True, "origem": origem_rel, "destino": destino_rel, "git": git}


# ============================================================
# ADICIONAR ANÚNCIO (ZIP com fotos) — requer senha
# ============================================================

ARQUIVOS_PERMITIDOS = IMAGENS_EXT | {'.txt'}
MAX_ZIP_BYTES = 300 * 1024 * 1024
MAX_EXTRAIDO_BYTES = 1024 * 1024 * 1024


@app.post("/api/adicionar")
async def adicionar_produto(tipo: str, nome: str, request: Request):
    """Cria um novo anúncio a partir de um ZIP com as fotos. Requer senha."""
    if not tem_acesso_vendidos(request):
        raise HTTPException(status_code=401, detail="Área restrita. Informe a senha.")
    if tipo not in CATEGORIAS:
        raise HTTPException(status_code=400, detail="Tipo inválido")

    nome = (nome or "").strip().replace('/', '-').replace('\\', '-').lstrip('.')
    if not nome:
        raise HTTPException(status_code=400, detail="Informe o nome do anúncio")

    base = BASE_DIR / CATEGORIAS[tipo]['dir']
    base.mkdir(parents=True, exist_ok=True)
    destino = base / nome
    if destino.exists():
        destino = base / f"{nome}_{int(time.time())}"

    # Recebe o corpo em streaming para um arquivo temporário (não estoura a memória)
    tam = 0
    resultado = None
    tmp = tempfile.NamedTemporaryFile(suffix='.zip', dir=str(BASE_DIR), delete=False)
    try:
        async for chunk in request.stream():
            tam += len(chunk)
            if tam > MAX_ZIP_BYTES:
                raise HTTPException(status_code=413, detail="ZIP muito grande (máx 300MB)")
            tmp.write(chunk)
        tmp.close()

        if tam == 0:
            raise HTTPException(status_code=400, detail="Arquivo ZIP vazio")

        try:
            zf = zipfile.ZipFile(tmp.name)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Arquivo não é um ZIP válido")

        destino.mkdir(parents=True, exist_ok=True)
        extraidos = 0
        total = 0
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                arquivo = os.path.basename(info.filename)  # achata subpastas + evita path traversal
                if not arquivo or arquivo.startswith('.'):
                    continue
                ext = Path(arquivo).suffix.lower()
                if ext not in ARQUIVOS_PERMITIDOS:
                    continue
                total += info.file_size
                if total > MAX_EXTRAIDO_BYTES:
                    raise HTTPException(status_code=413, detail="Conteúdo extraído muito grande")
                alvo = destino / arquivo
                if alvo.exists():
                    alvo = destino / f"{Path(arquivo).stem}_{extraidos}{ext}"
                with zf.open(info) as src, open(alvo, 'wb') as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                extraidos += 1

        if extraidos == 0:
            try:
                destino.rmdir()
            except OSError:
                pass
            raise HTTPException(status_code=400, detail="Nenhuma imagem encontrada no ZIP")

        resultado = {"ok": True, "destino": str(destino.relative_to(BASE_DIR)), "arquivos": extraidos}
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass

    git = git_commit_push(f"Adicionar anúncio: {resultado['destino']}")
    resultado["git"] = git
    return resultado


@app.post("/api/excluir")
def excluir_produto(payload: dict, request: Request):
    """Exclui a pasta de um anúncio. Requer senha."""
    if not tem_acesso_vendidos(request):
        raise HTTPException(status_code=401, detail="Área restrita. Informe a senha.")
    raizes = [(BASE_DIR / cfg['dir']).resolve() for cfg in CATEGORIAS.values()]
    try:
        alvo = (BASE_DIR / payload.get('pasta', '')).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Caminho inválido")
    if not any(c in alvo.parents for c in raizes):
        raise HTTPException(status_code=400, detail="Caminho inválido")
    if not alvo.is_dir():
        raise HTTPException(status_code=404, detail="Pasta não encontrada")
    shutil.rmtree(alvo)
    removido = str(alvo.relative_to(BASE_DIR.resolve()))
    git = git_commit_push(f"Excluir anúncio: {removido}")
    return {"ok": True, "removido": removido, "git": git}


# ============================================================
# SERVIR IMAGENS ESTATICAS DAS PASTAS REAIS
# ============================================================

@app.get("/imagem/{categoria}/{slug}/{filename}")
def servir_imagem(categoria: str, slug: str, filename: str, request: Request):
    """Serve imagem diretamente da pasta real na raiz."""
    if categoria not in CATEGORIAS:
        raise HTTPException(status_code=404)
    if categoria == 'sold' and not tem_acesso_vendidos(request):
        raise HTTPException(status_code=401, detail="Seção de vendidos protegida por senha")
    
    # Encontra a pasta real pelo slug
    cfg = CATEGORIAS[categoria]
    caminho_base = BASE_DIR / cfg['dir']

    # Imagens de vendidos ficam em cache privado; as demais, público e imutável
    escopo = "private" if categoria == 'sold' else "public"
    headers_cache = {"Cache-Control": f"{escopo}, max-age=31536000, immutable"}

    for pasta in caminho_base.iterdir():
        if pasta.is_dir() and slugify(pasta.name) == slug:
            img_path = pasta / filename
            if img_path.exists():
                return FileResponse(str(img_path), headers=headers_cache)
            break
    
    raise HTTPException(status_code=404, detail="Imagem não encontrada")


# ============================================================
# FRONTEND SPA (HTML EMBUTIDO)
# ============================================================

CSS = """
:root {
  --bg: #0a0a0f; --surface: #13131f; --surface-hover: #1c1c2e;
  --card: #181825; --text: #e2e2e8; --text-muted: #6b6b7b;
  --accent: #00d4aa; --accent-hover: #00b894; --accent-sec: #7c3aed;
  --danger: #ef4444; --border: #252538; --radius: 14px;
  --shadow-lg: 0 10px 40px rgba(0,0,0,0.6);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.6; }
header { background: rgba(19,19,31,0.95); backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border); padding: 1.2rem 2rem;
  position: sticky; top: 0; z-index: 100; }
header h1 { font-size: 1.6rem; font-weight: 800;
  background: linear-gradient(135deg, var(--accent), var(--accent-sec));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
header p { color: var(--text-muted); font-size: 0.9rem; margin-top: 0.2rem; }
nav { margin-top: 1rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
nav button { color: var(--text-muted); padding: 0.5rem 1.2rem; border-radius: 8px;
  font-size: 0.85rem; font-weight: 500; transition: all 0.2s; border: 1px solid transparent;
  background: none; cursor: pointer; font-family: inherit; }
nav button:hover { color: var(--text); background: var(--surface-hover); }
nav button.active { color: var(--accent); border-color: var(--accent);
  background: rgba(0,212,170,0.1); font-weight: 600; }
.container { max-width: 1400px; margin: 0 auto; padding: 2rem; }
.section-title { font-size: 1.4rem; margin: 2.5rem 0 1.2rem; padding-bottom: 0.6rem;
  border-bottom: 2px solid var(--accent); display: inline-block; font-weight: 700; }
.section-title:first-of-type { margin-top: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1.5rem; }
.card { background: var(--card); border-radius: var(--radius); overflow: hidden;
  border: 1px solid var(--border); transition: all 0.3s; cursor: pointer;
  display: flex; flex-direction: column; }
.card:hover { transform: translateY(-6px); box-shadow: var(--shadow-lg); border-color: var(--accent); }
.card-image-wrap { position: relative; overflow: hidden; aspect-ratio: 4/3; }
.card-image { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform 0.3s; }
.card:hover .card-image { transform: scale(1.05); }
.card-image-overlay { position: absolute; top: 0.75rem; right: 0.75rem;
  background: rgba(0,0,0,0.7); padding: 0.3rem 0.7rem; border-radius: 20px;
  font-size: 0.75rem; color: white; backdrop-filter: blur(4px); }
.card-body { padding: 1.1rem; flex: 1; display: flex; flex-direction: column; }
.card-title { font-size: 0.95rem; font-weight: 600; line-height: 1.4;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  margin-bottom: 0.6rem; }
.card-meta { font-size: 0.8rem; color: var(--text-muted); display: flex;
  justify-content: space-between; align-items: center; margin-bottom: 0.8rem; }
.badge { display: inline-block; padding: 0.25rem 0.7rem; border-radius: 6px;
  font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
.badge-classificados { background: rgba(0,212,170,0.15); color: var(--accent); }
.badge-fabricado { background: rgba(124,58,237,0.15); color: #a78bfa; }
.badge-sold { background: rgba(239,68,68,0.15); color: var(--danger); }
.card-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; margin-top: auto;
  padding-top: 0.8rem; border-top: 1px solid var(--border); }
.btn-action { padding: 0.4rem 0.7rem; border-radius: 6px; font-size: 0.72rem;
  font-weight: 600; border: 1px solid var(--border); background: var(--surface);
  color: var(--text-muted); cursor: pointer; transition: all 0.2s; font-family: inherit; }
.btn-action:hover { border-color: var(--accent); color: var(--accent); background: rgba(0,212,170,0.08); }
.btn-action.danger:hover { border-color: var(--danger); color: var(--danger); background: rgba(239,68,68,0.08); }
.btn-action.excluir { border-color: rgba(239,68,68,0.45); color: var(--danger); margin-left: auto; }
.btn-action.excluir:hover { border-color: var(--danger); color: #fff; background: var(--danger); }
.btn-action:disabled { opacity: 0.5; cursor: not-allowed; }

/* Botão WhatsApp */
.btn-whatsapp { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.5rem 0.9rem;
  border-radius: 8px; background: #25D366; color: #0a0a0f; font-weight: 700; font-size: 0.78rem;
  text-decoration: none; border: none; cursor: pointer; transition: all 0.2s; font-family: inherit;
  white-space: nowrap; }
.btn-whatsapp:hover { background: #1ebe5b; transform: translateY(-1px); }
.btn-whatsapp svg { width: 16px; height: 16px; fill: currentColor; flex-shrink: 0; }
.btn-whatsapp.full { width: 100%; justify-content: center; margin-top: 1.5rem; padding: 0.9rem;
  font-size: 1rem; gap: 0.6rem; }
.btn-whatsapp.full svg { width: 22px; height: 22px; }

/* Detail */
.detail { max-width: 1000px; margin: 0 auto; }
.detail-back { display: inline-flex; align-items: center; gap: 0.5rem; color: var(--accent);
  text-decoration: none; margin-bottom: 1.5rem; font-size: 0.9rem; font-weight: 500; }
.detail-back:hover { text-decoration: underline; }
.detail-header { margin-bottom: 1.5rem; }
.detail-title { font-size: 2rem; font-weight: 800; margin-bottom: 0.75rem; line-height: 1.2; }
.detail-badges { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1rem; margin: 1.5rem 0; }
.gallery-item { position: relative; border-radius: var(--radius); overflow: hidden;
  cursor: pointer; aspect-ratio: 1; background: var(--surface); }
.gallery-item img { width: 100%; height: 100%; object-fit: cover; transition: transform 0.3s; }
.gallery-item:hover img { transform: scale(1.08); }
.detail-info { background: var(--card); padding: 1.5rem; border-radius: var(--radius);
  margin-top: 1.5rem; border: 1px solid var(--border); }
.detail-info h3 { color: var(--accent); margin-bottom: 0.75rem; font-size: 1rem; font-weight: 700; }
.detail-info pre, .detail-info .desc-text { white-space: pre-wrap; word-wrap: break-word;
  color: var(--text-muted); font-family: inherit; line-height: 1.8; font-size: 0.95rem; }

/* Lightbox */
.lightbox { display: none; position: fixed; inset: 0; background: rgba(5,5,10,0.98);
  z-index: 1000; justify-content: center; align-items: center; flex-direction: column; }
.lightbox.active { display: flex; }
.lightbox img { max-width: 90vw; max-height: 80vh; border-radius: 8px; object-fit: contain; }
.lightbox-nav { position: absolute; top: 50%; transform: translateY(-50%);
  background: rgba(255,255,255,0.1); border: none; color: white; font-size: 2rem;
  width: 50px; height: 50px; border-radius: 50%; cursor: pointer;
  display: flex; align-items: center; justify-content: center; transition: background 0.2s; }
.lightbox-nav:hover { background: rgba(255,255,255,0.2); }
.lightbox-prev { left: 20px; }
.lightbox-next { right: 20px; }
.lightbox-close { position: absolute; top: 20px; right: 30px; color: white;
  font-size: 2.5rem; cursor: pointer; background: none; border: none; line-height: 1; }
.lightbox-counter { position: absolute; bottom: 30px; color: var(--text-muted); font-size: 0.9rem; }

/* Toast */
.toast { position: fixed; bottom: 30px; right: 30px; background: var(--surface);
  border: 1px solid var(--accent); color: var(--accent); padding: 1rem 1.5rem;
  border-radius: var(--radius); z-index: 3000; font-weight: 600;
  transform: translateY(100px); opacity: 0; transition: all 0.3s; }
.toast.show { transform: translateY(0); opacity: 1; }
.toast.error { border-color: var(--danger); color: var(--danger); }

.empty { text-align: center; padding: 4rem 2rem; color: var(--text-muted); }
footer { text-align: center; padding: 3rem 2rem; color: var(--text-muted);
  font-size: 0.85rem; border-top: 1px solid var(--border); margin-top: 3rem; }

/* Cadeado na nav */
nav button .lock { font-size: 0.75rem; margin-left: 0.3rem; opacity: 0.8; }
nav button.desbloqueado .lock { color: var(--accent); }

/* Modal de senha */
.pw-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85);
  z-index: 2500; justify-content: center; align-items: center; padding: 2rem; }
.pw-overlay.active { display: flex; }
.pw-box { background: var(--surface); border-radius: var(--radius); padding: 2rem;
  max-width: 380px; width: 100%; border: 1px solid var(--border); box-shadow: var(--shadow-lg);
  text-align: center; }
.pw-box .lock-big { font-size: 2.5rem; margin-bottom: 0.5rem; }
.pw-box h3 { margin-bottom: 0.5rem; color: var(--text); }
.pw-box p { color: var(--text-muted); font-size: 0.85rem; margin-bottom: 1.2rem; }
.pw-box input { width: 100%; padding: 0.8rem 1rem; border-radius: 8px;
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
  font-size: 1rem; font-family: inherit; margin-bottom: 1rem; }
.pw-box input:focus { outline: none; border-color: var(--accent); }
.pw-box .pw-erro { color: var(--danger); font-size: 0.85rem; margin-bottom: 0.8rem;
  display: none; }
.pw-actions { display: flex; gap: 0.5rem; }
.pw-actions button { flex: 1; padding: 0.7rem; border-radius: 8px; border: none;
  cursor: pointer; font-weight: 600; font-family: inherit; font-size: 0.9rem; }
.pw-btn-entrar { background: var(--accent); color: var(--bg); }
.pw-btn-entrar:hover { background: var(--accent-hover); }
.pw-btn-cancelar { background: var(--surface-hover); color: var(--text-muted);
  border: 1px solid var(--border) !important; }
.pw-btn-cancelar:hover { color: var(--text); }

/* Formulário de adicionar */
.pw-box.add-box { text-align: left; max-width: 440px; }
.add-box h3, .add-box p { text-align: center; }
.add-box label { display: block; font-size: 0.8rem; color: var(--text-muted);
  margin: 0.7rem 0 0.3rem; }
.add-box select, .add-box input[type=text], .add-box input[type=file] {
  width: 100%; padding: 0.7rem; border-radius: 8px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); font-family: inherit; font-size: 0.9rem; }
.add-box select:focus, .add-box input:focus { outline: none; border-color: var(--accent); }
.add-box .pw-actions { margin-top: 1.2rem; }

/* Cabeçalho: título + botão adicionar */
.header-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }
.btn-adicionar { padding: 0.6rem 1.2rem; border-radius: 8px; border: 1px solid var(--accent);
  background: rgba(0, 212, 170, 0.08); color: var(--accent); font-weight: 700; font-size: 0.85rem;
  font-family: inherit; cursor: pointer; transition: all 0.2s; white-space: nowrap; flex-shrink: 0; }
.btn-adicionar:hover { background: var(--accent); color: var(--bg); }

.btn-logout { background: none; border: 1px solid var(--border); color: var(--text-muted);
  padding: 0.5rem 1rem; border-radius: 8px; cursor: pointer; font-size: 0.8rem;
  font-family: inherit; transition: all 0.2s; }
.btn-logout:hover { border-color: var(--danger); color: var(--danger); }

@media (max-width: 768px) {
  .grid { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 1rem; }
  .gallery { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
  .detail-title { font-size: 1.4rem; }
  .container { padding: 1rem; }
}
"""

JS = r"""
let produtos = [];
let categoriaAtiva = 'classificados';
let autenticado = false;

// ===== WHATSAPP =====
const WHATSAPP_NUM = '5515976044496';
const WA_ICON = '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>';

function linkWhatsApp(nome) {
  const msg = 'Estou interessado: ' + nome;
  return 'https://wa.me/' + WHATSAPP_NUM + '?text=' + encodeURIComponent(msg);
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function carregarProdutos() {
  const res = await fetch('/api/produtos');
  const data = await res.json();
  produtos = data.produtos;
  autenticado = data.autenticado;
  atualizarNav();
  renderizar();
}

function atualizarNav() {
  const btnSold = document.getElementById('btn-sold');
  if (autenticado) {
    btnSold.classList.add('desbloqueado');
    btnSold.innerHTML = 'Vendidos 🔓';
    document.getElementById('btn-logout').style.display = 'inline-block';
  } else {
    btnSold.classList.remove('desbloqueado');
    btnSold.innerHTML = 'Vendidos <span class="lock">🔒</span>';
    document.getElementById('btn-logout').style.display = 'none';
  }
}

function renderizar() {
  const container = document.getElementById('app');
  const filtrados = categoriaAtiva === 'todos' ? produtos : produtos.filter(p => p.categoria === categoriaAtiva);
  
  // Agrupar por categoria para visual
  const grupos = {};
  filtrados.forEach(p => {
    if (!grupos[p.categoria]) grupos[p.categoria] = [];
    grupos[p.categoria].push(p);
  });

  let html = '';
  const ordem = ['classificados', 'fabricado', 'sold'];
  
  ordem.forEach(cat => {
    if (!grupos[cat]) return;
    const cfg = {classificados: 'Classificados', fabricado: 'Produtos & Serviços Próprios', sold: 'Itens Vendidos'}[cat];
    html += `<section id="${cat}"><h2 class="section-title">${cfg} (${grupos[cat].length})</h2><div class="grid">`;
    html += grupos[cat].map(p => renderCard(p)).join('');
    html += '</div></section>';
  });

  if (filtrados.length === 0) {
    html = '<div class="empty">Nenhum item nesta categoria.</div>';
  }

  container.innerHTML = html;
}

function renderCard(p) {
  const imgUrl = `/imagem/${p.categoria}/${p.slug}/${encodeURIComponent(p.imagem_capa)}?v=${p.versao}`;
  const botoes = [];
  
  if (p.categoria !== 'classificados') botoes.push(`<button class="btn-action" onclick="mover(event, '${p.pasta_relativa}', 'ads-classificados')">→ Classificado</button>`);
  if (p.categoria !== 'fabricado') botoes.push(`<button class="btn-action" onclick="mover(event, '${p.pasta_relativa}', 'ads-fabricado')">→ Fabricado</button>`);
  if (p.categoria !== 'sold') botoes.push(`<button class="btn-action danger" onclick="mover(event, '${p.pasta_relativa}', 'ads-sold/vendido')">✓ Vender</button>`);
  if (autenticado) botoes.push(`<button class="btn-action excluir" data-excluir="${esc(p.pasta_relativa)}" data-nome="${esc(p.nome)}">🗑 Excluir</button>`);

  return `
<div class="card" onclick="abrirDetalhe('${p.categoria}', '${p.slug}')">
  <div class="card-image-wrap">
    <img src="${imgUrl}" alt="${p.nome}" class="card-image" loading="lazy">
    <span class="card-image-overlay">${p.imagens.length} foto${p.imagens.length>1?'s':''}</span>
  </div>
  <div class="card-body">
    <div class="card-title">${p.nome}</div>
    <div class="card-meta">
      <span class="badge badge-${p.categoria}">${p.categoria_nome}</span>
    </div>
    <a class="btn-whatsapp" href="${linkWhatsApp(p.nome)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${WA_ICON} Comprar</a>
    <div class="card-actions" onclick="event.stopPropagation()">
      ${botoes.join('')}
    </div>
  </div>
</div>`;
}

function abrirDetalhe(categoria, slug) {
  const p = produtos.find(x => x.categoria === categoria && x.slug === slug);
  if (!p) return;

  const imagens = p.imagens.map((img, i) => 
    `<div class="gallery-item"><img src="/imagem/${p.categoria}/${p.slug}/${encodeURIComponent(img)}?v=${p.versao}" alt="Foto ${i+1}" loading="lazy"></div>`
  ).join('');

  const botoes = [];
  if (p.categoria !== 'classificados') botoes.push(`<button class="btn-action" onclick="mover(event, '${p.pasta_relativa}', 'ads-classificados')">→ Classificado</button>`);
  if (p.categoria !== 'fabricado') botoes.push(`<button class="btn-action" onclick="mover(event, '${p.pasta_relativa}', 'ads-fabricado')">→ Fabricado</button>`);
  if (p.categoria !== 'sold') botoes.push(`<button class="btn-action danger" onclick="mover(event, '${p.pasta_relativa}', 'ads-sold/vendido')">✓ Vender</button>`);
  if (autenticado) botoes.push(`<button class="btn-action excluir" data-excluir="${esc(p.pasta_relativa)}" data-nome="${esc(p.nome)}">🗑 Excluir</button>`);

  const desc = p.descricao ? `<pre>${p.descricao}</pre>` : '<p class="desc-text" style="color: var(--text-muted)">Sem descrição detalhada.</p>';

  document.getElementById('app').innerHTML = `
<div class="detail">
  <a href="#" class="detail-back" onclick="renderizar(); return false;">← Voltar ao catálogo</a>
  <div class="detail-header">
    <h1 class="detail-title">${p.nome}</h1>
    <div class="detail-badges">
      <span class="badge badge-${p.categoria}">${p.categoria_nome}</span>
      <span class="badge" style="background: var(--surface); color: var(--text-muted);">${p.imagens.length} foto${p.imagens.length>1?'s':''}</span>
    </div>
  </div>
  <div class="gallery">${imagens}</div>
  <div class="detail-info"><h3>Descrição</h3>${desc}</div>
  <a class="btn-whatsapp full" href="${linkWhatsApp(p.nome)}" target="_blank" rel="noopener">${WA_ICON} Comprar pelo WhatsApp</a>
  <div class="card-actions" style="margin-top:1.5rem;">${botoes.join('')}</div>
</div>`;

  bindLightbox();
}

async function mover(event, origem, destino) {
  event.stopPropagation();
  const btn = event.target;
  const textoOriginal = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Movendo...';

  try {
    const res = await fetch('/api/mover', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({origem, destino})
    });
    const data = await res.json();
    if (data.ok) {
      showToast('✓ Item movido com sucesso!' + gitResumo(data.git) + ' Recarregando...');
      setTimeout(() => carregarProdutos(), 1000);
    } else {
      throw new Error(data.detail || 'Erro ao mover');
    }
  } catch (err) {
    showToast('✗ ' + err.message, true);
    btn.disabled = false;
    btn.textContent = textoOriginal;
  }
}

async function excluirProduto(pasta, nome) {
  if (!confirm('Excluir "' + nome + '"?\n\nEssa ação não pode ser desfeita.')) return;
  try {
    const res = await fetch('/api/excluir', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pasta})
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      showToast('🗑 Item excluído!' + gitResumo(data.git) + ' Recarregando...');
      setTimeout(() => carregarProdutos(), 800);
    } else {
      showToast('✗ ' + (data.detail || 'Erro ao excluir'), true);
    }
  } catch (err) {
    showToast('✗ Falha de conexão', true);
  }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-excluir]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  excluirProduto(btn.dataset.excluir, btn.dataset.nome);
}, true);

function showToast(msg, isError) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast' + (isError ? ' error' : '') + ' show';
  setTimeout(() => t.classList.remove('show'), 3000);
}

function gitResumo(g) {
  if (!g) return '';
  if (g.ok && g.push) return ' · git ✓ enviado';
  if (g.ok && !g.commit) return ' · git: nada a enviar';
  if (g.ok && g.commit && !g.push) return ' · git: commit local (push falhou)';
  return ' · git ✗ ' + (g.erro || g.motivo || 'falhou');
}

function setCategoria(cat) {
  if (cat === 'sold' && !autenticado) {
    abrirSenha('A seção de vendidos é protegida por senha.');
    return;
  }
  categoriaAtiva = cat;
  document.querySelectorAll('nav button:not(.btn-logout)').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-' + cat).classList.add('active');
  renderizar();
  window.scrollTo({top: 0, behavior: 'smooth'});
}

// ===== SENHA =====
let acaoAposLogin = null;

function abrirSenha(msg, aoEntrar) {
  acaoAposLogin = aoEntrar || null;
  const overlay = document.getElementById('pw-overlay');
  document.getElementById('pw-msg').textContent = msg || 'Digite a senha para visualizar.';
  document.getElementById('pw-erro').style.display = 'none';
  document.getElementById('pw-input').value = '';
  overlay.classList.add('active');
  setTimeout(() => document.getElementById('pw-input').focus(), 100);
}

function fecharSenha() {
  document.getElementById('pw-overlay').classList.remove('active');
  acaoAposLogin = null;
}

async function fazerLogin() {
  const input = document.getElementById('pw-input');
  const erro = document.getElementById('pw-erro');
  const senha = input.value;

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({senha})
    });
    if (res.ok) {
      const cb = acaoAposLogin;
      acaoAposLogin = null;
      fecharSenha();
      autenticado = true;
      atualizarNav();
      showToast('🔓 Acesso liberado!');
      await carregarProdutos();
      if (cb) cb();
    } else {
      erro.textContent = 'Senha incorreta. Tente novamente.';
      erro.style.display = 'block';
      input.value = '';
      input.focus();
    }
  } catch (err) {
    erro.textContent = 'Erro ao conectar. Tente novamente.';
    erro.style.display = 'block';
  }
}

// ===== ADICIONAR ANÚNCIO =====
function abrirAdicionar() {
  if (!autenticado) {
    abrirSenha('Área restrita. Digite a senha para adicionar.', abrirAdicionar);
    return;
  }
  document.getElementById('add-erro').style.display = 'none';
  document.getElementById('add-nome').value = '';
  document.getElementById('add-fotos').value = '';
  document.getElementById('add-overlay').classList.add('active');
  setTimeout(() => document.getElementById('add-nome').focus(), 100);
}

function fecharAdicionar() {
  document.getElementById('add-overlay').classList.remove('active');
}

// ===== ZIP no navegador (método "stored", sem biblioteca) =====
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

async function criarZip(files) {
  const enc = new TextEncoder();
  const partes = [];
  const centrais = [];
  let offset = 0;
  for (const f of files) {
    const nomeBytes = enc.encode(f.name);
    const dados = new Uint8Array(await f.arrayBuffer());
    const crc = crc32(dados);

    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true);
    lh.setUint16(4, 20, true);
    lh.setUint16(6, 0x0800, true);   // UTF-8
    lh.setUint16(8, 0, true);        // stored
    lh.setUint32(14, crc, true);
    lh.setUint32(18, dados.length, true);
    lh.setUint32(22, dados.length, true);
    lh.setUint16(26, nomeBytes.length, true);
    partes.push(new Uint8Array(lh.buffer), nomeBytes, dados);

    const cd = new DataView(new ArrayBuffer(46));
    cd.setUint32(0, 0x02014b50, true);
    cd.setUint16(4, 20, true);
    cd.setUint16(6, 20, true);
    cd.setUint16(8, 0x0800, true);
    cd.setUint16(10, 0, true);
    cd.setUint32(16, crc, true);
    cd.setUint32(20, dados.length, true);
    cd.setUint32(24, dados.length, true);
    cd.setUint16(28, nomeBytes.length, true);
    cd.setUint32(42, offset, true);
    centrais.push(new Uint8Array(cd.buffer), nomeBytes);

    offset += 30 + nomeBytes.length + dados.length;
  }
  const cdSize = centrais.reduce((a, b) => a + b.length, 0);
  const eocd = new DataView(new ArrayBuffer(22));
  eocd.setUint32(0, 0x06054b50, true);
  eocd.setUint16(8, files.length, true);
  eocd.setUint16(10, files.length, true);
  eocd.setUint32(12, cdSize, true);
  eocd.setUint32(16, offset, true);
  partes.push(...centrais, new Uint8Array(eocd.buffer));
  return new Blob(partes, {type: 'application/zip'});
}

async function enviarAdicionar() {
  const tipo = document.getElementById('add-tipo').value;
  const nome = document.getElementById('add-nome').value.trim();
  const files = Array.from(document.getElementById('add-fotos').files);
  const erro = document.getElementById('add-erro');
  const btn = document.getElementById('add-enviar');

  if (!nome) { erro.textContent = 'Informe o nome do anúncio.'; erro.style.display = 'block'; return; }
  if (files.length === 0) { erro.textContent = 'Selecione fotos ou um arquivo .zip.'; erro.style.display = 'block'; return; }

  // Se for um único .zip, envia direto; senão, monta um zip com as fotos
  let corpo;
  if (files.length === 1 && files[0].name.toLowerCase().endsWith('.zip')) {
    corpo = files[0];
  } else {
    const ok = /\.(jpe?g|png|webp|gif|avif|bmp)$/i;
    const invalidos = files.filter(f => !ok.test(f.name)).map(f => f.name);
    if (invalidos.length) {
      erro.textContent = 'Só imagens ou um .zip. Inválido: ' + invalidos.join(', ');
      erro.style.display = 'block';
      return;
    }
    corpo = await criarZip(files);
  }

  erro.style.display = 'none';
  btn.disabled = true;
  const rotulo = btn.textContent;
  btn.textContent = 'Enviando...';

  try {
    const url = `/api/adicionar?tipo=${encodeURIComponent(tipo)}&nome=${encodeURIComponent(nome)}`;
    const res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/zip'},
      body: corpo
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      fecharAdicionar();
      showToast(`✓ Anúncio adicionado (${data.arquivos} arquivos)` + gitResumo(data.git));
      categoriaAtiva = tipo;
      document.querySelectorAll('nav button:not(.btn-logout)').forEach(b => b.classList.remove('active'));
      const alvo = document.getElementById('btn-' + tipo);
      if (alvo) alvo.classList.add('active');
      await carregarProdutos();
    } else {
      erro.textContent = data.detail || 'Erro ao enviar o anúncio.';
      erro.style.display = 'block';
    }
  } catch (e) {
    erro.textContent = 'Falha de conexão ao enviar.';
    erro.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = rotulo;
  }
}

async function logout() {
  await fetch('/api/logout', {method: 'POST'});
  autenticado = false;
  categoriaAtiva = 'classificados';
  document.querySelectorAll('nav button:not(.btn-logout)').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-classificados').classList.add('active');
  showToast('🔒 Sessão encerrada');
  await carregarProdutos();
  window.scrollTo({top: 0, behavior: 'smooth'});
}


function bindLightbox() {
  const existing = document.querySelector('.lightbox');
  if (existing) existing.remove();

  const lb = document.createElement('div');
  lb.className = 'lightbox';
  lb.innerHTML = '<button class="lightbox-close">&times;</button><button class="lightbox-nav lightbox-prev">&#10094;</button><img src="" alt=""><button class="lightbox-nav lightbox-next">&#10095;</button><div class="lightbox-counter"></div>';
  document.body.appendChild(lb);

  const img = lb.querySelector('img');
  let imgs = [], idx = 0;

  document.querySelectorAll('.gallery-item img').forEach((el, i) => {
    imgs = Array.from(el.closest('.gallery').querySelectorAll('.gallery-item img')).map(x => x.src);
    el.addEventListener('click', () => { idx = i; img.src = imgs[idx]; lb.classList.add('active'); document.body.style.overflow='hidden'; updateCounter(); });
  });

  function updateCounter() { lb.querySelector('.lightbox-counter').textContent = `${idx+1} / ${imgs.length}`; }
  function next() { idx = (idx+1)%imgs.length; img.src = imgs[idx]; updateCounter(); }
  function prev() { idx = (idx-1+imgs.length)%imgs.length; img.src = imgs[idx]; updateCounter(); }
  function close() { lb.classList.remove('active'); document.body.style.overflow=''; }

  lb.querySelector('.lightbox-close').addEventListener('click', close);
  lb.querySelector('.lightbox-next').addEventListener('click', (e) => { e.stopPropagation(); next(); });
  lb.querySelector('.lightbox-prev').addEventListener('click', (e) => { e.stopPropagation(); prev(); });
  lb.addEventListener('click', (e) => { if(e.target===lb) close(); });
  document.addEventListener('keydown', (e) => {
    if(!lb.classList.contains('active')) return;
    if(e.key==='Escape') close(); if(e.key==='ArrowRight') next(); if(e.key==='ArrowLeft') prev();
  });
}

// Inicializar
carregarProdutos();
"""

HTML = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>F5 Sites Anúncios</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <div class="header-top">
    <div>
      <h1>F5 Sites Anúncios</h1>
      <p>Catálogo de produtos, serviços e itens vendidos</p>
    </div>
    <button id="btn-adicionar" class="btn-adicionar" onclick="abrirAdicionar()">➕ Adicionar</button>
  </div>
  <nav>
    <button id="btn-todos" onclick="setCategoria('todos')">Todos</button>
    <button id="btn-classificados" class="active" onclick="setCategoria('classificados')">Classificados</button>
    <button id="btn-fabricado" onclick="setCategoria('fabricado')">Fabricados</button>
    <button id="btn-sold" onclick="setCategoria('sold')">Vendidos <span class="lock">🔒</span></button>
    <button id="btn-logout" class="btn-logout" style="display:none;margin-left:auto;" onclick="logout()">Sair</button>
  </nav>
</header>
<div class="container" id="app">
  <div class="empty">Carregando...</div>
</div>
<footer>
  <p>F5 Sites Anúncios • Servido por FastAPI</p>
</footer>
<div class="toast" id="toast"></div>

<div class="pw-overlay" id="pw-overlay">
  <div class="pw-box">
    <div class="lock-big">🔒</div>
    <h3>Área restrita</h3>
    <p id="pw-msg">Digite a senha para visualizar.</p>
    <input type="password" id="pw-input" placeholder="Senha" onkeydown="if(event.key==='Enter') fazerLogin()">
    <div class="pw-erro" id="pw-erro"></div>
    <div class="pw-actions">
      <button class="pw-btn-cancelar" onclick="fecharSenha()">Cancelar</button>
      <button class="pw-btn-entrar" onclick="fazerLogin()">Entrar</button>
    </div>
  </div>
</div>

<div class="pw-overlay" id="add-overlay">
  <div class="pw-box add-box">
    <h3>Adicionar anúncio</h3>
    <p>Escolha o tipo e envie um ZIP com as fotos.</p>
    <label>Tipo</label>
    <select id="add-tipo">
      <option value="classificados">Classificado</option>
      <option value="fabricado">Fabricado</option>
      <option value="sold">Vendido</option>
    </select>
    <label>Nome do anúncio</label>
    <input type="text" id="add-nome" placeholder="Ex: Bicicleta Caloi Aro 26">
    <label>Fotos (imagens ou um .zip)</label>
    <input type="file" id="add-fotos" accept=".zip,image/*" multiple>
    <div class="pw-erro" id="add-erro"></div>
    <div class="pw-actions">
      <button class="pw-btn-cancelar" onclick="fecharAdicionar()">Cancelar</button>
      <button class="pw-btn-entrar" id="add-enviar" onclick="enviarAdicionar()">Enviar</button>
    </div>
  </div>
</div>

<script>{JS}</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=HTML, headers={"Cache-Control": "no-cache"})


# ============================================================
# RODAR
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
