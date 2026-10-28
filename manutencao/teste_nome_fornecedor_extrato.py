"""Testa se a IA de texto (sem Vision) extrai so o nome do fornecedor do `detalhamento` dos extratos do BB.

Le os xlsx de extrato (so debitos), tira os textos distintos e imprime lado a lado o que uma regex simples
e o Claude acharam. Compara os dois com um gabarito pequeno. So le e imprime: nao grava no banco nem
mexe em arquivos. Cada lote chama a API (custo); `--sem-ia` roda so a regex.

    python -m manutencao.teste_nome_fornecedor_extrato
    python -m manutencao.teste_nome_fornecedor_extrato --sem-ia
    python -m manutencao.teste_nome_fornecedor_extrato --modelo claude-opus-5-5 --limite 40
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import anthropic
import openpyxl

from domain.config import carregar_config

PASTA_EXTRATOS = Path("C:/Users/Cauet/OneDrive/Documents/Arquivos_finalizados/BB")
TEXTOS_POR_LOTE = 60

# Colunas do extrato BB (sem cabecalho): historico, deb_cred e detalhamento.
COL_HISTORICO, COL_DEB_CRED, COL_DETALHAMENTO = 9, 11, 12

# Casos vistos nos extratos reais: texto do detalhamento -> nome esperado do fornecedor.
GABARITO = {
    "60300166 RAFAEL SILVA TAVARES": "RAFAEL SILVA TAVARES",
    "17/07 16:13 SC C P I E EIRELI": "SC C P I E EIRELI",
    "16/07 17:11 LOG PARTS DISTRIBUIDORA": "LOG PARTS DISTRIBUIDORA",
    "341 4893 11201833000137 CENTRAIS EOLIC": "CENTRAIS EOLIC",
    "033 2271 01937635000182 SONDA SUPERMER": "SONDA SUPERMER",
    "16/07 10:57 18507955000103 AGIL MOTORS": "AGIL MOTORS",
    "PORTORROL DISTRIBUIDORA LTDA": "PORTORROL DISTRIBUIDORA LTDA",
    "DEUTSH LAMP COM DE MATERIAIS E": "DEUTSH LAMP COM DE MATERIAIS E",
    "NAIP": "NAIP",
}

PROMPT = """Cada item da lista abaixo e o campo "detalhamento" de um extrato bancario, truncado em 40 caracteres.
Ele traz o nome do fornecedor (ou favorecido), as vezes com lixo na frente: codigo do banco, agencia, CNPJ,
numero de conta ou documento, data e hora.

Para cada item devolva SO o nome do fornecedor, exatamente como esta escrito no texto:
- tire numeros de banco, agencia, CNPJ, conta, documento, data e hora;
- nao complete, nao corrija e nao traduza nomes truncados;
- se nao houver nome de empresa ou pessoa, devolva "".

Responda apenas com um array JSON de strings, na mesma ordem e com o mesmo tamanho da lista, sem texto extra.

