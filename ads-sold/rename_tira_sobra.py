import os
import sys
import re

# Verifica se o diretório foi passado como argumento
if len(sys.argv) != 2:
    print(f"Uso: {sys.argv[0]} <diretório>")
    sys.exit(1)

diretorio_raiz = sys.argv[1]

# Verifica se o diretório existe
if not os.path.isdir(diretorio_raiz):
    print(f"Erro: Diretório '{diretorio_raiz}' não encontrado!")
    sys.exit(1)

# Regex para remover tudo após "R$"
padrao = re.compile(r" R\$.*")

# Percorre todos os diretórios recursivamente
for raiz, dirs, _ in os.walk(diretorio_raiz, topdown=False):  # topdown=False para renomear primeiro os mais profundos
    for nome_dir in dirs:
        caminho_antigo = os.path.join(raiz, nome_dir)

        try:
            # Se não encontrar "R$", pula para o próximo
            if " R$" not in nome_dir:
                continue

            # Remove tudo após "R$" e espaços extras no final
            novo_nome = re.sub(padrao, "", nome_dir).rstrip()

            # Se o nome mudou, renomeia
            if nome_dir != novo_nome:
                caminho_novo = os.path.join(raiz, novo_nome)
                os.rename(caminho_antigo, caminho_novo)
                print(f"Renomeado: '{caminho_antigo}' -> '{caminho_novo}'")

        except Exception as e:
            print(f"Erro ao renomear '{caminho_antigo}': {e}")
