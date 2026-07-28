#!/usr/bin/env python3
"""
OCR local com Amazon Textract (API síncrona).

Extrai texto de imagens e PDFs de até 5 MB, usando DetectDocumentText
ou AnalyzeDocument (FORMS / TABLES). Credenciais vêm do ambiente ou da AWS CLI —
nunca hardcode no código.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
)

# Extensões aceitas pela API síncrona deste projeto (bytes locais ≤ 5 MB)
EXTENSOES_SUPORTADAS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf"}
TAMANHO_MAXIMO_BYTES = 5 * 1024 * 1024  # 5 MB
FEATURES_VALIDAS = {"FORMS", "TABLES"}


def ler_documento(caminho: Path) -> bytes:
    """Lê o arquivo local e devolve o conteúdo em bytes."""
    try:
        return caminho.read_bytes()
    except OSError as exc:
        raise ValueError(f"Não foi possível ler o arquivo '{caminho}': {exc}") from exc


def criar_client_textract(region: str | None = None):
    """
    Cria o client do Amazon Textract.

    Credenciais e região seguem o padrão da AWS SDK:
    variáveis de ambiente, AWS_PROFILE ou ~/.aws/credentials.
    """
    kwargs: dict[str, str] = {}
    if region:
        kwargs["region_name"] = region
    return boto3.client("textract", **kwargs)


def extrair_linhas(resposta: dict[str, Any]) -> list[str]:
    """Extrai o texto das linhas (BlockType == LINE) da resposta do Textract."""
    linhas: list[str] = []
    for bloco in resposta.get("Blocks", []):
        if bloco.get("BlockType") == "LINE" and bloco.get("Text"):
            linhas.append(bloco["Text"])
    return linhas


def _mapa_blocos(resposta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Indexa blocos por Id para navegar relações KEY_VALUE_SET / CELL."""
    return {bloco["Id"]: bloco for bloco in resposta.get("Blocks", []) if "Id" in bloco}


def _texto_do_bloco(bloco: dict[str, Any], por_id: dict[str, dict[str, Any]]) -> str:
    """Monta o texto de um bloco a partir de WORD/CHILD relacionados."""
    if bloco.get("Text"):
        return bloco["Text"]

    partes: list[str] = []
    for relacao in bloco.get("Relationships", []):
        if relacao.get("Type") != "CHILD":
            continue
        for child_id in relacao.get("Ids", []):
            filho = por_id.get(child_id)
            if not filho:
                continue
            if filho.get("BlockType") == "WORD" and filho.get("Text"):
                partes.append(filho["Text"])
            elif filho.get("BlockType") == "SELECTION_ELEMENT":
                status = filho.get("SelectionStatus", "")
                partes.append("[X]" if status == "SELECTED" else "[ ]")
    return " ".join(partes).strip()


def extrair_formularios(resposta: dict[str, Any]) -> list[dict[str, str]]:
    """
    Extrai pares chave/valor de AnalyzeDocument com feature FORMS.

    Retorna uma lista de dicionários com as chaves: chave, valor, confianca.
    """
    por_id = _mapa_blocos(resposta)
    campos: list[dict[str, str]] = []

    for bloco in resposta.get("Blocks", []):
        if bloco.get("BlockType") != "KEY_VALUE_SET":
            continue
        if "KEY" not in bloco.get("EntityTypes", []):
            continue

        chave = _texto_do_bloco(bloco, por_id)
        valor = ""
        for relacao in bloco.get("Relationships", []):
            if relacao.get("Type") != "VALUE":
                continue
            for value_id in relacao.get("Ids", []):
                valor_bloco = por_id.get(value_id)
                if valor_bloco:
                    valor = _texto_do_bloco(valor_bloco, por_id)
                    break

        confianca = bloco.get("Confidence")
        campos.append(
            {
                "chave": chave,
                "valor": valor,
                "confianca": f"{confianca:.2f}" if confianca is not None else "",
            }
        )

    return campos


