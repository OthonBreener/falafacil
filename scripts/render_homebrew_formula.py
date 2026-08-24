#!/usr/bin/env python3
"""Render Homebrew formula template with version and sha256."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
UNRESOLVED_PLACEHOLDER_PATTERN = re.compile(r"@[A-Z0-9_]+@")


def validate_version(version: str) -> str:
    cleaned = version.strip()
    if not SEMVER_PATTERN.match(cleaned):
        raise ValueError(
            f"Versão inválida '{version}'. Deve ser SemVer simples (ex: 0.2.0), sem 'v' ou pré-lançamento."
        )
    return cleaned


def validate_sha256(sha256: str) -> str:
    cleaned = sha256.strip().lower()
    if not SHA256_PATTERN.match(cleaned):
        raise ValueError(
            f"SHA-256 inválido '{sha256}'. Deve conter exatamente 64 caracteres hexadecimais."
        )
    return cleaned


def render_formula(template_content: str, version: str, sha256: str) -> str:
    valid_version = validate_version(version)
    valid_sha256 = validate_sha256(sha256)

    if "@VERSION@" not in template_content:
        raise ValueError("Template não contém o placeholder obrigatório '@VERSION@'.")
    if "@SHA256@" not in template_content:
        raise ValueError("Template não contém o placeholder obrigatório '@SHA256@'.")

    rendered = template_content.replace("@VERSION@", valid_version)
    rendered = rendered.replace("@SHA256@", valid_sha256)

    remaining_placeholders = UNRESOLVED_PLACEHOLDER_PATTERN.findall(rendered)
    if remaining_placeholders:
        raise ValueError(
            f"Placeholders não resolvidos encontrados no template renderizado: {sorted(set(remaining_placeholders))}"
        )

    return rendered


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Renderiza a fórmula Homebrew a partir de template, versão e sha256."
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Versão SemVer simples (ex: 0.2.0)",
    )
    parser.add_argument(
        "--sha256",
        required=True,
        help="SHA-256 hexadecimal de 64 caracteres do tarball de release",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Caminho de saída da fórmula renderizada (ex: Formula/falafacil.rb)",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Caminho do template da fórmula (padrão: packaging/homebrew/falafacil.rb.in)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    template_path = args.template
    if template_path is None:
        repo_root = Path(__file__).resolve().parent.parent
        template_path = repo_root / "packaging" / "homebrew" / "falafacil.rb.in"

    if not template_path.is_file():
        sys.stderr.write(f"Arquivo de template não encontrado: {template_path}\n")
        return 1

    try:
        template_content = template_path.read_text(encoding="utf-8")
        rendered = render_formula(
            template_content=template_content,
            version=args.version,
            sha256=args.sha256,
        )
        output_path = args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    except ValueError as exc:
        sys.stderr.write(f"Erro ao renderizar fórmula: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"Erro de E/S: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
