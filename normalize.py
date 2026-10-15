#!/usr/bin/env python3
"""
Reduz imagens GRANDES e vídeos nas pastas ads-*, mantendo o formato.

- Fotos .jpg/.jpeg com 500KB ou mais -> recomprime mantendo .jpg (qualidade 82,
  maior lado limitado a 1600px), mesmo nome de arquivo.
- Fotos menores que 500KB: intactas.
- PNG: intactos (não converte formato).
- Vídeos .mp4/.mov -> H.264 (só substitui se ficar menor), mesmo nome.

Uso:
  python3 normalize.py --dry-run
  python3 normalize.py
  python3 normalize.py --conteudo ads-sold
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None
except ImportError:
    print("Erro: Pillow não instalado. Rode: pip install Pillow")
    sys.exit(1)

REPOS = ['ads-classificados', 'ads-fabricado', 'ads-sold']
IMG_EXT = {'.jpg', '.jpeg'}          # só JPEG
VID_EXT = {'.mp4', '.mov'}
LIMIAR = 500 * 1024                   # 500 KB
MAX_DIM = 1600
QUALITY = 82
VIDEO_CRF = 30
VIDEO_MAX_W = 1280


def human(n):
    return f"{n/1024/1024:.1f} MB"


def coletar(repos):
    imagens, videos = [], []
    for repo in repos:
        root_dir = Path(repo)
        if not root_dir.exists():
            continue
        for root, dirs, files in os.walk(root_dir):
            if '.git' in dirs:
                dirs.remove('.git')
            for f in files:
                p = Path(root) / f
                if p.is_symlink() or not p.is_file():
                    continue
                try:
                    p.stat()
                except OSError:
                    continue
                ext = p.suffix.lower()
                if ext in IMG_EXT:
                    imagens.append(p)
                elif ext in VID_EXT:
                    videos.append(p)
    return imagens, videos


def reduzir_foto(p, dry):
    orig = p.stat().st_size
    if orig < LIMIAR:
        return ('skip', orig, orig, p)
    if dry:
        return ('img', orig, 0, p)
    tmp = None
    try:
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im).convert('RGB')
            im.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=p.suffix)
            os.close(fd)
            im.save(tmp, 'JPEG', quality=QUALITY, optimize=True, progressive=True)
        novo = os.path.getsize(tmp)
        if novo < orig:
            os.replace(tmp, p)
            return ('img', orig, novo, p)
        else:
            os.remove(tmp)
            return ('skip', orig, orig, p)
    except Exception as e:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        return ('erro', orig, 0, p)


def comprimir_video(p, dry):
    orig = p.stat().st_size
    if dry:
        return ('vid', orig, 0, p)
    tmp = p.with_name(p.stem + '.normalizado.mp4')
    cmd = [
        'ffmpeg', '-y', '-loglevel', 'error', '-i', str(p),
        '-vf', f"scale='min({VIDEO_MAX_W},iw)':-2",
        '-c:v', 'libx264', '-crf', str(VIDEO_CRF), '-preset', 'fast',
        '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart',
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        return ('erro', orig, 0, p)
    novo = tmp.stat().st_size
    if novo < orig * 0.95:
        p.unlink()
        tmp.rename(p.with_suffix('.mp4'))
        return ('vid', orig, novo, p.with_suffix('.mp4'))
    else:
        tmp.unlink()
        return ('skip', orig, orig, p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--conteudo', default=None)
    args = ap.parse_args()

    repos = [args.conteudo] if args.conteudo else REPOS
    imagens, videos = coletar(repos)
    alvo = [p for p in imagens if p.stat().st_size >= LIMIAR]

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Reduzindo (JPG >= {LIMIAR//1024}KB -> max {MAX_DIM}px q{QUALITY})")
    print(f"  Fotos no total:   {len(imagens):,} ({human(sum(p.stat().st_size for p in imagens))})")
    print(f"  Fotos a reduzir:  {len(alvo):,} ({human(sum(p.stat().st_size for p in alvo))})")
    print(f"  Vídeos:           {len(videos):,} ({human(sum(p.stat().st_size for p in videos))})")
    print("-" * 60)

    o_img = n_img = o_vid = n_vid = 0
    erros = 0
    for i, p in enumerate(imagens, 1):
        tipo, o, n, dest = reduzir_foto(p, args.dry_run)
        o_img += o
        n_img += (n if n else o)
        if tipo == 'erro':
            erros += 1
            print(f"  ERRO foto: {p}")
        if i % 300 == 0:
            print(f"  ...{i}/{len(imagens)}  {human(o_img)} -> {human(n_img)}")

    for p in videos:
        tipo, o, n, dest = comprimir_video(p, args.dry_run)
        o_vid += o
        n_vid += (n if n else o)
        if tipo == 'vid':
            print(f"  vídeo: {p.name}  {human(o)} -> {human(n)}")
        elif tipo == 'erro':
            erros += 1
            print(f"  ERRO vídeo: {p.name}")
        else:
            print(f"  vídeo mantido (sem ganho): {p.name}")

    print("=" * 60)
    if args.dry_run:
        print(f"[DRY-RUN] Fotos >= {LIMIAR//1024}KB: {len(alvo):,} | nada alterado.")
        return
    tot_o = sum(p.stat().st_size for p in imagens)  # recalculado? não: usa acumulado
    print(f"FOTOS:  {human(o_img)} -> {human(n_img)}")
    print(f"VÍDEOS: {human(o_vid)} -> {human(n_vid)}")
    print(f"Erros:  {erros}")


if __name__ == '__main__':
    main()
