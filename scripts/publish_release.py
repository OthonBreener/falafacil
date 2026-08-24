#!/usr/bin/env python3
"""Deterministic release state machine for GitHub Releases.

Handles:
- NEW release: create draft -> upload local pair -> download remote pair -> verify coherence and probe -> publish latest -> output sha256
- DRAFT release (empty): upload local pair -> download remote pair -> verify coherence and probe -> publish latest -> output sha256
- DRAFT release (raw-only): download remote raw -> verify probe -> derive missing tar from verified remote raw -> upload derived tar -> download remote pair -> verify coherence and probe -> publish latest -> output sha256
- DRAFT release (tar-only): download remote tar -> extract raw verifying tar structure -> verify coherence and probe -> upload derived raw -> download remote pair -> verify coherence and probe -> publish latest -> output sha256
- DRAFT release (complete): download remote pair -> verify coherence and probe -> publish latest -> output sha256
- PUBLISHED release (immutable retry): read-only download remote pair -> verify coherence and probe -> output sha256

Authoritative Remote State Rules:
- Releases on retry do NOT compare remote assets to a nondeterministic local rebuild.
- NEW / draft-empty: upload local pair.
- Draft raw-only: derives missing tar from verified remote raw.
- Draft tar-only: derives missing raw from verified remote tar.
- Draft-complete / published: use remote pair as authoritative.
- All remote paths verify filename, tar root/mode (0755), raw↔tar bytes equality, executable probe, SemVer/tag, and formula SHA from remote tar.
- Published release stays strictly read-only (no upload, create, or edit).
- Every gh transition failure is fail-closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Callable, NamedTuple, Protocol, Sequence

SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ReleaseError(Exception):
    """Base exception for release management errors."""


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]: ...


def default_command_runner(
    args: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        text=True,
        capture_output=capture_output,
        check=check,
    )


def validate_tag_and_version(tag: str, version: str) -> tuple[str, str]:
    clean_tag = tag.strip()
    clean_version = version.strip()

    if not TAG_PATTERN.match(clean_tag):
        raise ReleaseError(
            f"Tag inválida '{tag}'. Deve seguir o padrão 'vX.Y.Z' (ex: v0.2.0)."
        )
    if not SEMVER_PATTERN.match(clean_version):
        raise ReleaseError(
            f"Versão inválida '{version}'. Deve seguir o padrão 'X.Y.Z' (ex: 0.2.0)."
        )
    if clean_tag != f"v{clean_version}":
        raise ReleaseError(
            f"Tag '{clean_tag}' diverge da versão 'v{clean_version}'."
        )
    return clean_tag, clean_version


def compute_sha256(file_path: Path) -> str:
    if not file_path.is_file():
        raise ReleaseError(f"Arquivo não encontrado para cálculo de SHA-256: {file_path}")
    hasher = hashlib.sha256()
    with file_path.open("rb") as fp:
        while chunk := fp.read(65536):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if not SHA256_PATTERN.match(digest):
        raise ReleaseError(f"Cálculo de SHA-256 gerou hash inválido: {digest}")
    return digest


def create_tarball_from_raw(raw_path: Path, tar_path: Path) -> None:
    if not raw_path.is_file():
        raise ReleaseError(f"Asset raw não encontrado para criar tarball: {raw_path}")
    try:
        with tarfile.open(tar_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            tarinfo = tar.gettarinfo(str(raw_path), arcname="falafacil")
            tarinfo.mode = 0o755
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            with raw_path.open("rb") as fp:
                tar.addfile(tarinfo, fp)
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError(f"Erro ao criar tarball a partir de '{raw_path}': {exc}") from exc


def extract_raw_from_tarball(tar_path: Path, raw_path: Path) -> None:
    if not tar_path.is_file():
        raise ReleaseError(f"Asset tar não encontrado para extração: {tar_path}")
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) != 1:
                raise ReleaseError(
                    f"Tarball '{tar_path.name}' deve conter exatamente 1 arquivo na raiz, encontrado: {len(members)}"
                )
            member = members[0]
            if member.name != "falafacil" or not member.isfile():
                raise ReleaseError(
                    f"Tarball '{tar_path.name}' deve conter o arquivo 'falafacil' na raiz, encontrado: '{member.name}'"
                )
            if member.mode != 0o755:
                raise ReleaseError(
                    f"Arquivo 'falafacil' no tarball '{tar_path.name}' deve ter permissão exata 0755, encontrado: {oct(member.mode)}"
                )
            extracted_fp = tar.extractfile(member)
            if extracted_fp is None:
                raise ReleaseError(
                    f"Não foi possível ler o arquivo 'falafacil' de dentro do tarball '{tar_path.name}'"
                )
            raw_path.write_bytes(extracted_fp.read())
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError(f"Erro ao extrair raw do tarball '{tar_path.name}': {exc}") from exc
    raw_path.chmod(0o755)


def verify_tar_and_raw_coherence(tar_path: Path, raw_path: Path) -> None:
    if not tar_path.is_file():
        raise ReleaseError(f"Asset tar não encontrado para verificação: {tar_path}")
    if not raw_path.is_file():
        raise ReleaseError(f"Asset raw não encontrado para verificação: {raw_path}")

    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) != 1:
                raise ReleaseError(
                    f"Tarball '{tar_path.name}' deve conter exatamente 1 arquivo na raiz, encontrado: {len(members)}"
                )
            member = members[0]
            if member.name != "falafacil" or not member.isfile():
                raise ReleaseError(
                    f"Tarball '{tar_path.name}' deve conter o arquivo 'falafacil' na raiz, encontrado: '{member.name}'"
                )
            if member.mode != 0o755:
                raise ReleaseError(
                    f"Arquivo 'falafacil' no tarball '{tar_path.name}' deve ter permissão exata 0755, encontrado: {oct(member.mode)}"
                )
            extracted_fp = tar.extractfile(member)
            if extracted_fp is None:
                raise ReleaseError(
                    f"Não foi possível ler o arquivo 'falafacil' de dentro do tarball '{tar_path.name}'"
                )
            tar_member_bytes = extracted_fp.read()
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError(f"Erro ao inspecionar tarball '{tar_path.name}': {exc}") from exc

    raw_bytes = raw_path.read_bytes()
    if tar_member_bytes != raw_bytes:
        raise ReleaseError(
            f"Incoerência raw↔tar: o executável extraído de '{tar_path.name}' diverge do binário raw '{raw_path.name}'."
        )


def verify_executable_probe(
    executable_path: Path,
    version: str,
    runner: CommandRunner = default_command_runner,
) -> None:
    if not executable_path.is_file():
        raise ReleaseError(f"Executável não encontrado para probe: {executable_path}")

    current_mode = executable_path.stat().st_mode
    executable_path.chmod(current_mode | 0o755)

    try:
        result = runner(
            [str(executable_path), "--update-probe", version],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise ReleaseError(
            f"Falha ao executar probe do binário '{executable_path}': {exc}"
        ) from exc

    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip()
        raise ReleaseError(
            f"Probe do executável falhou para a versão '{version}' (código {result.returncode}): {error_msg}"
        )


class ReleaseState(NamedTuple):
    exists: bool
    is_draft: bool
    asset_names: frozenset[str]


def query_release_state(
    tag: str,
    gh_cmd: str,
    runner: CommandRunner,
) -> ReleaseState:
    try:
        result = runner(
            [gh_cmd, "release", "view", tag, "--json", "isDraft,tagName,assets"],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise ReleaseError(f"Falha ao invocar '{gh_cmd}': {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if not stderr:
            raise ReleaseError(
                f"Falha ao consultar release '{tag}': comando retornou código {result.returncode} com stderr vazio."
            )

        stderr_lower = " ".join(stderr.lower().split())
        clean_stderr = stderr_lower.removeprefix("error: ").strip()

        clean_tag_lower = tag.lower()
        normalized_exact_not_found = {
            "release not found",
            f"release '{clean_tag_lower}' not found",
            f"release \"{clean_tag_lower}\" not found",
            f"release {clean_tag_lower} not found",
            f"graphql: could not resolve to a release with the tag '{clean_tag_lower}'",
            f"graphql: could not resolve to a release with the tag \"{clean_tag_lower}\"",
            f"graphql: could not resolve to a release with the tag {clean_tag_lower}",
        }
        if clean_stderr in normalized_exact_not_found:
            return ReleaseState(exists=False, is_draft=False, asset_names=frozenset())

        raise ReleaseError(f"Falha ao consultar release '{tag}': {stderr}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"Resposta JSON inválida de '{gh_cmd} release view': {exc}") from exc

    if not isinstance(data, dict):
        raise ReleaseError(
            f"Resposta de '{gh_cmd} release view' deve ser um objeto JSON, recebido: {type(data).__name__}"
        )

    tag_name = data.get("tagName")
    if not isinstance(tag_name, str) or not tag_name.strip():
        raise ReleaseError(
            f"Resposta de '{gh_cmd} release view' não contém 'tagName' válido do tipo string não vazia."
        )
    if tag_name.strip() != tag:
        raise ReleaseError(
            f"Resposta de '{gh_cmd} release view' retornou tagName '{tag_name}' divergente da tag solicitada '{tag}'."
        )

    is_draft = data.get("isDraft")
    if not isinstance(is_draft, bool):
        raise ReleaseError(
            f"Resposta de '{gh_cmd} release view' campo 'isDraft' deve ser booleano, recebido: {type(is_draft).__name__}"
        )

    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ReleaseError(
            f"Resposta de '{gh_cmd} release view' campo 'assets' deve ser uma lista, recebido: {type(assets).__name__}"
        )

    asset_names: list[str] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(assets):
        if not isinstance(item, dict):
            raise ReleaseError(
                f"Item {idx} de assets na resposta de '{gh_cmd} release view' não é um objeto."
            )
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ReleaseError(
                f"Item {idx} de assets na resposta de '{gh_cmd} release view' não possui 'name' válido do tipo string não vazia."
            )
        clean_name = name.strip()
        if clean_name in seen_names:
            raise ReleaseError(
                f"Asset duplicado '{clean_name}' encontrado na resposta de '{gh_cmd} release view'."
            )
        seen_names.add(clean_name)
        asset_names.append(clean_name)

    return ReleaseState(exists=True, is_draft=is_draft, asset_names=frozenset(asset_names))


def publish_or_verify_release(
    *,
    tag: str,
    version: str,
    asset_raw: Path,
    asset_tar: Path,
    verify_dir: Path,
    github_output: Path | None = None,
    gh_cmd: str = "gh",
    runner: CommandRunner = default_command_runner,
) -> str:
    clean_tag, clean_version = validate_tag_and_version(tag, version)

    if not asset_raw.is_file():
        raise ReleaseError(f"Asset raw local não encontrado: {asset_raw}")
    if not asset_tar.is_file():
        raise ReleaseError(f"Asset tar local não encontrado: {asset_tar}")

    expected_raw_name = "falafacil-linux-x86_64"
    expected_tar_name = f"falafacil-{clean_version}-linux-x86_64.tar.gz"

    if asset_raw.name != expected_raw_name:
        raise ReleaseError(
            f"Nome do asset raw local '{asset_raw.name}' diverge do esperado '{expected_raw_name}'."
        )
    if asset_tar.name != expected_tar_name:
        raise ReleaseError(
            f"Nome do asset tar local '{asset_tar.name}' diverge do esperado '{expected_tar_name}'."
        )

    # 1. Consulta o estado atual da release
    state = query_release_state(clean_tag, gh_cmd, runner)

    # 2. Transições da máquina de estados
    if not state.exists:
        # Estado: NEW -> cria draft e envia o par local
        create_res = runner(
            [
                gh_cmd,
                "release",
                "create",
                clean_tag,
                "--draft",
                "--title",
                f"FalaFácil v{clean_version}",
                "--notes",
                f"Versão {clean_version} do FalaFácil para Ubuntu Linux x86_64.",
            ],
            check=False,
            capture_output=True,
        )
        if create_res.returncode != 0:
            raise ReleaseError(
                f"Falha ao criar draft release '{clean_tag}': {create_res.stderr.strip()}"
            )

        upload_res = runner(
            [gh_cmd, "release", "upload", clean_tag, str(asset_raw), str(asset_tar)],
            check=False,
            capture_output=True,
        )
        if upload_res.returncode != 0:
            raise ReleaseError(
                f"Falha ao fazer upload dos assets para '{clean_tag}': {upload_res.stderr.strip()}"
            )

    elif state.is_draft:
        # Estado: DRAFT (retomada com 0, 1 ou 2 assets presentes)
        has_raw = expected_raw_name in state.asset_names
        has_tar = expected_tar_name in state.asset_names

        if not has_raw and not has_tar:
            # Draft vazio: envia par local
            upload_res = runner(
                [gh_cmd, "release", "upload", clean_tag, str(asset_raw), str(asset_tar)],
                check=False,
                capture_output=True,
            )
            if upload_res.returncode != 0:
                raise ReleaseError(
                    f"Falha ao fazer upload dos assets para o draft '{clean_tag}': {upload_res.stderr.strip()}"
                )

        elif has_raw and not has_tar:
            # Draft raw-only: baixa raw remoto verificado, valida probe, deriva tarball e faz upload do tarball
            if verify_dir.exists():
                shutil.rmtree(verify_dir)
            verify_dir.mkdir(parents=True, exist_ok=True)

            dl_res = runner(
                [gh_cmd, "release", "download", clean_tag, "--dir", str(verify_dir)],
                check=False,
                capture_output=True,
            )
            if dl_res.returncode != 0:
                raise ReleaseError(
                    f"Falha ao baixar asset raw do draft '{clean_tag}': {dl_res.stderr.strip()}"
                )

            downloaded_raw = verify_dir / expected_raw_name
            if not downloaded_raw.is_file():
                raise ReleaseError(
                    f"Asset raw '{expected_raw_name}' não encontrado após download do draft '{clean_tag}'."
                )

            verify_executable_probe(downloaded_raw, clean_version, runner)

            derived_tar = verify_dir / expected_tar_name
            create_tarball_from_raw(downloaded_raw, derived_tar)

            upload_res = runner(
                [gh_cmd, "release", "upload", clean_tag, str(derived_tar)],
                check=False,
                capture_output=True,
            )
            if upload_res.returncode != 0:
                raise ReleaseError(
                    f"Falha ao enviar tarball derivado para o draft '{clean_tag}': {upload_res.stderr.strip()}"
                )

        elif has_tar and not has_raw:
            # Draft tar-only: baixa tar remoto, extrai raw, valida coerência/probe e faz upload do raw
            if verify_dir.exists():
                shutil.rmtree(verify_dir)
            verify_dir.mkdir(parents=True, exist_ok=True)

            dl_res = runner(
                [gh_cmd, "release", "download", clean_tag, "--dir", str(verify_dir)],
                check=False,
                capture_output=True,
            )
            if dl_res.returncode != 0:
                raise ReleaseError(
                    f"Falha ao baixar asset tar do draft '{clean_tag}': {dl_res.stderr.strip()}"
                )

            downloaded_tar = verify_dir / expected_tar_name
            if not downloaded_tar.is_file():
                raise ReleaseError(
                    f"Asset tar '{expected_tar_name}' não encontrado após download do draft '{clean_tag}'."
                )

            derived_raw = verify_dir / expected_raw_name
            extract_raw_from_tarball(downloaded_tar, derived_raw)
            verify_tar_and_raw_coherence(downloaded_tar, derived_raw)
            verify_executable_probe(derived_raw, clean_version, runner)

            upload_res = runner(
                [gh_cmd, "release", "upload", clean_tag, str(derived_raw)],
                check=False,
                capture_output=True,
            )
            if upload_res.returncode != 0:
                raise ReleaseError(
                    f"Falha ao enviar binário raw derivado para o draft '{clean_tag}': {upload_res.stderr.strip()}"
                )

        else:
            # Draft completo (has_raw and has_tar): assets remotos são autoritativos, nenhum upload necessário
            pass

    else:
        # Estado: PUBLISHED (release imutável já publicada)
        # Modo estritamente somente leitura: nenhum upload ou edição
        if expected_raw_name not in state.asset_names or expected_tar_name not in state.asset_names:
            raise ReleaseError(
                f"Release publicada '{clean_tag}' não contém ambos os assets esperados ({expected_raw_name}, {expected_tar_name})."
            )
    # 3. Download completo e verificação do par remoto autoritativo
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    verify_dir.mkdir(parents=True, exist_ok=True)

    dl_res = runner(
        [gh_cmd, "release", "download", clean_tag, "--dir", str(verify_dir)],
        check=False,
        capture_output=True,
    )
    if dl_res.returncode != 0:
        raise ReleaseError(
            f"Falha no download dos assets de '{clean_tag}' para verificação: {dl_res.stderr.strip()}"
        )

    downloaded_raw = verify_dir / expected_raw_name
    downloaded_tar = verify_dir / expected_tar_name

    if not downloaded_raw.is_file():
        raise ReleaseError(
            f"Asset raw '{expected_raw_name}' ausente no download remoto de '{clean_tag}'."
        )
    if not downloaded_tar.is_file():
        raise ReleaseError(
            f"Asset tar '{expected_tar_name}' ausente no download remoto de '{clean_tag}'."
        )

    # Verificação de estrutura do tarball e coerência raw↔tar entre os assets remotos
    verify_tar_and_raw_coherence(downloaded_tar, downloaded_raw)

    # Verificação de probe executável no binário remoto
    verify_executable_probe(downloaded_raw, clean_version, runner)

    # 4. Se for NEW ou DRAFT, publica como latest APÓS toda a verificação ter passado com sucesso
    if not state.exists or state.is_draft:
        edit_res = runner(
            [gh_cmd, "release", "edit", clean_tag, "--draft=false", "--latest"],
            check=False,
            capture_output=True,
        )
        if edit_res.returncode != 0:
            raise ReleaseError(
                f"Falha ao publicar release '{clean_tag}' como latest: {edit_res.stderr.strip()}"
            )

    # 5. Cálculo do SHA-256 do tarball remoto verificado
    sha256 = compute_sha256(downloaded_tar)

    if github_output is not None:
        try:
            with github_output.open("a", encoding="utf-8") as fp:
                fp.write(f"sha256={sha256}\n")
        except OSError as exc:
            raise ReleaseError(f"Erro ao gravar GITHUB_OUTPUT: {exc}") from exc

    return sha256


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publica ou verifica idempotentemente releases no GitHub Releases."
    )
    parser.add_argument("--tag", required=True, help="Tag da release (ex: v0.2.0)")
    parser.add_argument("--version", required=True, help="Versão SemVer (ex: 0.2.0)")
    parser.add_argument("--asset-raw", required=True, type=Path, help="Caminho do asset binário raw")
    parser.add_argument("--asset-tar", required=True, type=Path, help="Caminho do asset tarball")
    parser.add_argument(
        "--verify-dir",
        type=Path,
        default=Path("verify_download"),
        help="Diretório temporário para download e verificação de integridade",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Caminho do arquivo GITHUB_OUTPUT para registrar o hash sha256",
    )
    parser.add_argument(
        "--gh-cmd",
        default="gh",
        help="Comando ou caminho do executável do GitHub CLI (padrão: gh)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sha256 = publish_or_verify_release(
            tag=args.tag,
            version=args.version,
            asset_raw=args.asset_raw,
            asset_tar=args.asset_tar,
            verify_dir=args.verify_dir,
            github_output=args.github_output,
            gh_cmd=args.gh_cmd,
        )
        sys.stdout.write(f"sha256={sha256}\n")
    except ReleaseError as exc:
        sys.stderr.write(f"Erro no gerenciador de release: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"Erro inesperado: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