Lista:
"""


def listar_textos_de_debito(pasta: Path) -> list[str]:
    """Le os xlsx da pasta e devolve os textos distintos de detalhamento dos debitos."""
    textos = set()
    for arquivo in sorted(pasta.glob("*.xlsx")):
        if arquivo.name.startswith("~$"):  # arquivo de bloqueio do Excel
            continue
        planilha = openpyxl.load_workbook(arquivo, read_only=True, data_only=True).active
        for linha in planilha.iter_rows(values_only=True):
            if len(linha) > COL_DETALHAMENTO and linha[COL_DEB_CRED] == "D" and linha[COL_DETALHAMENTO]:
                textos.add(" ".join(str(linha[COL_DETALHAMENTO]).split()))
    return sorted(textos)


def extrair_nome_por_regex(texto: str) -> str:
    """Tira da frente do texto os pedacos so de numero, data e hora e devolve o resto."""
    palavras = texto.split()
    while palavras and re.fullmatch(r"[\d/:.\-]+", palavras[0]):
        palavras.pop(0)
    return " ".join(palavras)


def extrair_nomes_por_ia(textos: list[str], api_key: str, modelo: str) -> list[str]:
    """Pede ao Claude o nome do fornecedor de cada texto, em lotes, e devolve a lista na mesma ordem."""
    cliente = anthropic.Anthropic(api_key=api_key)
    nomes: list[str] = []
    for inicio in range(0, len(textos), TEXTOS_POR_LOTE):
        lote = textos[inicio:inicio + TEXTOS_POR_LOTE]
        resposta = cliente.messages.create(
            model=modelo,
            max_tokens=4096,
            messages=[{"role": "user", "content": PROMPT + json.dumps(lote, ensure_ascii=False)}],
        )
        bruto = resposta.content[0].text.strip()
        bruto = re.sub(r"^```(?:json)?|```$", "", bruto, flags=re.MULTILINE).strip()
        devolvidos = json.loads(bruto)
        if len(devolvidos) != len(lote):
            raise ValueError(f"Lote com {len(lote)} textos voltou com {len(devolvidos)} respostas")
        nomes.extend(str(nome).strip() for nome in devolvidos)
        print(f"  lote {inicio // TEXTOS_POR_LOTE + 1}: {len(lote)} textos, {resposta.usage.input_tokens} tokens de entrada")
    return nomes


def normalizar(nome: str) -> str:
    """Deixa o nome em maiusculas e com espacos simples, para comparar."""
    return " ".join(nome.upper().split())


def imprimir_comparacao(textos: list[str], por_regex: list[str], por_ia: list[str] | None) -> None:
    """Imprime cada texto com o que a regex e a IA acharam, marcando onde os dois discordam."""
    for indice, texto in enumerate(textos):
        marca = ""
        if por_ia is not None and normalizar(por_regex[indice]) != normalizar(por_ia[indice]):
            marca = "  <-- DIFERENTE"
        print(f"\n{texto!r}{marca}")
        print(f"  regex: {por_regex[indice]!r}")
        if por_ia is not None:
            print(f"  ia   : {por_ia[indice]!r}")


def conferir_gabarito(api_key: str | None, modelo: str) -> None:
    """Roda regex e IA nos casos do GABARITO e imprime o acerto de cada metodo."""
    textos = list(GABARITO)
    esperados = [normalizar(nome) for nome in GABARITO.values()]
    por_regex = [normalizar(extrair_nome_por_regex(texto)) for texto in textos]
    print(f"\n=== GABARITO ({len(textos)} casos)")
    print(f"  regex acertou {sum(a == b for a, b in zip(por_regex, esperados))}/{len(textos)}")
    if api_key is not None:
        por_ia = [normalizar(nome) for nome in extrair_nomes_por_ia(textos, api_key, modelo)]
        print(f"  ia    acertou {sum(a == b for a, b in zip(por_ia, esperados))}/{len(textos)}")
        for texto, esperado, regex, ia in zip(textos, esperados, por_regex, por_ia):
            if regex != esperado or ia != esperado:
                print(f"  erro em {texto!r}: esperado {esperado!r} | regex {regex!r} | ia {ia!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pasta", default=str(PASTA_EXTRATOS), help="pasta com os xlsx de extrato do BB")
    parser.add_argument("--modelo", help="modelo da IA; padrao o do profile (VISION_MODEL)")
    parser.add_argument("--limite", type=int, help="usa so os N primeiros textos (menos custo)")
    parser.add_argument("--sem-ia", action="store_true", help="roda so a regex, sem chamar a API")
    args = parser.parse_args()

    textos = listar_textos_de_debito(Path(args.pasta))
    if args.limite:
        textos = textos[:args.limite]
    if not textos:
        print(f"Nenhum debito com detalhamento em {args.pasta}")
        return

    vision = carregar_config().vision
    modelo = args.modelo or vision.modelo
    api_key = None if args.sem_ia else vision.api_key
    print(f"{len(textos)} texto(s) distinto(s) de {args.pasta} - " + ("so regex" if args.sem_ia else f"modelo {modelo}"))

    por_regex = [extrair_nome_por_regex(texto) for texto in textos]
    por_ia = None if args.sem_ia else extrair_nomes_por_ia(textos, api_key, modelo)
    imprimir_comparacao(textos, por_regex, por_ia)
    conferir_gabarito(api_key, modelo)


if __name__ == "__main__":
    main()