def extrair_tabelas(resposta: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extrai tabelas de AnalyzeDocument com feature TABLES.

    Cada tabela vira um dicionário com índice e matriz de células (linhas × colunas).
    """
    por_id = _mapa_blocos(resposta)
    tabelas: list[dict[str, Any]] = []

    for indice, bloco in enumerate(resposta.get("Blocks", []), start=1):
        if bloco.get("BlockType") != "TABLE":
            continue

        celulas: list[dict[str, Any]] = []
        max_linha = 0
        max_coluna = 0

        for relacao in bloco.get("Relationships", []):
            if relacao.get("Type") != "CHILD":
                continue
            for child_id in relacao.get("Ids", []):
                celula = por_id.get(child_id)
                if not celula or celula.get("BlockType") != "CELL":
                    continue
                linha = int(celula.get("RowIndex", 0))
                coluna = int(celula.get("ColumnIndex", 0))
                max_linha = max(max_linha, linha)
                max_coluna = max(max_coluna, coluna)
                celulas.append(
                    {
                        "linha": linha,
                        "coluna": coluna,
                        "texto": _texto_do_bloco(celula, por_id),
                    }
                )

        matriz = [["" for _ in range(max_coluna)] for _ in range(max_linha)]
        for celula in celulas:
            r = celula["linha"] - 1
            c = celula["coluna"] - 1
            if r >= 0 and c >= 0:
                matriz[r][c] = celula["texto"]

        tabelas.append(
            {
                "indice": indice,
                "linhas": max_linha,
                "colunas": max_coluna,
                "matriz": matriz,
            }
        )

    # Reindexa tabelas apenas com as encontradas (índice sequencial 1..N)
    for i, tabela in enumerate(tabelas, start=1):
        tabela["indice"] = i

    return tabelas


def salvar_saidas(
    base_saida: Path,
    resposta: dict[str, Any],
    linhas: list[str],
    formularios: list[dict[str, str]] | None = None,
    tabelas: list[dict[str, Any]] | None = None,
) -> dict[str, Path]:
    """
    Persiste os resultados em arquivos locais.

    Sempre gera JSON (resposta completa) e TXT (linhas).
    Com FORMS gera CSV; com TABLES gera JSON das tabelas.
    """
    base_saida.parent.mkdir(parents=True, exist_ok=True)

    caminhos: dict[str, Path] = {}

    caminho_json = Path(f"{base_saida}.json")
    caminho_json.write_text(
        json.dumps(resposta, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    caminhos["json"] = caminho_json

    caminho_txt = Path(f"{base_saida}.txt")
    caminho_txt.write_text("\n".join(linhas) + ("\n" if linhas else ""), encoding="utf-8")
    caminhos["txt"] = caminho_txt

    if formularios is not None:
        caminho_csv = Path(f"{base_saida}_forms.csv")
        with caminho_csv.open("w", encoding="utf-8", newline="") as arquivo:
            writer = csv.DictWriter(arquivo, fieldnames=["chave", "valor", "confianca"])
            writer.writeheader()
            writer.writerows(formularios)
        caminhos["forms_csv"] = caminho_csv

    if tabelas is not None:
        caminho_tabelas = Path(f"{base_saida}_tables.json")
        caminho_tabelas.write_text(
            json.dumps(tabelas, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        caminhos["tables_json"] = caminho_tabelas

    return caminhos


def validar_arquivo(caminho: Path) -> None:
    """Valida existência, extensão suportada e limite de 5 MB."""
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")

    if not caminho.is_file():
        raise ValueError(f"O caminho informado não é um arquivo: {caminho}")

    extensao = caminho.suffix.lower()
    if extensao not in EXTENSOES_SUPORTADAS:
        suportadas = ", ".join(sorted(EXTENSOES_SUPORTADAS))
        raise ValueError(
            f"Extensão '{extensao}' não suportada. Use uma destas: {suportadas}"
        )

    tamanho = caminho.stat().st_size
    if tamanho == 0:
        raise ValueError(f"O arquivo está vazio: {caminho}")

    if tamanho > TAMANHO_MAXIMO_BYTES:
        tamanho_mb = tamanho / (1024 * 1024)
        raise ValueError(
            f"Arquivo com {tamanho_mb:.2f} MB excede o limite de 5 MB da API síncrona. "
            "Reduza o arquivo ou use a API assíncrona (fora do escopo deste projeto)."
        )


def parse_features(valores: list[str] | None) -> list[str]:
    """Normaliza e valida as features FORMS/TABLES."""
    if not valores:
        return []

    features: list[str] = []
    for item in valores:
        for parte in item.replace(",", " ").split():
            feature = parte.strip().upper()
            if feature not in FEATURES_VALIDAS:
                raise ValueError(
                    f"Feature inválida: '{parte}'. Use FORMS e/ou TABLES."
                )
            if feature not in features:
                features.append(feature)
    return features


def chamar_textract(
    client: Any,
    documento_bytes: bytes,
    features: list[str],
) -> dict[str, Any]:
    """Chama DetectDocumentText ou AnalyzeDocument conforme as features."""
    documento = {"Bytes": documento_bytes}

    if features:
        return client.analyze_document(
            Document=documento,
            FeatureTypes=features,
        )

    return client.detect_document_text(Document=documento)


def mensagem_erro_aws(exc: Exception) -> str:
    """Traduz erros comuns da AWS/boto3 em mensagens amigáveis."""
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return (
            "Credenciais AWS não encontradas ou incompletas.\n"
            "Configure via AWS CLI (`aws configure`) ou variáveis de ambiente:\n"
            "  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION\n"
            "  (opcional) AWS_PROFILE"
        )

    if isinstance(exc, EndpointConnectionError):
        return (
            "Não foi possível conectar ao endpoint do Textract.\n"
            "Verifique a região (--region / AWS_DEFAULT_REGION) e sua conexão de rede."
        )

    if isinstance(exc, ClientError):
        erro = exc.response.get("Error", {})
        codigo = erro.get("Code", "Unknown")
        mensagem = erro.get("Message", str(exc))

        if codigo in {"UnrecognizedClientException", "InvalidClientTokenId", "AuthFailure"}:
            return (
                f"Falha de autenticação ({codigo}): {mensagem}\n"
                "Revise as credenciais e o perfil AWS."
            )
        if codigo in {"AccessDeniedException", "AccessDenied"}:
            return (
                f"Permissão negada ({codigo}): {mensagem}\n"
                "A identidade IAM precisa de permissão para textract:DetectDocumentText "
                "e/ou textract:AnalyzeDocument."
            )
        if codigo in {"InvalidS3ObjectException", "InvalidParameterException", "UnsupportedDocumentException"}:
            return f"Arquivo ou parâmetro inválido para o Textract ({codigo}): {mensagem}"
        if codigo == "ProvisionedThroughputExceededException":
            return f"Limite de throughput excedido ({codigo}). Aguarde e tente novamente."
        if "region" in mensagem.lower() or codigo == "InvalidSignatureException":
            return (
                f"Erro relacionado à região ou assinatura ({codigo}): {mensagem}\n"
                "Confirme se a região está correta e habilitada para Textract."
            )
        return f"Erro da API Textract ({codigo}): {mensagem}"

    if isinstance(exc, BotoCoreError):
        return f"Erro no SDK AWS (boto3/botocore): {exc}"

    return f"Erro inesperado: {exc}"


def montar_argumentos() -> argparse.Namespace:
    """Configura a interface de linha de comando."""
    parser = argparse.ArgumentParser(
        description=(
            "Extrai texto de imagens/documentos locais com Amazon Textract "
            "(API síncrona, até 5 MB)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python scripts/ocr.py images/sample/documento.png\n"
            "  python scripts/ocr.py images/sample/formulario.jpg --features FORMS\n"
            "  python scripts/ocr.py images/sample/tabela.png --features TABLES --out output/tabela\n"
            "  python scripts/ocr.py doc.pdf --features FORMS TABLES --region us-east-1\n"
        ),
    )
    parser.add_argument(
        "arquivo",
        help="Caminho da imagem ou PDF local (.png, .jpg, .jpeg, .tif, .tiff, .pdf)",
    )
    parser.add_argument(
        "--features",
        nargs="+",
        metavar="FEATURE",
        help="Features do AnalyzeDocument: FORMS e/ou TABLES. Sem esta opção, usa DetectDocumentText.",
    )
    parser.add_argument(
        "--out",
        dest="saida",
        default=None,
        help="Caminho base dos arquivos de saída (sem extensão). Padrão: output/<nome_do_arquivo>",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Região AWS (ex.: us-east-1). Se omitido, usa AWS_DEFAULT_REGION / config local.",
    )
    return parser.parse_args()


def main() -> int:
    """Ponto de entrada do CLI."""
    args = montar_argumentos()

    try:
        caminho = Path(args.arquivo).expanduser().resolve()
        validar_arquivo(caminho)
        features = parse_features(args.features)

        if args.saida:
            base_saida = Path(args.saida).expanduser()
            if not base_saida.is_absolute():
                base_saida = Path.cwd() / base_saida
        else:
            base_saida = Path.cwd() / "output" / caminho.stem

        print(f"Arquivo : {caminho}")
        print(f"Tamanho : {caminho.stat().st_size / 1024:.1f} KB")
        print(f"Modo    : {'AnalyzeDocument (' + ', '.join(features) + ')' if features else 'DetectDocumentText'}")
        print(f"Região  : {args.region or '(padrão do ambiente/CLI)'}")
        print("Enviando documento ao Amazon Textract...")

        documento_bytes = ler_documento(caminho)
        client = criar_client_textract(args.region)
        resposta = chamar_textract(client, documento_bytes, features)

        linhas = extrair_linhas(resposta)
        formularios = extrair_formularios(resposta) if "FORMS" in features else None
        tabelas = extrair_tabelas(resposta) if "TABLES" in features else None

        caminhos = salvar_saidas(
            base_saida=base_saida,
            resposta=resposta,
            linhas=linhas,
            formularios=formularios,
            tabelas=tabelas,
        )

        print("\nConcluído com sucesso.")
        print(f"Linhas extraídas: {len(linhas)}")
        if formularios is not None:
            print(f"Campos (FORMS): {len(formularios)}")
        if tabelas is not None:
            print(f"Tabelas (TABLES): {len(tabelas)}")

        print("\nArquivos gerados:")
        for tipo, path in caminhos.items():
            print(f"  - [{tipo}] {path}")

        return 0

    except (FileNotFoundError, ValueError) as exc:
        print(f"\nErro de validação: {exc}", file=sys.stderr)
        return 1
    except (NoCredentialsError, PartialCredentialsError, ClientError, EndpointConnectionError, BotoCoreError) as exc:
        print(f"\n{mensagem_erro_aws(exc)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nOperação cancelada pelo usuário.", file=sys.stderr)
        return 130
    except Exception as exc:  # salvaguarda final
        print(f"\n{mensagem_erro_aws(exc)}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
