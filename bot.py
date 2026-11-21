from __future__ import annotations

import asyncio
import io
import hashlib
import json
import logging
import os
import random
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, unquote, quote
import ipaddress

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

import discord
from discord import app_commands
from dotenv import load_dotenv



BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
ARQUIVO_PARTIDAS = Path("/data/partidas.json")
ARQUIVO_FILAS = Path("/data/filas.json")
ARQUIVO_IMAGENS_FILA = Path("/data/queue_imagens.json")
ARQUIVO_CORES_FILA = Path("/data/queue_cores.json")
ARQUIVO_PRESENCA = Path("/data/bot_presenca.json")
PASTA_IMAGENS_FILA = Path("/data/queue_imagens")
TAMANHO_TIME = 5
# Modos do /queue: valor -> (rótulo, nº de equipes, jogadores por equipe).
MODOS_FILA = {
    "2x2": ("⚔️ 2 vs 2", 2, 2),
    "2x3": ("⚔️ 3 vs 3", 2, 3),
    "2x4": ("⚔️ 4 vs 4", 2, 4),
    "2x5": ("⚔️ 5 vs 5 (2 equipes)", 2, 5),
    "3x5": ("🏅 3 equipes de 5", 3, 5),
    "4x5": ("🏅 4 equipes de 5", 4, 5),
    "5x5": ("🏅 5 equipes de 5", 5, 5),
    "6x5": ("🏅 6 equipes de 5", 6, 5),
}
# De 2 a 6 equipes de cinco participantes; o painel visual tem SEMPRE duas colunas.
EQUIPES = (
    ("vermelho", "🔴", "Vermelho"),
    ("azul", "🔵", "Azul"),
    ("verde", "🟢", "Verde"),
    ("amarelo", "🟡", "Amarelo"),
    ("roxo", "🟣", "Roxo"),
    ("laranja", "🟠", "Laranja"),
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot-partidas")


def carregar_partidas() -> dict[str, dict[str, Any]]:
    if not ARQUIVO_PARTIDAS.exists():
        return {}
    try:
        dados = json.loads(ARQUIVO_PARTIDAS.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("O conteúdo precisa ser um objeto JSON.")
        return dados
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        raise SystemExit(
            "Não foi possível ler partidas.json. Preserve o arquivo e corrija-o "
            f"antes de iniciar o bot: {exc}"
        ) from exc


partidas = carregar_partidas()
# Várias partidas por servidor: cada uma guarda o servidor e um número próprio.
# Partidas do formato antigo (chave = ID do servidor) recebem esses dados aqui.
_partidas_convertidas = False
for _chave_partida, _partida in partidas.items():
    if "guild_id" not in _partida:
        _partida["guild_id"] = int(_chave_partida)
        _partidas_convertidas = True
    if "numero" not in _partida:
        _partida["numero"] = 1
        _partidas_convertidas = True


def carregar_filas() -> dict[str, dict[str, Any]]:
    if not ARQUIVO_FILAS.exists():
        return {}
    try:
        dados = json.loads(ARQUIVO_FILAS.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("A raiz deve ser um objeto JSON.")
        # Cada fila é guardada pelo ID da sua mensagem, permitindo várias por servidor.
        # Formato antigo (uma fila por servidor, chave = ID do servidor) é convertido.
        convertidas: dict[str, dict[str, Any]] = {}
        for chave, fila in dados.items():
            if not isinstance(fila, dict):
                continue
            if "guild_id" not in fila:
                fila["guild_id"] = int(chave)
                if not fila.get("message_id"):
                    continue
                chave = str(fila["message_id"])
            convertidas[chave] = fila
        return convertidas
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        raise SystemExit(f"Erro ao carregar filas.json: {exc}") from exc


filas = carregar_filas()


def carregar_config_imagens() -> dict[str, dict[str, str]]:
    if not ARQUIVO_IMAGENS_FILA.exists():
        return {}
    try:
        dados = json.loads(ARQUIVO_IMAGENS_FILA.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("Configuração inválida")
        return dados
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        raise SystemExit(f"Erro ao ler queue_imagens.json: {exc}") from exc


imagens_fila = carregar_config_imagens()

# Cores do embed do painel, configuradas por servidor e persistidas no FadeHost.
CORES_PREDEFINIDAS = (
    ("🟣 Roxo", "#9B59B6"),
    ("🔵 Azul", "#5865F2"),
    ("🔴 Vermelho", "#ED4245"),
    ("🟢 Verde", "#57F287"),
    ("🟡 Amarelo", "#FEE75C"),
    ("🟠 Laranja", "#F39C12"),
    ("🌸 Rosa", "#EB459E"),
    ("⚪ Branco", "#FFFFFF"),
    ("⚫ Escuro", "#23272A"),
)


def normalizar_cor_hex(texto: str) -> str:
    """Converte #RRGGBB ou RRGGBB em #RRGGBB, recusando formatos inválidos."""
    valor = (texto or "").strip().upper()
    if not re.fullmatch(r"#?[0-9A-F]{6}", valor):
        raise ValueError("Use uma cor HEX de seis dígitos. Exemplo: #FF1493.")
    return "#" + valor.lstrip("#")


def carregar_cores_fila() -> dict[str, str]:
    if not ARQUIVO_CORES_FILA.exists():
        return {}
    try:
        dados = json.loads(ARQUIVO_CORES_FILA.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("A configuração de cores deve ser um objeto JSON.")
        return {str(gid): normalizar_cor_hex(cor) for gid, cor in dados.items()}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Erro ao ler queue_cores.json: {exc}") from exc


cores_fila = carregar_cores_fila()


def salvar_cores_fila() -> None:
    ARQUIVO_CORES_FILA.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_CORES_FILA.with_suffix(".json.tmp")
    temporario.write_text(
        json.dumps(cores_fila, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(ARQUIVO_CORES_FILA)


def cor_do_embed_fila(fila: dict[str, Any]) -> int:
    """Retorna a cor customizada ou a cor original do painel da fila."""
    personalizado = fila.get("cor_embed")
    if personalizado:
        try:
            return int(normalizar_cor_hex(personalizado)[1:], 16)
        except ValueError:
            log.warning("Cor de fila inválida, usando cor padrão: %s", personalizado)
    return 0x22C55E if fila_cheia(fila) else 0x5865F2


def salvar_config_imagens() -> None:
    ARQUIVO_IMAGENS_FILA.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_IMAGENS_FILA.with_suffix(".json.tmp")
    temporario.write_text(
        json.dumps(imagens_fila, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(ARQUIVO_IMAGENS_FILA)


def guardar_imagem_fila(guild_id: int, dados: bytes) -> str:
    """Salva a imagem completa no volume persistente do FadeHost, com verificação."""
    formato = tipo_imagem(dados)
    if formato is None or not dados or len(dados) > LIMITE_BYTES:
        raise ValueError("Use PNG, JPG, GIF ou WEBP de até 10 MB.")
    extensao = {"PNG": "png", "JPG": "jpg", "GIF": "gif", "WEBP": "webp"}[formato]
    resumo = hashlib.sha256(dados).hexdigest()[:20]
    # Nome pelo conteúdo: impede o Discord de reutilizar a imagem antiga em cache.
    nome = f"painel-lol-{guild_id}-{resumo}.{extensao}"
    PASTA_IMAGENS_FILA.mkdir(parents=True, exist_ok=True)
    caminho = PASTA_IMAGENS_FILA / nome
    temporario = PASTA_IMAGENS_FILA / f"{guild_id}.{resumo}.tmp"
    registro_antigo = imagens_fila.get(str(guild_id))
    try:
        with temporario.open("wb") as destino:
            destino.write(dados)
            destino.flush()
            os.fsync(destino.fileno())
        temporario.replace(caminho)
        if caminho.read_bytes() != dados:
            raise OSError("O arquivo salvo não corresponde à imagem enviada.")
        imagens_fila[str(guild_id)] = {"nome": nome, "sha256": hashlib.sha256(dados).hexdigest()}
        try:
            salvar_config_imagens()
        except OSError:
            if registro_antigo is None:
                imagens_fila.pop(str(guild_id), None)
            else:
                imagens_fila[str(guild_id)] = registro_antigo
            raise
        log.info("Imagem permanente da queue confirmada: %s (%s bytes)", caminho, len(dados))
        # Não apaga a imagem antiga: filas já enviadas ainda podem precisar dela.
        return nome
    finally:
        temporario.unlink(missing_ok=True)


def imagem_fila_salva(guild_id: int) -> tuple[bytes, str] | None:
    """Carrega imagem persistida, recuperando registro caso o índice desapareça."""
    registro = imagens_fila.get(str(guild_id)) or {}
    nome_registrado = registro.get("nome", "")
    candidatos = []
    if nome_registrado and Path(nome_registrado).name == nome_registrado:
        candidatos.append(PASTA_IMAGENS_FILA / nome_registrado)
    if PASTA_IMAGENS_FILA.is_dir():
        # Compatível com as duas versões anteriores (nomes com underline).
        for padrao in (
            f"painel-lol-{guild_id}-*.*", f"painel_lol_{guild_id}_*.*",
            f"painel_lol_{guild_id}.*",
        ):
            outros = list(PASTA_IMAGENS_FILA.glob(padrao))
            candidatos.extend(sorted(outros, key=lambda p: p.stat().st_mtime, reverse=True))

    for caminho in dict.fromkeys(candidatos):
        try:
            if not caminho.is_file() or caminho.stat().st_size > LIMITE_BYTES:
                continue
            dados = caminho.read_bytes()
        except OSError:
            continue
        if not dados or tipo_imagem(dados) is None:
            continue
        digest = hashlib.sha256(dados).hexdigest()
        if caminho.name == nome_registrado and registro.get("sha256") and registro["sha256"] != digest:
            log.warning("Imagem padrão corrompida: %s", caminho)
            continue
        # Atualiza imagens antigas cujo nome continha underline, pois nomes de
        # anexos sem underscore são mais confiáveis em embeds do Discord.
        if "_" in caminho.name:
            extensao = caminho.suffix.lower().lstrip(".")
            seguro = f"painel-lol-{guild_id}-{digest[:20]}.{extensao}"
            destino = PASTA_IMAGENS_FILA / seguro
            if not destino.exists():
                temporario = PASTA_IMAGENS_FILA / f"{seguro}.tmp"
                temporario.write_bytes(dados)
                temporario.replace(destino)
            caminho = destino
        if nome_registrado != caminho.name:
            imagens_fila[str(guild_id)] = {"nome": caminho.name, "sha256": digest}
            salvar_config_imagens()
            log.info("Configuração da imagem da queue recuperada: %s", caminho)
        return dados, caminho.name

    if registro:
        raise FileNotFoundError(
            "A imagem cadastrada não foi encontrada no armazenamento /data. "
            "Envie-a novamente uma única vez usando /queue_imagem."
        )
    return None


def opcoes_imagem_edicao_fila(
    guild_id: int, fila: dict[str, Any], mensagem: discord.Message
) -> dict[str, Any]:
    """Preserva imagens de embed mesmo que Discord oculte seus anexos.

    Arquivos utilizados no embed frequentemente NÃO aparecem em
    mensagem.attachments. Nesse caso, não passar attachments ao editar
    conserva o anexo que já está na mensagem. Só reenvia se a imagem
    realmente não estiver referenciada nos embeds existentes.
    """
    nome = fila.get("imagem_nome")
    if not nome:
        return {}
    if any(anexo.filename == nome for anexo in mensagem.attachments):
        return {}
    for embed in mensagem.embeds:
        imagem = getattr(embed, "image", None)
        url = getattr(imagem, "url", None)
        if url and (url == f"attachment://{nome}" or unquote(urlsplit(url).path).endswith("/" + nome)):
            return {}
    # Imagem não identificada nem no embed nem na lista de anexos.
    imagem_salva = imagem_fila_salva(guild_id)
    if imagem_salva is None:
        raise FileNotFoundError("Imagem do painel indisponível. Use /queue_imagem.")
    dados, nome_salvo = imagem_salva
    fila["imagem_nome"] = nome_salvo
    salvar_filas()
    log.warning("Reanexando imagem ausente à queue do servidor %s", guild_id)
    return {"attachments": [
        *mensagem.attachments,
        discord.File(io.BytesIO(dados), filename=nome_salvo),
    ]}


# Se reiniciou enquanto uma fila estava iniciando, a partida já está salva.
_filas_recuperadas = False
for _chave_fila, _fila in list(filas.items()):
    if _fila.get("status") == "iniciando":
        _filas_recuperadas = True
        # A partida criada por uma fila usa o mesmo ID da mensagem da fila.
        if _chave_fila in partidas:
            filas.pop(_chave_fila, None)
        else:
            _fila["status"] = "aberta"
# Também regrava o arquivo se ele estava no formato antigo (uma fila por servidor).
if ARQUIVO_FILAS.exists():
    try:
        _filas_recuperadas = _filas_recuperadas or set(filas) != set(
            json.loads(ARQUIVO_FILAS.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        pass
# As alterações de recuperação são persistidas depois de definir salvar_filas().
travas: dict[int, asyncio.Lock] = {}


def salvar_filas() -> None:
    ARQUIVO_FILAS.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_FILAS.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(filas, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(ARQUIVO_FILAS)


if _filas_recuperadas:
    salvar_filas()



def trava_servidor(guild_id: int) -> asyncio.Lock:
    if guild_id not in travas:
        travas[guild_id] = asyncio.Lock()
    return travas[guild_id]


def salvar_partidas() -> None:
    ARQUIVO_PARTIDAS.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_PARTIDAS.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(partidas, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(ARQUIVO_PARTIDAS)


if _partidas_convertidas:
    salvar_partidas()


def partidas_do_servidor(guild_id: int) -> list[tuple[str, dict[str, Any]]]:
    """Partidas ativas do servidor, ordenadas pelo número da partida."""
    ativas = [
        (chave, partida) for chave, partida in partidas.items()
        if partida.get("guild_id") == guild_id
    ]
    return sorted(ativas, key=lambda item: int(item[1].get("numero", 1)))


def numero_livre_partida(guild_id: int) -> int:
    """Menor número ainda não usado por uma partida ativa do servidor."""
    usados = {int(p.get("numero", 1)) for _c, p in partidas_do_servidor(guild_id)}
    numero = 1
    while numero in usados:
        numero += 1
    return numero


def descrever_partida(guild: discord.Guild, partida: dict[str, Any]) -> tuple[str, str]:
    """Rótulo e descrição curtos para identificar a partida em menus."""
    times = list((partida.get("canais") or {}).keys()) or ["vermelho", "azul"]
    nomes = [nome for chave, _e, nome in EQUIPES if chave in times]
    rotulo = (
        f"Partida {partida.get('numero', 1)} | {len(partida.get('participantes', {}))} jogadores"
        f" | {len(times)} times"
    )
    origem = buscar_voz(guild, partida.get("origem_id"))
    descricao = (
        f"Sala: {origem.name if origem else 'indisponível'} | " + " x ".join(nomes)
    )
    return rotulo[:100], descricao[:100]


def buscar_voz(guild: discord.Guild, canal_id: int | None) -> discord.VoiceChannel | None:
    canal = guild.get_channel(canal_id) if canal_id is not None else None
    return canal if isinstance(canal, discord.VoiceChannel) else None


def canal_times(guild: discord.Guild, partida: dict[str, Any]) -> list[discord.VoiceChannel]:
    encontrados = []
    # Partidas criadas pela fila podem conter de 2 a 6 canais.
    identificadores = list((partida.get("canais") or {}).values())
    if not identificadores:
        identificadores = [partida.get("vermelho_id"), partida.get("azul_id")]
    for identificador in identificadores:
        canal = buscar_voz(guild, identificador)
        if canal is not None:
            encontrados.append(canal)
    return encontrados


def mencionar_jogadores(jogadores: list[discord.Member]) -> str:
    # Mantém o campo do embed dentro do limite de caracteres do Discord.
    nomes = [j.mention for j in jogadores[:30]]
    if len(jogadores) > 30:
        nomes.append(f"e mais {len(jogadores) - 30} jogador(es)")
    return "\n".join(nomes) if nomes else "Nenhum"


def autorizado(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_channels
    )


# ====================== PRESENÇA DO BOT ======================
# Estado e texto salvos em /data para continuar após reinício ou redeploy.
ESTADOS_BOT = {
    "disponivel": discord.Status.online,
    "ausente": discord.Status.idle,
    "ocupado": discord.Status.dnd,
    "invisivel": discord.Status.invisible,
}
ROTULOS_ESTADOS_BOT = {
    "disponivel": "🟢 Disponível",
    "ausente": "🌙 Ausente",
    "ocupado": "🔴 Ocupado",
    "invisivel": "⚫ Invisível",
}


def carregar_presenca() -> dict[str, str]:
    """Lê configuração global da presença e valida antes do login."""
    padrao = {"estado": "disponivel", "texto": ""}
    if not ARQUIVO_PRESENCA.exists():
        return padrao
    try:
        dados = json.loads(ARQUIVO_PRESENCA.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("A configuração precisa ser um objeto JSON.")
        estado = dados.get("estado", "disponivel")
        texto = dados.get("texto", "")
        if estado not in ESTADOS_BOT or not isinstance(texto, str) or len(texto) > 128:
            raise ValueError("Estado ou texto de presença inválido.")
        return {"estado": estado, "texto": texto}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Erro ao ler bot_presenca.json: {exc}") from exc


def salvar_presenca(config: dict[str, str]) -> None:
    """Gravação atômica no disco persistente do FadeHost."""
    ARQUIVO_PRESENCA.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_PRESENCA.with_suffix(".json.tmp")
    temporario.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(ARQUIVO_PRESENCA)


def atividade_presenca(texto: str) -> discord.CustomActivity | None:
    """Usa texto livre em vez de prefixos Jogando/Assistindo."""
    return discord.CustomActivity(name=texto) if texto else None


presenca_bot = carregar_presenca()
trava_presenca_bot = asyncio.Lock()


intents = discord.Intents.default()
intents.members = True
intents.voice_states = True


class ClientePartidas(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=intents,
            status=ESTADOS_BOT[presenca_bot["estado"]],
            activity=atividade_presenca(presenca_bot["texto"]),
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Os botões da fila continuam funcionando após restart/redeploy.
        for fila in filas.values():
            if fila.get("status") == "aberta" and fila.get("message_id"):
                self.add_view(
                    FilaView(cheia=fila_cheia(fila), pagina=fila_pagina(fila), paginas=fila_total_paginas(fila)),
                    message_id=int(fila["message_id"]),
                )
        # O botão público Assistir continua ativo após um reinício do bot.
        for partida in partidas.values():
            if partida.get("assistir_message_id"):
                self.add_view(
                    AssistirView(), message_id=int(partida["assistir_message_id"])
                )
        if GUILD_ID:
            try:
                servidor = discord.Object(id=int(GUILD_ID))
            except ValueError as exc:
                raise RuntimeError("DISCORD_GUILD_ID precisa ser um número inteiro.") from exc
            self.tree.copy_global_to(guild=servidor)
            sincronizados = await self.tree.sync(guild=servidor)
        else:
            sincronizados = await self.tree.sync()
        log.info("Comandos de barra sincronizados: %s", len(sincronizados))

    async def on_ready(self) -> None:
        log.info("Conectado como %s | %s partida(s) registrada(s)", self.user, len(partidas))
        if getattr(self, "_imagens_fila_recuperadas", False):
            return
        self._imagens_fila_recuperadas = True
        # Recupera também a imagem do painel da versão anterior, caso ainda
        # exista no Discord e ainda não tenha sido salva em /data.
        for fila in list(filas.values()):
            guild_id = fila.get("guild_id")
            if (not fila.get("imagem_nome") or not fila.get("message_id")
                    or str(guild_id) in imagens_fila):
                continue
            canal = self.get_channel(fila.get("channel_id"))
            if not isinstance(canal, discord.TextChannel):
                continue
            try:
                mensagem = await canal.fetch_message(fila["message_id"])
                anexo = next(
                    (a for a in mensagem.attachments
                     if a.filename == fila.get("imagem_nome") and a.size <= LIMITE_BYTES),
                    None,
                )
                if anexo is not None:
                    guardar_imagem_fila(int(guild_id), await anexo.read())
                    log.info("Imagem antiga da fila recuperada para o servidor %s", guild_id)
            except (discord.HTTPException, OSError, ValueError) as exc:
                log.warning("Não foi possível recuperar a imagem antiga da fila: %s", exc)


bot = ClientePartidas()
async def devolver_jogadores(
    guild: discord.Guild, partida: dict[str, Any]
) -> tuple[int, list[str]]:
    """Devolve quem continua nos canais da partida.

    Jogadores que saíram voluntariamente dos canais da partida não são movidos.
    Visitantes que chegaram depois são enviados ao canal de origem da partida.
    """
    devolvidos = 0
    problemas = []
    participantes = partida.get("participantes", {})
    espectadores = partida.get("espectadores", {})
    canal_padrao = buscar_voz(guild, partida.get("origem_id"))

    for canal in canal_times(guild, partida):
        for membro in list(canal.members):
            cadastro = (participantes.get(str(membro.id))
                        or espectadores.get(str(membro.id)) or {})
            destino = buscar_voz(guild, cadastro.get("origem_id")) or canal_padrao
            if destino is None:
                problemas.append(f"{membro.display_name}: canal original indisponível")
                continue
            try:
                await membro.move_to(destino, reason="Encerramento da partida: voltar ao canal original")
                devolvidos += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Falha ao devolver %s (%s): %s", membro.id, guild.id, exc)
                problemas.append(f"{membro.display_name}: não foi possível mover")
    return devolvidos, problemas


async def remover_estrutura(
    guild: discord.Guild, partida: dict[str, Any]
) -> list[str]:
    """Não apaga canais que ainda tenham pessoas nem categorias com outros canais."""
    problemas = []
    for canal in canal_times(guild, partida):
        if canal.members:
            problemas.append(f"{canal.name} ainda tem {len(canal.members)} pessoa(s)")
            continue
        try:
            await canal.delete(reason="Limpeza da partida finalizada")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Falha ao apagar canal %s: %s", canal.id, exc)
            problemas.append(f"não foi possível apagar {canal.name}")

    categoria = guild.get_channel(partida.get("categoria_id"))
    if isinstance(categoria, discord.CategoryChannel):
        if categoria.channels:
            problemas.append("a categoria ainda contém canais")
        else:
            try:
                await categoria.delete(reason="Limpeza da partida finalizada")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Falha ao apagar categoria %s: %s", categoria.id, exc)
                problemas.append("não foi possível apagar a categoria")
    return problemas


async def terminar_partida(interaction: discord.Interaction, vencedor: str | None) -> None:
    guild = interaction.guild
    if guild is None or not autorizado(interaction):
        await interaction.response.send_message(
            "⛔ Apenas membros com **Gerenciar Canais** podem usar este comando.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    async with trava_servidor(guild.id):
        ativas = partidas_do_servidor(guild.id)
        if not ativas:
            await interaction.followup.send("ℹ️ Não há partida ativa neste servidor.")
            return
        # Apenas uma partida ativa: finaliza/cancela direto, sem perguntar.
        if len(ativas) == 1:
            mensagem = await encerrar_partida(guild, ativas[0][0], vencedor)
            await interaction.followup.send(
                mensagem, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        aviso = (
            f"\n⚠️ Mostrando as 25 primeiras de {len(ativas)} partidas."
            if len(ativas) > 25 else ""
        )
        acao = "finalizar" if vencedor else "cancelar"
        await interaction.followup.send(
            f"🎮 Há **{len(ativas)} partidas em andamento**. "
            f"Selecione qual deseja {acao}:{aviso}",
            view=SeletorPartida(interaction.user.id, guild, vencedor),
        )


async def encerrar_partida(
    guild: discord.Guild, chave_partida: str, vencedor: str | None
) -> str:
    """Finaliza ou cancela uma partida. Deve ser chamada com a trava do servidor."""
    partida = partidas.get(chave_partida)
    if partida is None or partida.get("guild_id") != guild.id:
        return "ℹ️ Essa partida não está mais ativa."
    numero = partida.get("numero", 1)
    anterior = partida.get("vencedor")
    if anterior and vencedor and anterior != vencedor:
        return (
            f"⚠️ O vencedor da Partida {numero} já foi registrado como **{anterior.title()}**. "
            "Não é possível alterar o resultado enquanto a partida está sendo finalizada."
        )
    # Não aceita declarar um time que não participa desta partida.
    chaves_ativas = set((partida.get("canais") or {}).keys()) or {"vermelho", "azul"}
    if vencedor is not None and vencedor not in chaves_ativas:
        return (
            f"⚠️ Esse time não participa da Partida {numero}. Selecione um vencedor válido."
        )
    if vencedor and not anterior:
        partida["vencedor"] = vencedor
        salvar_partidas()

    # Desativa o botão de espectador assim que a finalização começa.
    canal_painel = guild.get_channel(partida.get("assistir_channel_id"))
    if isinstance(canal_painel, discord.TextChannel) and partida.get("assistir_message_id"):
        try:
            painel = await canal_painel.fetch_message(partida["assistir_message_id"])
            await painel.edit(view=None)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            log.warning("Não consegui desativar o botão Assistir: %s", exc)

    devolvidos, problemas = await devolver_jogadores(guild, partida)
    # Dá ao Discord um curto intervalo para atualizar os estados de voz.
    await asyncio.sleep(1)
    problemas.extend(await remover_estrutura(guild, partida))

    # É seguro descartar o registro somente quando a estrutura foi removida.
    categoria_restante = guild.get_channel(partida.get("categoria_id"))
    canais_restantes = canal_times(guild, partida)
    concluida = not categoria_restante and not canais_restantes
    if concluida:
        partidas.pop(chave_partida, None)
        salvar_partidas()

    resultado = partida.get("vencedor")
    mensagem = f"🎮 **Partida {numero}**\n"
    if resultado:
        detalhes = next((f"{emoji} {nome.upper()}" for chave, emoji, nome in EQUIPES
                          if chave == resultado), resultado.upper())
        mensagem += f"🏆 **O TIME {detalhes} VENCEU!**\n"
    else:
        mensagem += "🛑 **Partida cancelada.**\n"
    mensagem += f"↩️ Pessoas devolvidas ao canal original: **{devolvidos}**.\n"
    if concluida:
        mensagem += "🧹 Canais e categoria da partida removidos."
    else:
        mensagem += (
            "⚠️ A estrutura foi mantida para proteger jogadores ou canais ainda presentes. "
            "Use `/partida_finalizar` ou `/partida_cancelar` novamente depois de corrigir o problema."
        )
    if problemas:
        mensagem += "\n**Pendências:** " + "; ".join(problemas[:5])
    return mensagem


class SeletorPartida(discord.ui.View):
    """Menu para escolher qual partida em andamento será finalizada ou cancelada."""

    def __init__(self, usuario_id: int, guild: discord.Guild, vencedor: str | None) -> None:
        super().__init__(timeout=180)
        self.usuario_id = usuario_id
        self.guild_id = guild.id
        self.vencedor = vencedor
        opcoes = []
        # O Discord aceita no máximo 25 opções em um menu.
        for chave, partida in partidas_do_servidor(guild.id)[:25]:
            rotulo, descricao = descrever_partida(guild, partida)
            opcoes.append(discord.SelectOption(
                label=rotulo, value=chave, description=descricao,
                emoji="🏆" if vencedor else "🛑",
            ))
        self.menu = discord.ui.Select(
            placeholder="Escolha a partida", min_values=1, max_values=1, options=opcoes,
        )
        self.menu.callback = self.escolher
        self.add_item(self.menu)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id or inter.guild_id != self.guild_id:
            await inter.response.send_message(
                "🚫 Somente quem abriu este menu pode usá-lo.", ephemeral=True
            )
            return False
        return True

    async def escolher(self, inter: discord.Interaction) -> None:
        if inter.guild is None or not autorizado(inter):
            await inter.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        chave = self.menu.values[0]
        await inter.response.defer()
        async with trava_servidor(inter.guild.id):
            mensagem = await encerrar_partida(inter.guild, chave, self.vencedor)
        await inter.edit_original_response(
            content=mensagem, view=None, allowed_mentions=discord.AllowedMentions.none()
        )



def permissoes_de_time(
    guild: discord.Guild, bot_membro: discord.Member, integrantes: list[discord.Member]
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Todos veem o cadeado, mas só o time tem permissão para conectar."""
    permissoes = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        bot_membro: discord.PermissionOverwrite(
            view_channel=True, connect=True, move_members=True, manage_channels=True
        ),
    }
    for jogador in integrantes:
        permissoes[jogador] = discord.PermissionOverwrite(view_channel=True, connect=True)
    return permissoes


@bot.tree.command(name="partida_iniciar", description="Cria dois times aleatórios e move todos para os canais de voz.")
@app_commands.describe(canal="Canal de voz com os jogadores. Se vazio, usa o canal em que você está.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def partida_iniciar(
    interaction: discord.Interaction, canal: discord.VoiceChannel | None = None
) -> None:
    guild = interaction.guild
    if guild is None or not autorizado(interaction):
        await interaction.response.send_message(
            "⛔ Apenas membros com **Gerenciar Canais** podem iniciar partidas.", ephemeral=True
        )
        return

    if canal is None:
        estado = getattr(interaction.user, "voice", None)
        canal = estado.channel if estado else None
    if not isinstance(canal, discord.VoiceChannel):
        await interaction.response.send_message(
            "🎙️ Entre em um canal de voz ou informe o canal no comando.", ephemeral=True
        )
        return
    if canal.guild.id != guild.id:
        await interaction.response.send_message("⛔ Canal inválido.", ephemeral=True)
        return

    eu = guild.me
    if eu is None or not (
        eu.guild_permissions.manage_channels and eu.guild_permissions.move_members
        and eu.guild_permissions.manage_roles
    ):
        await interaction.response.send_message(
            "⚠️ O bot precisa de **Gerenciar Canais**, **Mover Membros** e "
            "**Gerenciar Cargos** para trancar os canais de voz.",
            ephemeral=True,
        )
        return
    if not canal.permissions_for(eu).move_members:
        await interaction.response.send_message(
            "⚠️ O bot não pode mover pessoas no canal de origem selecionado.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    async with trava_servidor(guild.id):
        # Cada partida tem sua própria chave; várias podem rodar ao mesmo tempo.
        chave = str(interaction.id)
        numero = numero_livre_partida(guild.id)
        jogadores = [m for m in canal.members if not m.bot]
        if len(jogadores) < 2:
            await interaction.followup.send(
                "⚠️ São necessários **pelo menos 2 jogadores humanos** no canal de voz."
            )
            return

        random.shuffle(jogadores)
        corte = (len(jogadores) + 1) // 2
        vermelhos, azuis = jogadores[:corte], jogadores[corte:]

        partida: dict[str, Any] = {
            "guild_id": guild.id,
            "numero": numero,
            "origem_id": canal.id,
            "categoria_id": None,
            "vermelho_id": None,
            "azul_id": None,
            "vencedor": None,
            "participantes": {
                str(m.id): {
                    "origem_id": canal.id,
                    "time": "vermelho" if m in vermelhos else "azul",
                }
                for m in jogadores
            },
        }
        partidas[chave] = partida
        salvar_partidas()
        try:
            categoria = await guild.create_category(
                f"🎮 PARTIDA {numero} EM ANDAMENTO", reason="Início da partida"
            )
            partida["categoria_id"] = categoria.id
            salvar_partidas()
            canal_vermelho = await guild.create_voice_channel(
                "🔴 Time Vermelho 🔒", category=categoria,
                overwrites=permissoes_de_time(guild, eu, vermelhos),
                user_limit=len(vermelhos),
                reason="Início da partida com canal restrito",
            )
            partida["vermelho_id"] = canal_vermelho.id
            salvar_partidas()
            canal_azul = await guild.create_voice_channel(
                "🔵 Time Azul 🔒", category=categoria,
                overwrites=permissoes_de_time(guild, eu, azuis),
                user_limit=len(azuis),
                reason="Início da partida com canal restrito",
            )
            partida["azul_id"] = canal_azul.id
            salvar_partidas()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.exception("Falha ao criar estrutura da partida: %s", exc)
            pendencias = await remover_estrutura(guild, partida)
            if not guild.get_channel(partida.get("categoria_id")) and not canal_times(guild, partida):
                partidas.pop(chave, None)
                salvar_partidas()
            await interaction.followup.send(
                "❌ Não foi possível criar os canais. Confira as permissões do bot."
                + (" Pendências: " + "; ".join(pendencias) if pendencias else "")
            )
            return

        erros = []
        movimentados = 0
        for membro in vermelhos + azuis:
            # Quem saiu do canal por conta própria não será puxado de volta.
            if not membro.voice or membro.voice.channel != canal:
                erros.append(membro.display_name)
                continue
            destino = canal_vermelho if membro in vermelhos else canal_azul
            try:
                await membro.move_to(destino, reason="Sorteio de equipes da partida")
                movimentados += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Falha ao mover jogador %s: %s", membro.id, exc)
                erros.append(membro.display_name)

        if movimentados == 0:
            pendencias = await remover_estrutura(guild, partida)
            if not guild.get_channel(partida.get("categoria_id")) and not canal_times(guild, partida):
                partidas.pop(chave, None)
                salvar_partidas()
            await interaction.followup.send(
                "❌ Não foi possível mover nenhum jogador. Verifique **Mover Membros** e **Conectar** "
                "nos canais de destino."
                + (" Pendências: " + "; ".join(pendencias) if pendencias else "")
            )
            return

        embed = discord.Embed(
            title=f"🎮 Partida {numero} iniciada!",
            description=(
                f"Canal original: {canal.mention}\n"
                f"Jogadores sorteados: **{len(jogadores)}** | Movidos: **{movimentados}**"
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name=f"🔴 Vermelho ({len(vermelhos)}) | {canal_vermelho.name}",
            value=mencionar_jogadores(vermelhos),
            inline=True,
        )
        embed.add_field(
            name=f"🔵 Azul ({len(azuis)}) | {canal_azul.name}",
            value=mencionar_jogadores(azuis),
            inline=True,
        )
        embed.set_footer(text="No fim, use /partida_finalizar e escolha o vencedor.")
        mensagem = (
            "⚠️ Não foi possível mover: " + ", ".join(erros[:10])
            if erros
            else None
        )
        await interaction.followup.send(
            content=mensagem,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )


@bot.tree.command(name="partida_finalizar", description="Registra o vencedor, devolve os jogadores e remove os canais.")
@app_commands.describe(vencedor="Selecione o time que ganhou a partida.")
@app_commands.choices(
    vencedor=[
        app_commands.Choice(name="🔴 Vermelho", value="vermelho"),
        app_commands.Choice(name="🔵 Azul", value="azul"),
        app_commands.Choice(name="🟢 Verde", value="verde"),
        app_commands.Choice(name="🟡 Amarelo", value="amarelo"),
        app_commands.Choice(name="🟣 Roxo", value="roxo"),
        app_commands.Choice(name="🟠 Laranja", value="laranja"),
    ]
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def partida_finalizar(
    interaction: discord.Interaction, vencedor: app_commands.Choice[str]
) -> None:
    await terminar_partida(interaction, vencedor.value)


@bot.tree.command(name="partida_cancelar", description="Cancela a partida e devolve todos aos canais originais.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def partida_cancelar(interaction: discord.Interaction) -> None:
    await terminar_partida(interaction, None)


@bot.tree.command(name="partida_status", description="Exibe o estado da partida em andamento.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def partida_status(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None or not autorizado(interaction):
        await interaction.response.send_message(
            "⛔ Apenas membros com **Gerenciar Canais** podem consultar o status.",
            ephemeral=True,
        )
        return
    async with trava_servidor(guild.id):
        ativas = partidas_do_servidor(guild.id)
        if not ativas:
            await interaction.response.send_message("ℹ️ Não existe partida ativa.", ephemeral=True)
            return
        blocos = []
        # Mostra todas as partidas em andamento no servidor.
        for _chave, partida in ativas:
            canais = canal_times(guild, partida)
            linhas = [
                f"🎮 **Partida {partida.get('numero', 1)}**",
                f"🎙️ Origem: <#{partida['origem_id']}>",
                f"👥 Jogadores sorteados: {len(partida.get('participantes', {}))}",
                f"🏆 Vencedor: {partida.get('vencedor') or 'a definir'}",
            ]
            for canal in canais:
                linhas.append(f"• {canal.mention}: {len(canal.members)} pessoa(s) conectada(s)")
            blocos.append("\n".join(linhas))
        texto = "\n\n".join(blocos)
        if len(texto) > 2000:
            texto = texto[:1990] + "\n…"
        await interaction.response.send_message(texto, ephemeral=True)




# ======================= CONTA RIOT (API) =======================
# /link liga o Discord ao Riot ID. O elo oficial (Solo/Duo) aparece no painel da fila.
# O elo é só informativo: o sorteio dos times continua totalmente aleatório.
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()
RIOT_REGIAO = "americas"  # Account-V1: contas do Brasil ficam na região americas.
RIOT_PLATAFORMA = (os.getenv("RIOT_PLATAFORMA", "br1").strip().lower() or "br1")
ARQUIVO_CONTAS_RIOT = Path("/data/contas_riot.json")
ELO_VALIDADE_SEGUNDOS = 30 * 60  # Reconsulta o elo no máximo a cada 30 minutos.
# Aviso exigido pela Riot para produtos que usam a API.
AVISO_RIOT = (
    "Este bot não é endossado pela Riot Games e não reflete as opiniões da Riot Games "
    "ou de qualquer pessoa oficialmente envolvida na produção ou gestão das propriedades "
    "da Riot Games. Riot Games e todas as propriedades associadas são marcas comerciais "
    "ou marcas registradas da Riot Games, Inc."
)
NOMES_TIER = {
    "IRON": "Ferro",
    "BRONZE": "Bronze",
    "SILVER": "Prata",
    "GOLD": "Ouro",
    "PLATINUM": "Platina",
    "EMERALD": "Esmeralda",
    "DIAMOND": "Diamante",
    "MASTER": "Mestre",
    "GRANDMASTER": "Grão-Mestre",
    "CHALLENGER": "Desafiante",
}


def carregar_contas_riot() -> dict[str, dict[str, Any]]:
    if not ARQUIVO_CONTAS_RIOT.exists():
        return {}
    try:
        dados = json.loads(ARQUIVO_CONTAS_RIOT.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("A raiz deve ser um objeto JSON.")
        return dados
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        raise SystemExit(f"Erro ao ler contas_riot.json: {exc}") from exc


contas_riot = carregar_contas_riot()
trava_contas_riot = asyncio.Lock()


def salvar_contas_riot() -> None:
    ARQUIVO_CONTAS_RIOT.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_CONTAS_RIOT.with_suffix(".json.tmp")
    temporario.write_text(
        json.dumps(contas_riot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(ARQUIVO_CONTAS_RIOT)


# Cada pessoa pode ter várias contas: {"ID do Discord": {"contas": [conta, ...]}}.
# O formato antigo (uma conta por pessoa) é convertido automaticamente.
MAX_CONTAS_RIOT = 5
# Além do dono, quem tem este cargo pode vincular/remover contas de outros membros.
CARGO_GERENCIAR_CONTAS = 1049073332357701704
_contas_convertidas = False
for _uid_riot, _registro_riot in list(contas_riot.items()):
    if isinstance(_registro_riot, dict) and "puuid" in _registro_riot:
        contas_riot[_uid_riot] = {"contas": [_registro_riot]}
        _contas_convertidas = True
if _contas_convertidas:
    salvar_contas_riot()


def contas_do_usuario(usuario_id: int) -> list[dict[str, Any]]:
    registro = contas_riot.get(str(usuario_id))
    return list(registro.get("contas", [])) if isinstance(registro, dict) else []


def encontrar_conta_riot(
    contas: list[dict[str, Any]], texto: str
) -> dict[str, Any] | None:
    """Acha a conta pelo valor do autocompletar (PUUID) ou pelo Riot ID digitado."""
    texto = (texto or "").strip()
    return next(
        (c for c in contas
         if c.get("puuid") == texto or c.get("riot_id", "").casefold() == texto.casefold()),
        None,
    )


class ErroRiot(Exception):
    def __init__(self, status: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.status = status


# Limites da chave pessoal: 20 por segundo e 100 a cada 2 minutos.
# Usamos valores um pouco menores para nunca estourar o limite.
LIMITES_RIOT = ((18, 1.0), (95, 120.0))
_chamadas_riot: deque[float] = deque()
_trava_limite_riot = asyncio.Lock()


async def aguardar_limite_riot() -> None:
    """Espera, se preciso, até poder fazer mais uma consulta sem passar do limite."""
    async with _trava_limite_riot:
        while True:
            agora = time.monotonic()
            while _chamadas_riot and agora - _chamadas_riot[0] >= 120.0:
                _chamadas_riot.popleft()
            espera = 0.0
            for maximo, janela in LIMITES_RIOT:
                recentes = [t for t in _chamadas_riot if agora - t < janela]
                if len(recentes) >= maximo:
                    espera = max(espera, janela - (agora - recentes[-maximo]) + 0.05)
            if espera <= 0:
                _chamadas_riot.append(agora)
                return
            await asyncio.sleep(espera)


async def chamar_riot(url: str) -> Any:
    """Faz uma consulta à API da Riot. A chave vai só no cabeçalho, nunca na URL."""
    if not RIOT_API_KEY:
        raise ErroRiot(0, "A API da Riot não está configurada (RIOT_API_KEY).")
    await aguardar_limite_riot()
    timeout = aiohttp.ClientTimeout(total=8, connect=4)
    async with aiohttp.ClientSession(timeout=timeout) as sessao:
        async with sessao.get(url, headers={"X-Riot-Token": RIOT_API_KEY}) as resposta:
            if resposta.status == 200:
                return await resposta.json()
            if resposta.status in (400, 404):
                raise ErroRiot(
                    resposta.status,
                    "Riot ID não encontrado. Confira o nick e a tag (exemplo: Nick#BR1).",
                )
            if resposta.status in (401, 403):
                raise ErroRiot(
                    resposta.status,
                    "A chave da API da Riot é inválida ou expirou. Confira RIOT_API_KEY.",
                )
            if resposta.status == 429:
                raise ErroRiot(
                    resposta.status,
                    "Muitas consultas à Riot agora. Tente de novo em alguns minutos.",
                )
            raise ErroRiot(resposta.status, f"A Riot respondeu com erro {resposta.status}.")


async def buscar_conta_riot(nome: str, tag: str) -> dict[str, Any]:
    dados = await chamar_riot(
        f"https://{RIOT_REGIAO}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(nome, safe='')}/{quote(tag, safe='')}"
    )
    if not isinstance(dados, dict) or not dados.get("puuid"):
        raise ErroRiot(0, "A Riot devolveu uma resposta inesperada.")
    return dados


ORDEM_TIERS = [
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
    "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
]
ORDEM_DIVISOES = {"IV": 0, "III": 1, "II": 2, "I": 3}


def formatar_elo(entradas: Any) -> tuple[str, int]:
    """Texto do elo Solo/Duo oficial e um número para saber qual conta tem o maior."""
    solo = None
    if isinstance(entradas, list):
        solo = next(
            (e for e in entradas if isinstance(e, dict)
             and e.get("queueType") == "RANKED_SOLO_5x5"),
            None,
        )
    if not solo:
        return "Sem ranque", -1
    tier = str(solo.get("tier", ""))
    nome = NOMES_TIER.get(tier, tier.title())
    pdl = int(solo.get("leaguePoints", 0) or 0)
    divisao = str(solo.get("rank", ""))
    ordem = (
        (ORDEM_TIERS.index(tier) if tier in ORDEM_TIERS else 0) * 10000
        + ORDEM_DIVISOES.get(divisao, 0) * 1000 + pdl
    )
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return f"{nome} ({pdl} PDL)", ordem
    return f"{nome} {divisao} ({pdl} PDL)", ordem


async def buscar_elo(puuid: str) -> tuple[str, int]:
    dados = await chamar_riot(
        f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/league/v4/entries/by-puuid/"
        f"{quote(puuid, safe='')}"
    )
    return formatar_elo(dados)


async def atualizar_elo_se_preciso(usuario_id: int, limite: float = 2.0) -> None:
    """Atualiza o elo salvo das contas antigas. Em caso de falha, mantém o anterior."""
    if not RIOT_API_KEY:
        return
    antigas = [
        c for c in contas_do_usuario(usuario_id)
        if time.time() - float(c.get("elo_em", 0)) >= ELO_VALIDADE_SEGUNDOS
    ]
    if not antigas:
        return
    resultados = await asyncio.gather(
        *(asyncio.wait_for(buscar_elo(c["puuid"]), timeout=limite) for c in antigas),
        return_exceptions=True,
    )
    mudou = False
    async with trava_contas_riot:
        for conta, resultado in zip(antigas, resultados):
            if isinstance(resultado, BaseException):
                log.warning("Não consegui atualizar o elo de %s: %s", usuario_id, resultado)
                continue
            conta["elo"], conta["elo_ordem"] = resultado
            conta["elo_em"] = time.time()
            mudou = True
        if mudou:
            try:
                salvar_contas_riot()
            except OSError as exc:
                log.warning("Não consegui salvar contas_riot.json: %s", exc)


def texto_elo(usuario_id: int) -> str:
    """Trecho exibido ao lado do jogador no painel da fila (maior elo entre as contas)."""
    contas = [c for c in contas_do_usuario(usuario_id) if c.get("elo")]
    if not contas:
        return ""
    maior = max(contas, key=lambda c: int(c.get("elo_ordem", -1)))
    return f" • {maior['elo']}"


async def pode_gerenciar_contas(inter: discord.Interaction) -> bool:
    """Dono do servidor/bot ou quem tem o cargo autorizado."""
    if isinstance(inter.user, discord.Member) and any(
        cargo.id == CARGO_GERENCIAR_CONTAS for cargo in inter.user.roles
    ):
        return True
    return await dono_da_fila(inter)


async def atualizar_paineis_do_jogador(guild: discord.Guild, usuario_id: int) -> None:
    """Redesenha os painéis de fila abertos em que o jogador está inscrito."""
    async with trava_servidor(guild.id):
        for _chave, fila in filas_abertas(guild.id):
            if usuario_id in fila.get("inscritos", []):
                await atualizar_mensagem_fila(guild, fila)


async def autocompletar_conta_riot(
    inter: discord.Interaction, atual: str
) -> list[app_commands.Choice[str]]:
    """Lista as contas Riot do membro escolhido no comando (ou de quem está digitando)."""
    alvo = getattr(inter.namespace, "jogador", None) or getattr(inter.namespace, "membro", None)
    usuario_id = getattr(alvo, "id", None) or inter.user.id
    texto = (atual or "").casefold()
    return [
        app_commands.Choice(name=c.get("riot_id", "?")[:100], value=c["puuid"])
        for c in contas_do_usuario(usuario_id)
        if texto in c.get("riot_id", "").casefold() and len(c.get("puuid", "")) <= 100
    ][:25]


@bot.tree.command(
    name="link", description="Vincula uma conta do League of Legends (Riot ID) ao Discord"
)
@app_commands.describe(
    riot_id="Seu Riot ID (Nick#TAG). Deixe vazio para abrir a janela e vincular vários de uma vez",
    membro="Vincular para outro membro (só o dono ou o cargo autorizado)",
)
async def link(
    inter: discord.Interaction,
    riot_id: Optional[str] = None,
    membro: Optional[discord.Member] = None,
) -> None:
    if not RIOT_API_KEY:
        await inter.response.send_message(
            "⚠️ A API da Riot ainda não foi configurada no bot (RIOT_API_KEY).",
            ephemeral=True,
        )
        return
    # Sem Riot ID: abre a janela para vincular vários de uma vez (até 100).
    if not riot_id:
        if inter.guild is None:
            await inter.response.send_message("Use este comando no servidor.", ephemeral=True)
            return
        # Para si mesmo é livre; para outros membros, só o dono ou o cargo autorizado.
        if (membro is None or membro.id != inter.user.id) and not await pode_gerenciar_contas(inter):
            if membro is None:
                texto = (
                    "ℹ️ Informe seu Riot ID: `/link riot_id:Nick#TAG`.\n"
                    "Vincular vários de uma vez para outros membros é só para o dono "
                    "ou quem tem o cargo autorizado."
                )
            else:
                texto = (
                    "🔒 Só o dono ou quem tem o cargo autorizado pode vincular "
                    "contas de outros membros."
                )
            await inter.response.send_message(texto, ephemeral=True)
            return
        await inter.response.send_modal(ModalLinkVarios(inter.user.id, membro))
        return
    alvo = membro or inter.user
    if alvo.id != inter.user.id and not await pode_gerenciar_contas(inter):
        await inter.response.send_message(
            "🔒 Só o dono ou quem tem o cargo autorizado pode vincular contas de outros membros.",
            ephemeral=True,
        )
        return
    if alvo.bot:
        await inter.response.send_message("⚠️ Bots não podem ter conta vinculada.", ephemeral=True)
        return
    separado = separar_riot_id(riot_id)
    if separado is None:
        await inter.response.send_message(
            "⚠️ Use o formato **Nick#TAG**. Exemplo: `/link Nick#BR1`", ephemeral=True
        )
        return

    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        riot_nome, elo, total = await vincular_conta_riot(alvo.id, *separado)
    except ErroVinculo as exc:
        await inter.followup.send(
            f"⚠️ {exc}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
        return

    if inter.guild is not None:
        await atualizar_paineis_do_jogador(inter.guild, alvo.id)

    dono_conta = "Conta vinculada" if alvo.id == inter.user.id else f"Conta vinculada a {alvo.mention}"
    await inter.followup.send(
        f"✅ {dono_conta}: **{discord.utils.escape_markdown(riot_nome)}**\n"
        f"🏅 Elo (Solo/Duo): **{elo}**\n"
        f"📚 Contas vinculadas: **{total}/{MAX_CONTAS_RIOT}**\n"
        "No painel da fila aparece o maior elo entre as contas. "
        "Para remover uma conta, use `/desvincular`.\n\n"
        f"-# {AVISO_RIOT}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class ErroVinculo(Exception):
    """Falha ao vincular uma conta. 'parar' indica que não adianta tentar as próximas."""

    def __init__(self, mensagem: str, parar: bool = False) -> None:
        super().__init__(mensagem)
        self.parar = parar


def separar_riot_id(texto: str) -> tuple[str, str] | None:
    """'Nick#TAG' -> ('Nick', 'TAG'), ou None se o formato for inválido."""
    nome, separador, tag = (texto or "").strip().rpartition("#")
    nome, tag = nome.strip(), tag.strip()
    if not separador or not 3 <= len(nome) <= 16 or not 2 <= len(tag) <= 5:
        return None
    return nome, tag


async def vincular_conta_riot(usuario_id: int, nome: str, tag: str) -> tuple[str, str, int]:
    """Consulta a Riot e salva a conta. Retorna (Riot ID, elo, total de contas)."""
    try:
        conta = await buscar_conta_riot(nome, tag)
        elo, elo_ordem = await buscar_elo(conta["puuid"])
    except ErroRiot as exc:
        raise ErroVinculo(str(exc), parar=exc.status in (0, 401, 403)) from exc
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
        log.warning("Falha ao consultar a Riot ao vincular: %s", exc)
        raise ErroVinculo("Não consegui falar com a Riot agora. Tente de novo daqui a pouco.") from exc

    riot_nome = f"{conta.get('gameName') or nome}#{conta.get('tagLine') or tag}"
    puuid = conta["puuid"]
    chave = str(usuario_id)
    async with trava_contas_riot:
        # A mesma conta Riot não pode ficar vinculada a duas pessoas.
        for outro_id, registro in contas_riot.items():
            if outro_id != chave and any(
                c.get("puuid") == puuid for c in (registro or {}).get("contas", [])
            ):
                raise ErroVinculo(f"Essa conta já está vinculada a <@{outro_id}>.")
        registro = contas_riot.get(chave)
        lista = list(registro.get("contas", [])) if isinstance(registro, dict) else []
        existente = next((c for c in lista if c.get("puuid") == puuid), None)
        if existente is None and len(lista) >= MAX_CONTAS_RIOT:
            raise ErroVinculo(
                f"Limite de {MAX_CONTAS_RIOT} contas por pessoa. "
                "Remova uma com `/desvincular` antes de adicionar outra."
            )
        nova = {
            "puuid": puuid,
            "riot_id": riot_nome,
            "elo": elo,
            "elo_ordem": elo_ordem,
            "elo_em": time.time(),
        }
        if existente is None:
            lista.append(nova)
        else:
            lista[lista.index(existente)] = nova
        contas_riot[chave] = {"contas": lista}
        try:
            salvar_contas_riot()
        except OSError as exc:
            if registro is None:
                contas_riot.pop(chave, None)
            else:
                contas_riot[chave] = registro
            log.exception("Não consegui salvar a conta Riot: %s", exc)
            raise ErroVinculo(
                "Não consegui salvar o vínculo em /data/contas_riot.json.", parar=True
            ) from exc
        return riot_nome, elo, len(lista)


# ======================= VINCULAR VÁRIAS CONTAS =======================
MAX_LINHAS_LINK_VARIOS = 100


def resolver_membro(guild: discord.Guild, texto: str) -> discord.Member | None:
    """Acha o membro por menção, ID, nome de usuário ou apelido (se for único)."""
    texto = texto.strip()
    numero = re.fullmatch(r"<@!?(\d+)>|@?(\d{15,20})", texto)
    if numero:
        return guild.get_member(int(numero.group(1) or numero.group(2)))
    procurado = texto.lstrip("@").strip().casefold()
    if not procurado:
        return None
    for membro in guild.members:
        if membro.name.casefold() == procurado:
            return membro
    candidatos = [
        m for m in guild.members
        if m.display_name.casefold() == procurado or (m.global_name or "").casefold() == procurado
    ]
    return candidatos[0] if len(candidatos) == 1 else None


class ModalLinkVarios(discord.ui.Modal):
    """Janela com uma caixa de texto grande para colar vários Riot IDs."""

    def __init__(self, usuario_id: int, membro: discord.Member | None) -> None:
        super().__init__(title="Vincular vários Riot IDs", timeout=900)
        self.usuario_id = usuario_id
        self.membro = membro
        if membro is not None:
            texto = f"Riot IDs de {membro.display_name}"[:45]
            descricao = "Um Riot ID por linha (Nick#TAG), até 100."
            exemplo = "egirl Mikasa#0000\nmavie#Br666\nMorto D Fome#Fome"
        else:
            texto = "Membro e Riot ID, um por linha"
            descricao = "Formato: usuário: Nick#TAG (nome de usuário, @menção ou ID)."
            exemplo = "sakurinha: egirl Mikasa#0000\nfulano: mavie#Br666"
        self.entrada = discord.ui.Label(
            text=texto,
            description=descricao,
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph, placeholder=exemplo,
                required=True, max_length=4000,
            ),
        )
        self.add_item(self.entrada)

    async def on_submit(self, inter: discord.Interaction) -> None:
        if inter.user.id != self.usuario_id or inter.guild is None:
            await inter.response.send_message("🚫 Este formulário não é seu.", ephemeral=True)
            return
        campo = self.entrada.component
        assert isinstance(campo, discord.ui.TextInput)
        linhas = [linha.strip() for linha in campo.value.splitlines() if linha.strip()]
        if len(linhas) > MAX_LINHAS_LINK_VARIOS:
            await inter.response.send_message(
                f"⚠️ Envie no máximo {MAX_LINHAS_LINK_VARIOS} Riot IDs de uma vez "
                f"(você enviou {len(linhas)}).",
                ephemeral=True,
            )
            return

        # Lê cada linha e separa o que é válido do que tem erro de formato.
        tarefas: list[tuple[str, discord.Member, tuple[str, str]]] = []
        relatorio: list[str] = []
        for linha in linhas:
            if self.membro is not None:
                alvo, texto_riot = self.membro, linha
            else:
                usuario, separador, texto_riot = linha.partition(":")
                alvo = resolver_membro(inter.guild, usuario) if separador else None
                if not separador:
                    relatorio.append(f"❌ `{linha}` - use o formato usuário: Nick#TAG")
                    continue
                if alvo is None:
                    relatorio.append(f"❌ `{linha}` - membro não encontrado")
                    continue
            separado = separar_riot_id(texto_riot)
            if separado is None:
                relatorio.append(f"❌ `{linha}` - Riot ID inválido (use Nick#TAG)")
            elif alvo.bot:
                relatorio.append(f"❌ `{linha}` - bots não podem ter conta vinculada")
            else:
                tarefas.append((linha, alvo, separado))

        await inter.response.defer(ephemeral=True, thinking=True)
        sucesso = 0
        afetados: set[int] = set()
        interrompido = False
        for indice, (linha, alvo, (nome, tag)) in enumerate(tarefas, start=1):
            if indice % 10 == 1 and len(tarefas) > 10:
                try:
                    await inter.edit_original_response(
                        content=f"⏳ Vinculando contas... {indice - 1}/{len(tarefas)}"
                    )
                except discord.HTTPException:
                    pass
            try:
                riot_nome, elo, _total = await vincular_conta_riot(alvo.id, nome, tag)
            except ErroVinculo as exc:
                relatorio.append(f"❌ `{linha}` - {exc}")
                if exc.parar:
                    interrompido = True
                    break
                continue
            sucesso += 1
            afetados.add(alvo.id)
            relatorio.append(
                f"✅ {discord.utils.escape_markdown(riot_nome)} → {alvo.mention} ({elo})"
            )

        for usuario_id in afetados:
            await atualizar_paineis_do_jogador(inter.guild, usuario_id)

        resumo = (
            f"📚 **{sucesso} de {len(linhas)} conta(s) vinculada(s).**"
            + ("\n⚠️ Processo interrompido por erro na API da Riot." if interrompido else "")
        )
        texto = resumo + "\n\n" + "\n".join(relatorio)
        if len(texto) <= 1900:
            await inter.edit_original_response(
                content=texto, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            # Relatório grande vai como arquivo de texto.
            await inter.edit_original_response(content=resumo + "\nDetalhes no arquivo abaixo.")
            await inter.followup.send(
                file=discord.File(
                    io.BytesIO("\n".join(relatorio).encode("utf-8")),
                    filename="vinculos.txt",
                ),
                ephemeral=True,
            )


@bot.tree.command(
    name="desvincular", description="Remove uma conta do League of Legends vinculada"
)
@app_commands.describe(
    conta="Conta Riot que será removida",
    membro="Remover de outro membro (só o dono ou o cargo autorizado)",
)
@app_commands.autocomplete(conta=autocompletar_conta_riot)
async def desvincular(
    inter: discord.Interaction, conta: str, membro: Optional[discord.Member] = None
) -> None:
    alvo = membro or inter.user
    if alvo.id != inter.user.id and not await pode_gerenciar_contas(inter):
        await inter.response.send_message(
            "🔒 Só o dono ou quem tem o cargo autorizado pode remover contas de outros membros.",
            ephemeral=True,
        )
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    chave = str(alvo.id)
    async with trava_contas_riot:
        registro = contas_riot.get(chave)
        lista = list(registro.get("contas", [])) if isinstance(registro, dict) else []
        escolhida = encontrar_conta_riot(lista, conta)
        if escolhida is None:
            await inter.followup.send(
                "⚠️ Conta não encontrada. Escolha uma das opções que aparecem ao digitar.",
                ephemeral=True,
            )
            return
        lista.remove(escolhida)
        if lista:
            contas_riot[chave] = {"contas": lista}
        else:
            contas_riot.pop(chave, None)
        try:
            salvar_contas_riot()
        except OSError as exc:
            contas_riot[chave] = registro
            log.exception("Não consegui salvar a remoção da conta Riot: %s", exc)
            await inter.followup.send(
                "⚠️ Não consegui salvar em /data/contas_riot.json.", ephemeral=True
            )
            return

    if inter.guild is not None:
        await atualizar_paineis_do_jogador(inter.guild, alvo.id)
    await inter.followup.send(
        f"🗑️ Conta removida: **{discord.utils.escape_markdown(escolhida.get('riot_id', ''))}**"
        + ("" if alvo.id == inter.user.id else f" (de {alvo.mention})")
        + f"\n📚 Contas restantes: **{len(lista)}**",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ======================= ESTATÍSTICAS DOS JOGADORES =======================
# /estatisticas analisa as partidas recentes de Summoner's Rift (normal e ranqueada)
# de quem usou /link: campeões mais jogados, taxa de vitória e rotas.
PARTIDAS_ANALISADAS = 15  # Por jogador. Um time de 5 cabe no limite de 2 minutos.
FILAS_SUMMONERS_RIFT = {400, 420, 430, 440, 490}  # Normal, Solo/Duo, Flex, Escolha Rápida.
ESTATISTICAS_VALIDADE_SEGUNDOS = 10 * 60
NOMES_ROTA = {
    "TOP": "Topo",
    "JUNGLE": "Selva",
    "MIDDLE": "Meio",
    "BOTTOM": "Atirador",
    "UTILITY": "Suporte",
}
NOMES_FILAS_LOL = {
    400: "Normal Alternada",
    420: "Ranqueada Solo/Duo",
    430: "Normal às Cegas",
    440: "Ranqueada Flex",
    450: "ARAM",
    490: "Escolha Rápida",
    1700: "Arena",
}
_nomes_campeoes: dict[int, str] = {}
_ids_ddragon: dict[int, str] = {}  # Número do campeão -> nome interno (ex.: 62 -> MonkeyKing).
_versao_ddragon = ""
# Ícones dos campeões viram emojis da aplicação do bot (criados só quando aparecem).
_emojis_campeoes: dict[str, str] = {}
_emojis_carregados = False
_trava_emojis = asyncio.Lock()
# Partidas do LoL não mudam depois de terminar; guardamos um resumo para não reconsultar.
_cache_partidas_lol: dict[str, dict[str, Any] | None] = {}
_cache_estatisticas: dict[str, tuple[float, dict[str, Any]]] = {}


async def nomes_campeoes() -> dict[int, str]:
    """Nomes dos campeões em português, pelo Data Dragon público da Riot."""
    global _versao_ddragon
    if _nomes_campeoes:
        return _nomes_campeoes
    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sessao:
            async with sessao.get("https://ddragon.leagueoflegends.com/api/versions.json") as r:
                versao = (await r.json())[0]
            async with sessao.get(
                f"https://ddragon.leagueoflegends.com/cdn/{versao}/data/pt_BR/champion.json"
            ) as r:
                dados = await r.json()
        for campeao in dados["data"].values():
            _nomes_campeoes[int(campeao["key"])] = campeao["name"]
            _ids_ddragon[int(campeao["key"])] = campeao["id"]
        _versao_ddragon = versao
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError) as exc:
        log.warning("Não consegui carregar os nomes dos campeões: %s", exc)
    return _nomes_campeoes


def nome_emoji_campeao(campeao_id: int) -> str:
    # Emojis só aceitam letras, números e "_" (2 a 32 caracteres).
    interno = re.sub(r"[^A-Za-z0-9_]", "", _ids_ddragon.get(campeao_id, str(campeao_id)))
    return f"lol_{interno}"[:32]


async def icones_campeoes(ids: set[int]) -> dict[int, str]:
    """Ícone oficial de cada campeão (Data Dragon) como emoji da aplicação do bot.

    O emoji é criado só na primeira vez que o campeão aparece e fica salvo no
    Discord, então reinícios do bot não precisam enviar as imagens de novo.
    """
    global _emojis_carregados
    if not _ids_ddragon or not _versao_ddragon:
        return {}
    async with _trava_emojis:
        if not _emojis_carregados:
            try:
                for emoji in await bot.fetch_application_emojis():
                    _emojis_campeoes[emoji.name] = str(emoji)
                _emojis_carregados = True
            except (discord.HTTPException, AttributeError) as exc:
                log.warning("Não consegui listar os emojis do bot: %s", exc)
                return {}
        faltando = [
            cid for cid in ids
            if cid in _ids_ddragon and nome_emoji_campeao(cid) not in _emojis_campeoes
        ]
        if faltando:
            timeout = aiohttp.ClientTimeout(total=10, connect=4)
            async with aiohttp.ClientSession(timeout=timeout) as sessao:
                for cid in faltando:
                    url = (
                        f"https://ddragon.leagueoflegends.com/cdn/{_versao_ddragon}"
                        f"/img/champion/{quote(_ids_ddragon[cid], safe='')}.png"
                    )
                    try:
                        async with sessao.get(url) as resposta:
                            if resposta.status != 200:
                                continue
                            imagem = await resposta.read()
                        emoji = await bot.create_application_emoji(
                            name=nome_emoji_campeao(cid), image=imagem
                        )
                        _emojis_campeoes[emoji.name] = str(emoji)
                    except (aiohttp.ClientError, asyncio.TimeoutError,
                            discord.HTTPException, AttributeError) as exc:
                        log.warning("Não consegui criar o ícone do campeão %s: %s", cid, exc)
    return {
        cid: _emojis_campeoes[nome_emoji_campeao(cid)]
        for cid in ids if nome_emoji_campeao(cid) in _emojis_campeoes
    }


async def resumo_partida_lol(match_id: str) -> dict[str, Any] | None:
    """Resumo de uma partida: modo, horário e campeão/rota/resultado de cada participante."""
    if match_id in _cache_partidas_lol:
        return _cache_partidas_lol[match_id]
    dados = await chamar_riot(
        f"https://{RIOT_REGIAO}.api.riotgames.com/lol/match/v5/matches/{quote(match_id, safe='')}"
    )
    info = dados.get("info", {}) if isinstance(dados, dict) else {}
    resumo = None
    # Partidas encerradas por remake (menos de 5 minutos) são ignoradas.
    if info.get("participants") and info.get("gameDuration", 0) >= 300:
        fila = int(info.get("queueId", 0))
        resumo = {
            "fila": fila,
            # Só Summoner's Rift (normal e ranqueada) entra nas estatísticas.
            "summoners_rift": fila in FILAS_SUMMONERS_RIFT,
            "fim": int(info.get("gameEndTimestamp") or 0) // 1000,
            "participantes": [
                {
                    "puuid": p.get("puuid"),
                    "campeao_id": int(p.get("championId", 0)),
                    "campeao": p.get("championName") or "",
                    "rota": p.get("teamPosition") or "",
                    "vitoria": bool(p.get("win")),
                }
                for p in info.get("participants", [])
            ],
        }
    _cache_partidas_lol[match_id] = resumo
    while len(_cache_partidas_lol) > 3000:
        _cache_partidas_lol.pop(next(iter(_cache_partidas_lol)))
    return resumo


async def dados_estatisticas(puuid: str, quantidade: int = PARTIDAS_ANALISADAS) -> dict[str, Any]:
    """Partidas recentes, última partida jogada e campeões de maior maestria."""
    chave_cache = f"{puuid}:{quantidade}"
    guardado = _cache_estatisticas.get(chave_cache)
    if guardado and time.time() - guardado[0] < ESTATISTICAS_VALIDADE_SEGUNDOS:
        return guardado[1]
    ids = await chamar_riot(
        f"https://{RIOT_REGIAO}.api.riotgames.com/lol/match/v5/matches/by-puuid/"
        f"{quote(puuid, safe='')}/ids?start=0&count={quantidade}"
    )
    try:
        maestria = await chamar_riot(
            f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/champion-mastery/v4/"
            f"champion-masteries/by-puuid/{quote(puuid, safe='')}/top?count=3"
        )
    except ErroRiot as exc:
        log.warning("Não consegui buscar a maestria: %s", exc)
        maestria = []
    jogos = []
    recente = None
    # A Riot devolve os IDs da partida mais nova para a mais antiga.
    for match_id in ids if isinstance(ids, list) else []:
        try:
            resumo = await resumo_partida_lol(str(match_id))
        except ErroRiot as exc:
            if exc.status in (401, 403, 429):
                raise
            continue
        if not resumo:
            continue
        participante = next(
            (p for p in resumo["participantes"] if p["puuid"] == puuid), None
        )
        if not participante:
            continue
        if recente is None:
            # Última partida jogada, em qualquer modo (ARAM, Arena, etc.).
            recente = {**participante, "fila": resumo["fila"], "fim": resumo["fim"]}
        if resumo["summoners_rift"]:
            jogos.append(participante)
    dados = {
        "jogos": jogos,
        "recente": recente,
        "maestria": [
            int(m.get("championId", 0)) for m in (maestria if isinstance(maestria, list) else [])
        ],
    }
    _cache_estatisticas[chave_cache] = (time.time(), dados)
    return dados


def _porcentagem(vitorias: int, jogos: int) -> int:
    return round(100 * vitorias / jogos) if jogos else 0


def _melhor(grupos: list[dict[str, Any]]) -> dict[str, Any]:
    """Maior taxa de vitória, exigindo ao menos 2 jogos quando possível."""
    candidatos = [g for g in grupos if g["j"] >= 2] or grupos
    return max(candidatos, key=lambda g: (g["v"] / g["j"], g["j"]))


def ids_campeoes_estatisticas(dados: dict[str, Any]) -> set[int]:
    """Campeões que podem aparecer no texto de estatísticas de um jogador."""
    ids = {j["campeao_id"] for j in dados["jogos"]} | set(dados["maestria"])
    if dados.get("recente"):
        ids.add(dados["recente"]["campeao_id"])
    return ids


def texto_estatisticas(
    conta: dict[str, Any], dados: dict[str, Any], nomes: dict[int, str],
    icones: dict[int, str] | None = None,
) -> str:
    icones = icones or {}

    def campeao(cid: int, reserva: str = "") -> str:
        # Mostra o ícone do campeão; se não houver ícone, mostra o nome.
        return icones.get(cid) or nomes.get(cid) or reserva or f"Campeão {cid}"

    linhas = [
        f"🔗 {discord.utils.escape_markdown(conta.get('riot_id', ''))} • "
        f"{conta.get('elo') or 'Sem ranque'}"
    ]
    recente = dados.get("recente")
    if recente:
        nome_recente = campeao(recente["campeao_id"], recente["campeao"])
        detalhes = [NOMES_FILAS_LOL.get(recente["fila"], "Outro modo")]
        if recente["rota"] in NOMES_ROTA and recente["fila"] in FILAS_SUMMONERS_RIFT:
            detalhes.append(NOMES_ROTA[recente["rota"]])
        detalhes.append("Vitória" if recente["vitoria"] else "Derrota")
        quando = f" <t:{recente['fim']}:R>" if recente.get("fim") else ""
        linhas.append(
            f"🕹️ Último campeão jogado: {nome_recente} ({' • '.join(detalhes)}){quando}"
        )
    if dados["maestria"]:
        linhas.append(
            "⭐ Maior maestria: " + " ".join(campeao(cid) for cid in dados["maestria"])
        )
    jogos = dados["jogos"]
    if not jogos:
        linhas.append("ℹ️ Nenhuma partida recente de Summoner's Rift encontrada.")
        return "\n".join(linhas)

    por_campeao: dict[int, dict[str, Any]] = {}
    por_rota: dict[str, dict[str, Any]] = {}
    for jogo in jogos:
        nome = campeao(jogo["campeao_id"], jogo["campeao"])
        grupo = por_campeao.setdefault(jogo["campeao_id"], {"nome": nome, "j": 0, "v": 0})
        grupo["j"] += 1
        grupo["v"] += int(jogo["vitoria"])
        if jogo["rota"] in NOMES_ROTA:
            rota = por_rota.setdefault(
                jogo["rota"], {"nome": NOMES_ROTA[jogo["rota"]], "j": 0, "v": 0}
            )
            rota["j"] += 1
            rota["v"] += int(jogo["vitoria"])

    campeoes = list(por_campeao.values())
    mais_jogados = sorted(campeoes, key=lambda c: (-c["j"], -c["v"]))[:3]
    linhas.append(
        f"📊 Mais jogados ({len(jogos)} partidas): "
        + " • ".join(
            f"{c['nome']} {c['j']}j ({_porcentagem(c['v'], c['j'])}% vit.)" for c in mais_jogados
        )
    )
    melhor = _melhor(campeoes)
    if melhor["v"]:
        linhas.append(
            f"🏆 Melhor campeão: {melhor['nome']} - {melhor['v']}V {melhor['j'] - melhor['v']}D "
            f"({_porcentagem(melhor['v'], melhor['j'])}%)"
        )
    else:
        linhas.append("🏆 Melhor campeão: ainda sem vitórias nas partidas analisadas")
    mais_derrotas = max(campeoes, key=lambda c: (c["j"] - c["v"], c["j"]))
    derrotas = mais_derrotas["j"] - mais_derrotas["v"]
    if derrotas:
        linhas.append(
            f"📉 Mais derrotas com: {mais_derrotas['nome']} - {derrotas} derrota(s) "
            f"em {mais_derrotas['j']} jogo(s)"
        )
    else:
        linhas.append("📉 Mais derrotas com: nenhuma derrota nas partidas analisadas")

    if por_rota:
        rotas = list(por_rota.values())
        mais_jogada = max(rotas, key=lambda r: (r["j"], r["v"]))
        mais_vitorias = max(rotas, key=lambda r: (r["v"], -r["j"]))
        melhor_rota = _melhor(rotas)
        linhas.append(f"🧭 Rota mais jogada: {mais_jogada['nome']} ({mais_jogada['j']} jogos)")
        linhas.append(
            f"✅ Rota com mais vitórias: {mais_vitorias['nome']} ({mais_vitorias['v']} vitórias)"
        )
        linhas.append(
            f"🎯 Melhor rota: **{melhor_rota['nome']}** - "
            f"{_porcentagem(melhor_rota['v'], melhor_rota['j'])}% de vitórias "
            f"em {melhor_rota['j']} jogo(s)"
        )
    texto = "\n".join(linhas)
    return texto if len(texto) <= 1024 else texto[:1020] + "…"


@bot.tree.command(
    name="estatisticas",
    description="Campeões mais jogados, taxa de vitória e rotas de um time ou jogador",
)
@app_commands.describe(
    equipe="Um time da partida em andamento, ou Todos os times de uma vez",
    jogador="Ver apenas um jogador (se vazio e sem equipe, mostra você)",
    partida="Número da partida, se houver mais de uma em andamento",
    conta_riot="Qual conta Riot ver (do jogador escolhido ou sua). Se vazio, mostra todas",
)
@app_commands.rename(conta_riot="conta")
@app_commands.choices(
    equipe=[
        app_commands.Choice(name="👥 Todos os times", value="todos"),
        *[
            app_commands.Choice(name=f"{emoji} {nome}", value=chave)
            for chave, emoji, nome in EQUIPES
        ],
    ]
)
@app_commands.autocomplete(conta_riot=autocompletar_conta_riot)
@app_commands.guild_only()
async def estatisticas(
    inter: discord.Interaction,
    equipe: app_commands.Choice[str] | None = None,
    jogador: discord.Member | None = None,
    partida: Optional[app_commands.Range[int, 1, 99]] = None,
    conta_riot: Optional[str] = None,
) -> None:
    guild = inter.guild
    if guild is None:
        await inter.response.send_message("Use este comando no servidor.", ephemeral=True)
        return
    if not RIOT_API_KEY:
        await inter.response.send_message(
            "⚠️ A API da Riot ainda não foi configurada no bot (RIOT_API_KEY).",
            ephemeral=True,
        )
        return

    # Cada grupo vira um embed: (título, IDs dos jogadores).
    grupos: list[tuple[str, list[int]]] = []
    if equipe is None:
        grupos.append((
            f"📊 Estatísticas de {(jogador or inter.user).display_name}",
            [(jogador or inter.user).id],
        ))
    else:
        ativas = partidas_do_servidor(guild.id)
        escolhida = None
        if partida is not None:
            escolhida = next((p for _c, p in ativas if p.get("numero") == partida), None)
        else:
            # Usa a partida em que quem pediu está jogando ou assistindo.
            uid = str(inter.user.id)
            escolhida = next(
                (p for _c, p in ativas
                 if uid in p.get("participantes", {}) or uid in p.get("espectadores", {})),
                None,
            )
            if escolhida is None and len(ativas) == 1:
                escolhida = ativas[0][1]
        if escolhida is None:
            texto = (
                "ℹ️ Não há partida em andamento." if not ativas else
                "⚠️ Há mais de uma partida em andamento. Informe o número em `partida`."
                if partida is None else f"⚠️ Não encontrei a Partida {partida}."
            )
            await inter.response.send_message(texto, ephemeral=True)
            return
        numero = escolhida.get("numero", 1)
        participantes = escolhida.get("participantes", {})
        for chave, emoji, nome in EQUIPES:
            if equipe.value not in ("todos", chave):
                continue
            ids_time = [
                int(uid) for uid, dados in participantes.items() if dados.get("time") == chave
            ]
            if ids_time:
                grupos.append((f"📊 Partida {numero} | {emoji} Time {nome}", ids_time))
        if not grupos:
            await inter.response.send_message(
                f"⚠️ O time {equipe.name} não está na Partida {numero}.", ephemeral=True
            )
            return

    # Contas Riot de cada jogador. Uma conta específica só vale ao ver um jogador.
    grupos_contas: list[tuple[str, list[tuple[str, str | None, list[dict[str, Any]]]]]] = []
    for titulo, ids in grupos:
        itens = []
        for uid in ids[:6]:
            membro_alvo = guild.get_member(uid)
            nome_membro = membro_alvo.display_name if membro_alvo else f"Jogador {uid}"
            avatar = membro_alvo.display_avatar.url if membro_alvo else None
            contas = contas_do_usuario(uid)
            if conta_riot and equipe is None:
                escolhida_conta = encontrar_conta_riot(contas, conta_riot)
                if escolhida_conta is None:
                    await inter.response.send_message(
                        f"⚠️ Não encontrei essa conta em {nome_membro}. "
                        "Escolha uma das opções que aparecem ao digitar.",
                        ephemeral=True,
                    )
                    return
                contas = [escolhida_conta]
            itens.append((nome_membro, avatar, contas))
        grupos_contas.append((titulo, itens))

    await inter.response.defer(ephemeral=True, thinking=True)
    nomes = await nomes_campeoes()
    # Com muitas contas, analisa menos partidas de cada uma para não demorar demais.
    total_contas = sum(len(c) for _t, itens in grupos_contas for _n, _a, c in itens)
    quantidade = PARTIDAS_ANALISADAS if total_contas <= 10 else 8
    rodape = f"Últimas {quantidade} partidas de Summoner's Rift (normal e ranqueada)."
    chave_invalida = False

    def cartao(nome_membro: str, avatar: str | None, texto: str) -> discord.Embed:
        # Um cartão por conta: avatar e apelido do Discord no topo, estatísticas embaixo.
        embed = discord.Embed(description=texto, color=0x5865F2)
        embed.set_author(name=nome_membro[:256], icon_url=avatar)
        return embed

    # Cada time é enviado assim que fica pronto, sem esperar os outros.
    for titulo, itens in grupos_contas:
        cartoes: list[discord.Embed] = []
        for nome_membro, avatar, contas in itens:
            if not contas:
                cartoes.append(cartao(
                    nome_membro, avatar, "🔗 Ainda não vinculou a conta (`/link Nick#TAG`)."
                ))
                continue
            for conta in contas:
                if chave_invalida:
                    cartoes.append(cartao(nome_membro, avatar, "⚠️ Consulta interrompida."))
                    continue
                try:
                    dados = await dados_estatisticas(conta["puuid"], quantidade)
                except ErroRiot as exc:
                    cartoes.append(cartao(
                        nome_membro, avatar,
                        f"🔗 {discord.utils.escape_markdown(conta.get('riot_id', ''))}\n⚠️ {exc}",
                    ))
                    chave_invalida = exc.status in (401, 403)
                    continue
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
                    log.warning("Falha nas estatísticas de %s: %s", nome_membro, exc)
                    cartoes.append(cartao(
                        nome_membro, avatar,
                        f"🔗 {discord.utils.escape_markdown(conta.get('riot_id', ''))}\n"
                        "⚠️ Não consegui falar com a Riot agora.",
                    ))
                    continue
                icones = await icones_campeoes(ids_campeoes_estatisticas(dados))
                cartoes.append(cartao(
                    nome_membro, avatar, texto_estatisticas(conta, dados, nomes, icones)
                ))
        await enviar_cartoes(inter, titulo, cartoes, rodape)


async def enviar_cartoes(
    inter: discord.Interaction, titulo: str, cartoes: list[discord.Embed], rodape: str
) -> None:
    """Envia os cartões em mensagens de até 10 embeds, respeitando o limite de tamanho."""
    lotes: list[list[discord.Embed]] = []
    tamanho = 0
    for cartao in cartoes:
        peso = len(cartao.description or "") + len(cartao.author.name or "")
        if not lotes or len(lotes[-1]) >= 10 or tamanho + peso > 5500:
            lotes.append([])
            tamanho = len(rodape)
        lotes[-1].append(cartao)
        tamanho += peso
    for indice, lote in enumerate(lotes):
        lote[-1].set_footer(text=rodape)
        await inter.followup.send(
            content=f"**{titulo}**" + (" (continuação)" if indice else ""),
            embeds=lote,
            ephemeral=True,
        )


# ======================= FILA DE JOGADORES =======================
# /queue cria uma mensagem pública. Os inscritos ficam na mesma sala de espera.
# Inscrições (colunas) não são os times definitivos: o sorteio ocorre em Iniciar.


def tamanho_time(fila: dict[str, Any]) -> int:
    # Filas antigas (sem "tamanho") continuam com 5 jogadores por equipe.
    return int(fila.get("tamanho", TAMANHO_TIME))


def fila_cheia(fila: dict[str, Any]) -> bool:
    return len(fila.get("inscritos", [])) == tamanho_time(fila) * int(fila["equipes"])


def fila_total_paginas(fila: dict[str, Any]) -> int:
    # Duas colunas por página, cada uma com o tamanho de um time.
    tam = tamanho_time(fila)
    capacidade = tam * int(fila["equipes"])
    return max(1, -(-capacidade // (2 * tam)))


def fila_pagina(fila: dict[str, Any]) -> int:
    return min(max(0, int(fila.get("pagina", 0))), fila_total_paginas(fila) - 1)


def filas_abertas(guild_id: int) -> list[tuple[str, dict[str, Any]]]:
    """Filas abertas do servidor, da mais antiga para a mais nova."""
    abertas = [
        (chave, fila) for chave, fila in filas.items()
        if fila.get("guild_id") == guild_id and fila.get("status") == "aberta"
    ]
    return sorted(abertas, key=lambda item: int(item[0]))


def nome_modo_fila(fila: dict[str, Any]) -> str:
    numero = int(fila["equipes"])
    tam = tamanho_time(fila)
    return f"{tam} vs {tam}" if numero == 2 else f"{numero} equipes de {tam}"


async def cancelar_fila(guild: discord.Guild, chave: str) -> bool:
    """Cancela uma fila aberta. Deve ser chamada com a trava do servidor."""
    fila = filas.get(chave)
    if not fila or fila.get("status") != "aberta" or fila.get("guild_id") != guild.id:
        return False
    fila["status"] = "cancelada"
    await atualizar_mensagem_fila(guild, fila, fechada=True)
    filas.pop(chave, None)
    salvar_filas()
    return True


def montar_embed_fila(fila: dict[str, Any]) -> discord.Embed:
    numero = int(fila["equipes"])
    tam = tamanho_time(fila)
    inscritos = fila.get("inscritos", [])
    capacidade = tam * numero
    cheio = len(inscritos) == capacidade
    pagina = fila_pagina(fila)
    modo = (
        f"🏅 **Modo:** {tam} vs {tam}" if numero == 2
        else f"🏅 **Equipes:** {numero} times de {tam} jogadores"
    )
    embed = discord.Embed(
        title="🎮 League of Legends | FILA PARA PARTIDA",
        description=(
            f"🎙️ **Sala de espera:** <#{fila['origem_id']}>\n"
            f"👥 **Vagas preenchidas:** {len(inscritos)}/{capacidade}\n"
            f"{modo}\n\n"
            + (
                "✅ **Fila completa! O botão Iniciar está disponível para o dono.**"
                if cheio else "Clique em **Entrar** para participar ou **Sair** para cancelar."
            )
        ),
        color=cor_do_embed_fila(fila),
    )
    # Sempre exatamente duas colunas. Páginas extras não criam novas colunas.
    for coluna, (emoji, nome) in enumerate((("🔴", "Vermelho"), ("🔵", "Azul"))):
        inicio = pagina * 2 * tam + coluna * tam
        linhas = []
        for indice in range(inicio, inicio + tam):
            if indice >= capacidade:
                linhas.append("▫️ Sem vaga")
            elif indice < len(inscritos):
                # Elo apenas informativo (quem usou /link); não afeta o sorteio.
                linhas.append(f"<@{inscritos[indice]}>{texto_elo(inscritos[indice])}")
            else:
                linhas.append("⬜ Livre")
        embed.add_field(name=f"{emoji} Coluna {nome}", value="\n".join(linhas), inline=True)
    if fila.get("imagem_nome"):
        embed.set_image(url=f"attachment://{fila['imagem_nome']}")
    rodape = "As colunas são vagas de inscrição; os times só serão sorteados ao iniciar."
    if fila_total_paginas(fila) > 1:
        rodape = f"Inscrições {pagina + 1}/{fila_total_paginas(fila)} | " + rodape
    embed.set_footer(text=rodape)
    return embed


async def dono_da_fila(inter: discord.Interaction) -> bool:
    # Reconhece o dono do servidor, o dono da aplicação Discord e a equipe proprietária.
    if inter.guild and inter.user.id == inter.guild.owner_id:
        return True
    try:
        info = await bot.application_info()
        if info.team:
            return inter.user.id == info.team.owner_id
        return inter.user.id == info.owner.id
    except discord.HTTPException:
        return False


async def atualizar_mensagem_fila(
    guild: discord.Guild, fila: dict[str, Any], *, fechada: bool = False
) -> None:
    canal = guild.get_channel(fila["channel_id"])
    if not isinstance(canal, discord.TextChannel):
        return
    try:
        mensagem = await canal.fetch_message(fila["message_id"])
        if fechada:
            embed = montar_embed_fila(fila)
            cancelada = fila.get("status") == "cancelada"
            embed.title = "🛑 Fila cancelada" if cancelada else "🎲 Fila encerrada | Times sorteados"
            embed.description = (
                "A fila foi cancelada. Use `/queue` para abrir outra."
                if cancelada else "A partida começou. Confira abaixo a mensagem com os times sorteados."
            )
            embed.color = cor_do_embed_fila(fila) if fila.get("cor_embed") else 0xF59E0B
            await mensagem.edit(embed=embed, view=None)
        else:
            await mensagem.edit(
                embed=montar_embed_fila(fila), view=FilaView(
                    cheia=fila_cheia(fila), pagina=fila_pagina(fila), paginas=fila_total_paginas(fila)
                ),
                **opcoes_imagem_edicao_fila(guild.id, fila, mensagem),
            )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.warning("Não foi possível atualizar a mensagem da fila: %s", exc)


def valida_fila_interacao(inter: discord.Interaction) -> dict[str, Any] | None:
    if inter.guild_id is None or not inter.message:
        return None
    fila = filas.get(str(inter.message.id))
    if (not fila or fila.get("status") != "aberta"
            or fila.get("guild_id") != inter.guild_id):
        return None
    return fila


class FilaView(discord.ui.View):
    """Botões persistentes, restaurados no reinício com o ID da mensagem."""

    def __init__(self, cheia: bool, pagina: int = 0, paginas: int = 1) -> None:
        super().__init__(timeout=None)
        if cheia:
            self.remove_item(self.entrar)
        else:
            self.remove_item(self.iniciar)
        if paginas <= 1:
            self.remove_item(self.anterior)
            self.remove_item(self.proximo)
        else:
            self.anterior.disabled = pagina <= 0
            self.proximo.disabled = pagina >= paginas - 1

    @discord.ui.button(
        label="Entrar", emoji="✅", style=discord.ButtonStyle.success,
        custom_id="queue:entrar"
    )
    async def entrar(self, inter: discord.Interaction, botao: discord.ui.Button) -> None:
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Use a fila dentro de um servidor.", ephemeral=True)
            return
        # Primeira vez sem conta vinculada: pede o Riot ID antes de entrar.
        if RIOT_API_KEY and not inter.user.bot and not contas_do_usuario(inter.user.id):
            await inter.response.send_modal(ModalVincularAoEntrar(self))
            return
        # Atualiza o elo salvo (se estiver antigo) antes de redesenhar o painel.
        await atualizar_elo_se_preciso(inter.user.id)
        async with trava_servidor(inter.guild.id):
            fila = valida_fila_interacao(inter)
            if fila is None:
                await inter.response.send_message("⚠️ Esta fila não está mais aberta.", ephemeral=True)
                return
            uid = inter.user.id
            if uid in fila["inscritos"]:
                await inter.response.send_message("✅ Você já está na fila.", ephemeral=True)
                return
            if fila_cheia(fila):
                await inter.response.send_message("⚠️ Fila completa.", ephemeral=True)
                return
            if inter.user.bot or not inter.user.voice or not inter.user.voice.channel:
                await inter.response.send_message(
                    "🎙️ Entre primeiro na sala de voz indicada na fila.", ephemeral=True
                )
                return
            if inter.user.voice.channel.id != fila["origem_id"]:
                await inter.response.send_message(
                    f"🎙️ Para entrar nesta fila, conecte-se a <#{fila['origem_id']}>.",
                    ephemeral=True,
                )
                return
            fila["inscritos"].append(uid)
            salvar_filas()
            await inter.response.edit_message(
                embed=montar_embed_fila(fila), view=FilaView(
                    cheia=fila_cheia(fila), pagina=fila_pagina(fila), paginas=fila_total_paginas(fila)
                ),
                **opcoes_imagem_edicao_fila(inter.guild.id, fila, inter.message),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @discord.ui.button(
        label="Sair", emoji="🚪", style=discord.ButtonStyle.secondary,
        custom_id="queue:sair"
    )
    async def sair(self, inter: discord.Interaction, botao: discord.ui.Button) -> None:
        if inter.guild is None:
            await inter.response.send_message("Fila não encontrada.", ephemeral=True)
            return
        async with trava_servidor(inter.guild.id):
            fila = valida_fila_interacao(inter)
            if fila is None:
                await inter.response.send_message("⚠️ A fila foi encerrada.", ephemeral=True)
                return
            if inter.user.id not in fila["inscritos"]:
                await inter.response.send_message("Você não está inscrito na fila.", ephemeral=True)
                return
            fila["inscritos"].remove(inter.user.id)
            salvar_filas()
            await inter.response.edit_message(
                embed=montar_embed_fila(fila), view=FilaView(cheia=False, pagina=fila_pagina(fila), paginas=fila_total_paginas(fila)),
                **opcoes_imagem_edicao_fila(inter.guild.id, fila, inter.message),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @discord.ui.button(
        label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary,
        custom_id="queue:anterior", row=1,
    )
    async def anterior(self, inter: discord.Interaction, botao: discord.ui.Button) -> None:
        await self.trocar_pagina(inter, -1)

    @discord.ui.button(
        label="Próxima", emoji="▶️", style=discord.ButtonStyle.secondary,
        custom_id="queue:proximo", row=1,
    )
    async def proximo(self, inter: discord.Interaction, botao: discord.ui.Button) -> None:
        await self.trocar_pagina(inter, 1)

    async def trocar_pagina(self, inter: discord.Interaction, diferenca: int) -> None:
        if inter.guild is None:
            await inter.response.send_message("Fila indisponível.", ephemeral=True)
            return
        async with trava_servidor(inter.guild.id):
            fila = valida_fila_interacao(inter)
            if fila is None:
                await inter.response.send_message("⚠️ A fila foi encerrada.", ephemeral=True)
                return
            fila["pagina"] = min(
                max(0, fila_pagina(fila) + diferenca), fila_total_paginas(fila) - 1
            )
            salvar_filas()
            await inter.response.edit_message(
                embed=montar_embed_fila(fila),
                view=FilaView(
                    cheia=fila_cheia(fila), pagina=fila_pagina(fila),
                    paginas=fila_total_paginas(fila),
                ),
                **opcoes_imagem_edicao_fila(inter.guild.id, fila, inter.message),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @discord.ui.button(
        label="Iniciar", emoji="🎮", style=discord.ButtonStyle.primary,
        custom_id="queue:iniciar"
    )
    async def iniciar(self, inter: discord.Interaction, botao: discord.ui.Button) -> None:
        if inter.guild is None:
            await inter.response.send_message("Fila não encontrada.", ephemeral=True)
            return
        if not await dono_da_fila(inter):
            await inter.response.send_message(
                "🔒 Apenas o dono do servidor ou do bot pode iniciar a partida.", ephemeral=True
            )
            return

        await inter.response.defer(thinking=True, ephemeral=True)
        guild = inter.guild
        async with trava_servidor(guild.id):
            fila = valida_fila_interacao(inter)
            if fila is None or not fila_cheia(fila):
                await inter.followup.send("⚠️ A fila ainda não está completa ou já foi encerrada.")
                return
            bot_membro = guild.me
            if not bot_membro or not (
                bot_membro.guild_permissions.manage_channels
                and bot_membro.guild_permissions.move_members
                and bot_membro.guild_permissions.manage_roles
            ):
                await inter.followup.send(
                    "⚠️ Preciso de **Gerenciar Canais**, **Mover Membros** e "
                    "**Gerenciar Cargos** para criar canais trancados."
                )
                return
            origem = buscar_voz(guild, fila["origem_id"])
            if not origem:
                await inter.followup.send("⚠️ A sala de espera não existe mais.")
                return
            if not origem.permissions_for(bot_membro).move_members:
                await inter.followup.send(
                    "⚠️ Preciso da permissão **Mover Membros** na sala de espera."
                )
                return

            por_id = {m.id: m for m in origem.members if not m.bot}
            ausentes = [uid for uid in fila["inscritos"] if uid not in por_id]
            if ausentes:
                fila["inscritos"] = [uid for uid in fila["inscritos"] if uid in por_id]
                salvar_filas()
                await atualizar_mensagem_fila(guild, fila)
                await inter.followup.send(
                    f"⚠️ {len(ausentes)} jogador(es) saíram da sala de espera. "
                    "A fila foi atualizada; aguarde completar novamente."
                )
                return

            # Os jogadores são embaralhados apenas AQUI, não na ordem da inscrição.
            jogadores = [por_id[uid] for uid in fila["inscritos"]]
            random.SystemRandom().shuffle(jogadores)
            tam = tamanho_time(fila)
            times = {
                chave: jogadores[i * tam:(i + 1) * tam]
                for i, (chave, _emoji, _nome) in enumerate(EQUIPES[:fila["equipes"]])
            }

            fila["status"] = "iniciando"
            salvar_filas()
            # A partida usa o ID da mensagem da fila como chave própria.
            chave_partida = str(fila["message_id"])
            numero = numero_livre_partida(guild.id)
            partida = {
                "guild_id": guild.id,
                "numero": numero,
                "origem_id": origem.id,
                "categoria_id": None,
                "canais": {},
                "vencedor": None,
                "espectadores": {},
                "assistir_channel_id": None,
                "assistir_message_id": None,
                "participantes": {
                    str(m.id): {"origem_id": origem.id, "time": chave}
                    for chave, membros in times.items() for m in membros
                },
            }
            partidas[chave_partida] = partida
            salvar_partidas()
            try:
                categoria = await guild.create_category(
                    f"🔒 PARTIDA {numero} EM ANDAMENTO", reason="Queue: início da partida"
                )
                partida["categoria_id"] = categoria.id
                salvar_partidas()

                for chave, emoji, nome in EQUIPES[:fila["equipes"]]:
                    permissao = permissoes_de_time(guild, bot_membro, times[chave])
                    canal = await guild.create_voice_channel(
                        f"{emoji} Time {nome} 🔒",
                        category=categoria,
                        overwrites=permissao,
                        # Um limite de cinco impediria espectadores com o time completo.
                        # A entrada continua protegida pelas permissões individuais.
                        user_limit=0,
                        reason=f"Queue: canal privado do Time {nome}",
                    )
                    partida["canais"][chave] = canal.id
                    salvar_partidas()
            except (discord.Forbidden, discord.HTTPException, TypeError) as exc:
                log.exception("Falha ao criar os canais privados da fila: %s", exc)
                pendencias = await remover_estrutura(guild, partida)
                if not guild.get_channel(partida.get("categoria_id")) and not canal_times(guild, partida):
                    partidas.pop(chave_partida, None)
                    salvar_partidas()
                    fila["status"] = "aberta"
                    salvar_filas()
                    await atualizar_mensagem_fila(guild, fila)
                else:
                    # Preserva o registro parcial para /partida_cancelar.
                    filas.pop(str(fila["message_id"]), None)
                    salvar_filas()
                    await atualizar_mensagem_fila(guild, fila, fechada=True)
                await inter.followup.send(
                    "❌ Não consegui criar os canais privados. "
                    "Confira permissões e hierarquia de cargos. "
                    + ("Pendências: " + "; ".join(pendencias) if pendencias else "")
                )
                return

            erros = []
            movidos = 0
            for chave, membros in times.items():
                destino = buscar_voz(guild, partida["canais"][chave])
                for membro in membros:
                    if not membro.voice or membro.voice.channel != origem:
                        erros.append(f"{membro.display_name} (saiu da sala)")
                        continue
                    try:
                        await membro.move_to(destino, reason="Sorteio aleatório da queue")
                        movidos += 1
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("Não consegui mover %s: %s", membro.id, exc)
                        erros.append(membro.display_name)

            # A fila vira partida. A finalização usa os mesmos comandos anteriores.
            filas.pop(str(fila["message_id"]), None)
            salvar_filas()
            await atualizar_mensagem_fila(guild, fila, fechada=True)
            embed = discord.Embed(
                title="🎮 League of Legends | EQUIPES SORTEADAS",
                description=(
                    f"🎮 **Partida {numero}**\n"
                    f"🔒 Cada time tem um canal privado: apenas membros sorteados "
                    f"podem conectar.\n🎙️ Canal original: {origem.mention}\n"
                    f"↪️ {movidos}/{len(jogadores)} jogadores movidos."
                ),
                color=(cor_do_embed_fila(fila) if fila.get("cor_embed") else 0x5865F2),
            )
            for chave, emoji, nome in EQUIPES[:fila["equipes"]]:
                canal = buscar_voz(guild, partida["canais"][chave])
                embed.add_field(
                    name=f"{emoji} Time {nome}",
                    value=f"{canal.mention}\n" + "\n".join(m.mention for m in times[chave]),
                    inline=True,
                )
            embed.set_footer(text="Finalize com /partida_finalizar ou /partida_cancelar")
            texto = "⚠️ Não movidos: " + ", ".join(erros[:10]) if erros else None
            # A mensagem de resultados é pública para todos os participantes.
            canal_texto = guild.get_channel(fila["channel_id"])
            if isinstance(canal_texto, discord.TextChannel):
                try:
                    mensagem_resultado = await canal_texto.send(
                        content=texto, embed=embed, view=AssistirView(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    partida["assistir_channel_id"] = canal_texto.id
                    partida["assistir_message_id"] = mensagem_resultado.id
                    salvar_partidas()
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Não foi possível divulgar o resultado: %s", exc)
            await inter.followup.send(
                "✅ Partida iniciada! Confira os times e canais privados na mensagem da fila."
                + (f" ⚠️ {len(erros)} jogador(es) não foram movidos." if erros else ""),
            )


# ======================= ASSISTIR PARTIDA =======================
# O espectador recebe acesso exclusivamente ao canal escolhido e pode falar.
# Administradores podem ignorar restrições de canais do Discord.

class EscolhaEquipeAssistir(discord.ui.View):
    def __init__(self, usuario_id: int, guild_id: int, chave_partida: str) -> None:
        super().__init__(timeout=180)
        self.usuario_id = usuario_id
        self.guild_id = guild_id
        self.chave_partida = chave_partida
        partida = partidas.get(chave_partida) or {}
        chaves = set((partida.get("canais") or {}).keys())
        for chave, emoji, nome in EQUIPES:
            if chave not in chaves:
                continue
            botao = discord.ui.Button(
                label=nome, emoji=emoji, style=discord.ButtonStyle.primary,
            )

            async def escolher(inter: discord.Interaction, time: str = chave) -> None:
                await self.escolher_time(inter, time)

            botao.callback = escolher
            self.add_item(botao)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id or inter.guild_id != self.guild_id:
            await inter.response.send_message(
                "🚫 Apenas quem pediu para assistir pode escolher o time.", ephemeral=True
            )
            return False
        return True

    async def escolher_time(self, inter: discord.Interaction, time: str) -> None:
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Use este botão no servidor.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        async with trava_servidor(inter.guild.id):
            partida = partidas.get(self.chave_partida)
            if not partida or partida.get("vencedor") is not None:
                await inter.followup.send("⚠️ A partida já terminou ou está sendo finalizada.")
                return
            # Com várias partidas, ninguém joga numa e assiste outra ao mesmo tempo.
            for outra_chave, outra in partidas_do_servidor(inter.guild.id):
                if outra_chave == self.chave_partida:
                    continue
                if str(inter.user.id) in outra.get("participantes", {}):
                    await inter.followup.send(
                        f"🎮 Você é jogador da Partida {outra.get('numero', 1)}."
                    )
                    return
                if str(inter.user.id) in outra.get("espectadores", {}):
                    await inter.followup.send(
                        f"🔒 Você já está assistindo à Partida {outra.get('numero', 1)}."
                    )
                    return
            if time not in (partida.get("canais") or {}):
                await inter.followup.send("⚠️ Esse time não está disponível.")
                return
            if str(inter.user.id) in partida.get("participantes", {}):
                await inter.followup.send("🎮 Você já é jogador desta partida.")
                return

            ja_registrado = partida.setdefault("espectadores", {}).get(str(inter.user.id))
            if ja_registrado and ja_registrado["time"] != time:
                await inter.followup.send(
                    "🔒 Você já escolheu um time para assistir nesta partida. "
                    "Não é possível trocar de equipe durante o jogo."
                )
                return

            voz = inter.user.voice
            if voz is None or voz.channel is None:
                await inter.followup.send(
                    "🎙️ Entre em qualquer canal de voz (de preferência a sala de espera) "
                    "e clique novamente para assistir."
                )
                return

            canal = buscar_voz(inter.guild, partida["canais"].get(time))
            bot_membro = inter.guild.me
            if not canal or bot_membro is None:
                await inter.followup.send("⚠️ Canal da equipe indisponível.")
                return
            if not (
                canal.permissions_for(bot_membro).manage_roles
                and canal.permissions_for(bot_membro).move_members
            ):
                await inter.followup.send(
                    "⚠️ O bot precisa de Gerenciar Cargos e Mover Membros neste canal."
                )
                return

            overwrite_anterior = canal.overwrites_for(inter.user)
            try:
                # Espectador pode conectar, ouvir e falar no time escolhido.
                # O outro time continua sem autorizar sua entrada.
                await canal.set_permissions(
                    inter.user,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        stream=True,
                        use_voice_activation=True,
                    ),
                    reason="League of Legends: espectador autorizado a falar no time escolhido",
                )
                if voz.channel.id != canal.id:
                    await inter.user.move_to(
                        canal, reason="League of Legends: assistir time escolhido"
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Não consegui mover espectador %s: %s", inter.user.id, exc)
                try:
                    await canal.set_permissions(
                        inter.user, overwrite=overwrite_anterior,
                        reason="Reverter autorização de espectador que não conseguiu entrar",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Falha ao restaurar permissão do espectador")
                await inter.followup.send(
                    "❌ Não consegui movê-lo. Confira minhas permissões de voz e cargos."
                )
                return

            # Guarda a origem para devolver o espectador quando a partida terminar.
            if not ja_registrado:
                partida["espectadores"][str(inter.user.id)] = {
                    "time": time,
                    "origem_id": voz.channel.id,
                }
                salvar_partidas()
            emoji, nome = next((e, n) for c, e, n in EQUIPES if c == time)
            await inter.followup.send(
                f"👀 Você está assistindo ao **Time {emoji} {nome}** em {canal.mention}. "
                "Você pode ouvir e falar normalmente neste canal."
            )


class AssistirView(discord.ui.View):
    """Botão público persistente na mensagem de times sorteados."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Assistir", emoji="👀", style=discord.ButtonStyle.secondary,
        custom_id="queue:assistir",
    )
    async def assistir(self, inter: discord.Interaction, botao: discord.ui.Button) -> None:
        if inter.guild is None or not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Use este botão dentro do servidor.", ephemeral=True)
            return
        # Cada mensagem de times sorteados pertence a uma partida específica.
        chave_partida, partida = next(
            ((c, p) for c, p in partidas_do_servidor(inter.guild.id)
             if inter.message and p.get("assistir_message_id") == inter.message.id),
            (None, None),
        )
        if partida is None:
            await inter.response.send_message(
                "⚠️ Essa partida já terminou ou o painel foi substituído.", ephemeral=True
            )
            return
        if str(inter.user.id) in partida.get("participantes", {}):
            await inter.response.send_message(
                "🎮 Você já participa de um time nesta partida.", ephemeral=True
            )
            return
        canais = partida.get("canais") or {}
        if set(canais) == {"vermelho", "azul"}:
            pergunta = "👀 **Quer assistir à equipe Vermelha ou Azul?**"
        else:
            pergunta = "👀 **Qual equipe você quer assistir?**"
        await inter.response.send_message(
            pergunta + "\nVocê poderá ouvir e falar no canal escolhido. Não será possível trocar de time durante a partida.",
            view=EscolhaEquipeAssistir(inter.user.id, inter.guild.id, chave_partida),
            ephemeral=True,
        )


@bot.tree.command(
    name="queue",
    description="Abre uma fila (2v2, 3v3, 4v4 ou equipes de 5) com sorteio ao iniciar",
)
@app_commands.describe(
    equipes="Modo da fila: 2v2, 3v3, 4v4 ou de 2 a 6 equipes de 5 jogadores",
    canal="Sala de voz de espera; se vazio, usa a sala em que você está",
    imagem="Foto/GIF para salvar como imagem padrão da fila (opcional)",
    link_imagem="Link HTTPS direto para salvar como imagem padrão (opcional)",
)
@app_commands.choices(
    equipes=[
        app_commands.Choice(name=rotulo, value=valor)
        for valor, (rotulo, _n, _t) in MODOS_FILA.items()
    ]
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def queue(
    inter: discord.Interaction,
    equipes: app_commands.Choice[str] | None = None,
    canal: discord.VoiceChannel | None = None,
    imagem: discord.Attachment | None = None,
    link_imagem: str | None = None,
) -> None:
    _rotulo, num_equipes, tamanho = MODOS_FILA[equipes.value if equipes else "2x5"]
    if inter.guild is None or not autorizado(inter):
        await inter.response.send_message(
            "⛔ Apenas quem tem **Gerenciar Canais** pode abrir uma queue.", ephemeral=True
        )
        return
    guild = inter.guild
    if canal is None:
        voz = getattr(inter.user, "voice", None)
        canal = voz.channel if voz else None
    if not isinstance(canal, discord.VoiceChannel) or canal.guild.id != guild.id:
        await inter.response.send_message(
            "🎙️ Conecte-se à sala de espera ou selecione um canal de voz.", ephemeral=True
        )
        return
    if not isinstance(inter.channel, discord.TextChannel):
        await inter.response.send_message("Use /queue em um canal de texto.", ephemeral=True)
        return

    if imagem is not None and link_imagem:
        await inter.response.send_message(
            "⚠️ Escolha apenas uma imagem: arquivo OU link.", ephemeral=True
        )
        return

    await inter.response.defer(thinking=True)
    # Imagem enviada aqui também se torna a padrão das próximas filas.
    # Sem upload/link, reutiliza o arquivo salvo de forma persistente em /data.
    imagem_dados = None
    imagem_nome = None
    imagem_nova = imagem is not None or bool(link_imagem)
    if imagem_nova:
        try:
            imagem_dados = await obter_imagem(imagem, link_imagem)
        except ValueError as exc:
            await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("Falha ao buscar imagem da queue: %s", exc)
            await inter.followup.send(
                "⚠️ Não consegui receber a imagem. Confira o arquivo ou link HTTPS.",
                ephemeral=True,
            )
            return

    async with trava_servidor(guild.id):
        # Várias filas e partidas podem existir ao mesmo tempo no mesmo servidor.
        if imagem_nova:
            try:
                imagem_nome = guardar_imagem_fila(guild.id, imagem_dados)
            except (OSError, ValueError) as exc:
                log.warning("Falha ao salvar imagem da fila: %s", exc)
                await inter.followup.send("⚠️ Não consegui salvar a imagem em /data.", ephemeral=True)
                return
        else:
            try:
                padrao = imagem_fila_salva(guild.id)
            except (OSError, ValueError) as exc:
                log.error("Imagem persistente da fila indisponível: %s", exc)
                await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
                return
            if padrao is not None:
                imagem_dados, imagem_nome = padrao
                log.info("Queue usando imagem salva em /data: %s", imagem_nome)

        fila: dict[str, Any] = {
            "guild_id": guild.id,
            "equipes": num_equipes,
            "tamanho": tamanho,
            "pagina": 0,
            "origem_id": canal.id,
            "channel_id": inter.channel.id,
            "message_id": None,
            "inscritos": [],
            "status": "aberta",
            "imagem_nome": imagem_nome,
            "cor_embed": cores_fila.get(str(guild.id)),
        }
        envio = {
            "embed": montar_embed_fila(fila),
            "view": FilaView(cheia=False, pagina=0, paginas=fila_total_paginas(fila)),
            "wait": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if imagem_dados is not None:
            envio["file"] = discord.File(io.BytesIO(imagem_dados), filename=imagem_nome)
        mensagem = await inter.followup.send(**envio)
        # Em mensagens com attachment://, o Discord não lista a imagem em
        # mensagem.attachments. O envio já foi aceito sem lançar exceção;
        # nunca excluir o painel por essa lista estar vazia.
        fila["message_id"] = mensagem.id
        filas[str(mensagem.id)] = fila
        salvar_filas()


# ======================= PERSONALIZAR COR DO EMBED =======================
# Uma cor por servidor, compartilhada pelas filas futuras e atuais.
# Somente o dono da aplicação ou do servidor pode alterar.


async def aplicar_cor_queue(inter: discord.Interaction, cor: str | None) -> None:
    """Persiste a cor escolhida e atualiza o embed da fila aberta, se houver."""
    if inter.guild is None or not await dono_da_fila(inter):
        await inter.response.send_message(
            "🔒 Apenas o dono do servidor ou do bot pode mudar a cor do painel.",
            ephemeral=True,
        )
        return
    if cor is not None:
        try:
            cor = normalizar_cor_hex(cor)
        except ValueError as exc:
            await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

    await inter.response.defer(ephemeral=True, thinking=True)
    guild = inter.guild
    gid = str(guild.id)
    atualizada = False
    aviso = None
    async with trava_servidor(guild.id):
        anterior = cores_fila.get(gid)
        if cor is None:
            cores_fila.pop(gid, None)
        else:
            cores_fila[gid] = cor
        try:
            salvar_cores_fila()
        except OSError as exc:
            if anterior is None:
                cores_fila.pop(gid, None)
            else:
                cores_fila[gid] = anterior
            log.exception("Não foi possível salvar a cor permanente: %s", exc)
            await inter.followup.send(
                "⚠️ Não consegui salvar a cor em /data/queue_cores.json.", ephemeral=True
            )
            return

        for _chave, fila in filas_abertas(guild.id):
            anterior_fila = fila.get("cor_embed")
            fila["cor_embed"] = cor
            try:
                salvar_filas()
            except OSError as exc:
                log.warning("Falha ao atualizar cor em filas.json: %s", exc)
                fila["cor_embed"] = anterior_fila
                aviso = "⚠️ A fila aberta não foi atualizada no armazenamento."
            else:
                canal = guild.get_channel(fila.get("channel_id"))
                if isinstance(canal, discord.TextChannel) and fila.get("message_id"):
                    try:
                        mensagem = await canal.fetch_message(fila["message_id"])
                        await mensagem.edit(
                            embed=montar_embed_fila(fila),
                            view=FilaView(
                                cheia=fila_cheia(fila),
                                pagina=fila_pagina(fila),
                                paginas=fila_total_paginas(fila),
                            ),
                            **opcoes_imagem_edicao_fila(guild.id, fila, mensagem),
                        )
                        atualizada = True
                    except (discord.HTTPException, OSError, ValueError) as exc:
                        log.warning("Cor salva, mas painel aberto não atualizado: %s", exc)
                        aviso = "⚠️ A fila aberta não foi atualizada. A próxima fila usará a cor salva."

    texto = (
        "✅ Cor padrão restaurada!" if cor is None
        else f"✅ Cor **{cor}** salva permanentemente!"
    )
    if atualizada:
        texto += " Os painéis que já estavam abertos também foram atualizados."
    if aviso:
        texto += "\n" + aviso
    await inter.followup.send(
        texto,
        embed=discord.Embed(
            title="🎨 Prévia da cor do painel",
            description="Esta será a cor lateral do embed da fila.",
            color=int(cor[1:], 16) if cor else 0x5865F2,
        ),
        ephemeral=True,
    )


class ModalCorQueue(discord.ui.Modal):
    def __init__(self, usuario_id: int, guild_id: int):
        super().__init__(title="Cor personalizada da fila", timeout=300)
        self.usuario_id = usuario_id
        self.guild_id = guild_id
        self.entrada = discord.ui.Label(
            text="Código hexadecimal (HEX)",
            description="Exemplo: #FF1493 ou 5865F2",
            component=discord.ui.TextInput(
                placeholder="#FF1493", required=True, max_length=7,
            ),
        )
        self.add_item(self.entrada)

    async def on_submit(self, inter: discord.Interaction) -> None:
        if inter.user.id != self.usuario_id or inter.guild_id != self.guild_id:
            await inter.response.send_message("🚫 Este formulário não é seu.", ephemeral=True)
            return
        campo = self.entrada.component
        assert isinstance(campo, discord.ui.TextInput)
        await aplicar_cor_queue(inter, campo.value)


class SeletorCorQueue(discord.ui.View):
    def __init__(self, usuario_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.usuario_id = usuario_id
        self.guild_id = guild_id

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id or inter.guild_id != self.guild_id:
            await inter.response.send_message(
                "🚫 Somente quem abriu este menu pode alterar a cor.", ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        placeholder="Escolha a cor do embed",
        min_values=1,
        max_values=1,
        options=[
            *[
                discord.SelectOption(label=rotulo, value=hexadecimal)
                for rotulo, hexadecimal in CORES_PREDEFINIDAS
            ],
            discord.SelectOption(label="🎨 Digitar minha cor HEX", value="personalizada"),
            discord.SelectOption(label="🔄 Restaurar padrão", value="padrao"),
        ],
    )
    async def escolher(self, inter: discord.Interaction, menu: discord.ui.Select) -> None:
        escolha = menu.values[0]
        if escolha == "personalizada":
            await inter.response.send_modal(ModalCorQueue(self.usuario_id, self.guild_id))
            return
        await aplicar_cor_queue(inter, None if escolha == "padrao" else escolha)


@bot.tree.command(
    name="queue_cor", description="Personaliza a cor do embed da fila League of Legends"
)
@app_commands.guild_only()
async def queue_cor(inter: discord.Interaction) -> None:
    if inter.guild is None or not await dono_da_fila(inter):
        await inter.response.send_message(
            "🔒 Apenas o dono do servidor ou do bot pode alterar a cor.", ephemeral=True
        )
        return
    atual = cores_fila.get(str(inter.guild.id))
    cor = int(atual[1:], 16) if atual else 0x5865F2
    embed = discord.Embed(
        title="🎨 Personalizar cor do embed",
        description=(
            f"Cor atual: **{atual or 'Padrão (automática)'}**\n"
            "Selecione uma cor pronta ou escolha **Digitar minha cor HEX**.\n"
            "A escolha será aplicada à fila aberta e às próximas filas, mesmo após reiniciar."
        ),
        color=cor,
    )
    await inter.response.send_message(
        embed=embed, view=SeletorCorQueue(inter.user.id, inter.guild.id), ephemeral=True
    )


@bot.tree.command(
    name="queue_imagem",
    description="Salva uma foto/GIF permanente para o painel do /queue",
)
@app_commands.describe(
    imagem="Foto ou GIF do painel (arquivo)",
    link_imagem="Link HTTPS direto da foto ou GIF (alternativa ao arquivo)",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def queue_imagem(
    inter: discord.Interaction,
    imagem: discord.Attachment | None = None,
    link_imagem: str | None = None,
) -> None:
    if inter.guild is None or not await dono_da_fila(inter):
        await inter.response.send_message(
            "🔒 Somente o dono do servidor ou do bot pode alterar a imagem da fila.",
            ephemeral=True,
        )
        return
    if bool(imagem) == bool(link_imagem):
        await inter.response.send_message(
            "⚠️ Envie uma foto/GIF em `imagem` OU use `link_imagem`.", ephemeral=True
        )
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        dados = await obter_imagem(imagem, link_imagem)
    except ValueError as exc:
        await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
        return
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning("Erro de download da imagem padrão: %s", exc)
        await inter.followup.send("⚠️ Não consegui baixar a foto/GIF.", ephemeral=True)
        return

    guild = inter.guild
    async with trava_servidor(guild.id):
        try:
            nome = guardar_imagem_fila(guild.id, dados)
            leitura = imagem_fila_salva(guild.id)
            if leitura is None or leitura[0] != dados:
                raise OSError("Falha na verificação da imagem persistida.")
        except (OSError, ValueError) as exc:
            log.warning("Erro ao salvar imagem padrão: %s", exc)
            await inter.followup.send(f"⚠️ Não consegui salvar em /data: {exc}", ephemeral=True)
            return

        alterada = False
        falha_painel = None
        for _chave, fila in filas_abertas(guild.id):
            canal = guild.get_channel(fila.get("channel_id"))
            if isinstance(canal, discord.TextChannel):
                anterior = fila.get("imagem_nome")
                try:
                    mensagem = await canal.fetch_message(fila["message_id"])
                    fila["imagem_nome"] = nome
                    await mensagem.edit(
                        embed=montar_embed_fila(fila),
                        view=FilaView(
                            cheia=fila_cheia(fila), pagina=fila_pagina(fila),
                            paginas=fila_total_paginas(fila),
                        ),
                        attachments=[discord.File(io.BytesIO(dados), filename=nome)],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    salvar_filas()
                    alterada = True
                except (discord.Forbidden, discord.HTTPException, OSError) as exc:
                    fila["imagem_nome"] = anterior
                    falha_painel = str(exc)
                    log.warning("Não consegui trocar imagem do painel ativo: %s", exc)

    await inter.followup.send(
        "✅ **Imagem/GIF salva e conferida no armazenamento persistente!** "
        "As próximas filas usarão essa imagem automaticamente."
        + (" Os painéis abertos também foram atualizados." if alterada else "")
        + (" ⚠️ Algum painel aberto não foi atualizado. Faça /queue_cancelar nele e abra outra fila."
           if falha_painel else ""),
        ephemeral=True,
    )


@bot.tree.command(
    name="queue_imagem_status",
    description="Confere se a foto/GIF padrão está salva permanentemente",
)
@app_commands.guild_only()
async def queue_imagem_status(inter: discord.Interaction) -> None:
    if inter.guild is None or not await dono_da_fila(inter):
        await inter.response.send_message("🔒 Comando reservado ao dono.", ephemeral=True)
        return
    try:
        salvo = imagem_fila_salva(inter.guild.id)
    except (OSError, ValueError) as exc:
        await inter.response.send_message(f"⚠️ {exc}", ephemeral=True)
        return
    if salvo is None:
        await inter.response.send_message(
            "ℹ️ Nenhuma foto/GIF padrão configurada. Use /queue_imagem uma única vez.",
            ephemeral=True,
        )
        return
    dados, nome = salvo
    await inter.response.send_message(
        f"✅ **Imagem persistente encontrada!**\n"
        f"Arquivo: `{nome}`\n"
        f"Formato: **{tipo_imagem(dados)}**\n"
        f"Tamanho: **{len(dados) / 1024:.1f} KB**\n"
        "Armazenamento: `/data/queue_imagens/`. "
        "Se este status continuar OK depois do Redeploy, a imagem está salva.",
        ephemeral=True,
    )


@bot.event
async def on_voice_state_update(
    membro: discord.Member,
    antes: discord.VoiceState,
    depois: discord.VoiceState,
) -> None:
    """Retira automaticamente da fila quem deixa a sala de espera."""
    if membro.bot or antes.channel == depois.channel:
        return
    async with trava_servidor(membro.guild.id):
        for _chave, fila in filas_abertas(membro.guild.id):
            if membro.id not in fila.get("inscritos", []):
                continue
            if depois.channel and depois.channel.id == fila["origem_id"]:
                continue
            fila["inscritos"].remove(membro.id)
            salvar_filas()
            await atualizar_mensagem_fila(membro.guild, fila)


class SeletorCancelarFila(discord.ui.View):
    """Menu para escolher qual das filas abertas será cancelada."""

    def __init__(self, usuario_id: int, guild: discord.Guild) -> None:
        super().__init__(timeout=180)
        self.usuario_id = usuario_id
        self.guild_id = guild.id
        opcoes = []
        # O Discord aceita no máximo 25 opções em um menu.
        for numero, (chave, fila) in enumerate(filas_abertas(guild.id)[:25], start=1):
            sala = guild.get_channel(fila.get("origem_id"))
            canal_texto = guild.get_channel(fila.get("channel_id"))
            capacidade = tamanho_time(fila) * int(fila["equipes"])
            rotulo = (
                f"Fila {numero} | {nome_modo_fila(fila)} | "
                f"{len(fila.get('inscritos', []))}/{capacidade} jogadores"
            )
            descricao = (
                f"Sala: {sala.name if sala else 'indisponível'} | "
                f"Canal: #{canal_texto.name if canal_texto else 'indisponível'}"
            )
            opcoes.append(discord.SelectOption(
                label=rotulo[:100], value=chave, description=descricao[:100], emoji="🛑",
            ))
        self.menu = discord.ui.Select(
            placeholder="Escolha a fila que deseja cancelar",
            min_values=1, max_values=1, options=opcoes,
        )
        self.menu.callback = self.escolher
        self.add_item(self.menu)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id or inter.guild_id != self.guild_id:
            await inter.response.send_message(
                "🚫 Somente quem abriu este menu pode usá-lo.", ephemeral=True
            )
            return False
        return True

    async def escolher(self, inter: discord.Interaction) -> None:
        if inter.guild is None or not autorizado(inter):
            await inter.response.send_message("⛔ Sem permissão.", ephemeral=True)
            return
        chave = self.menu.values[0]
        await inter.response.defer()
        async with trava_servidor(inter.guild.id):
            cancelada = await cancelar_fila(inter.guild, chave)
        rotulo = next((o.label for o in self.menu.options if o.value == chave), "")
        texto = (
            f"✅ Fila cancelada: **{rotulo}**"
            if cancelada else "⚠️ Essa fila já foi iniciada ou cancelada."
        )
        await inter.edit_original_response(content=texto, view=None)


@bot.tree.command(name="queue_cancelar", description="Cancela uma fila antes de iniciar a partida")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def queue_cancelar(inter: discord.Interaction) -> None:
    if inter.guild is None or not autorizado(inter):
        await inter.response.send_message("⛔ Sem permissão.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    async with trava_servidor(inter.guild.id):
        abertas = filas_abertas(inter.guild.id)
        if not abertas:
            await inter.followup.send("ℹ️ Não há fila aberta.")
            return
        # Apenas uma fila aberta: cancela direto, sem perguntar.
        if len(abertas) == 1:
            await cancelar_fila(inter.guild, abertas[0][0])
            await inter.followup.send("✅ Fila cancelada.")
            return
        aviso = (
            f"\n⚠️ Mostrando as 25 filas mais antigas de {len(abertas)}."
            if len(abertas) > 25 else ""
        )
        await inter.followup.send(
            f"🛑 Há **{len(abertas)} filas abertas**. Selecione qual deseja cancelar:{aviso}",
            view=SeletorCancelarFila(inter.user.id, inter.guild),
        )


@bot.tree.error
async def ao_erro_de_comando(
    interaction: discord.Interaction, erro: app_commands.AppCommandError
) -> None:
    log.error("Erro em comando de barra: %s", erro, exc_info=erro)
    texto = "❌ Ocorreu um erro. Verifique as permissões do bot e tente novamente."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(texto, ephemeral=True)
        else:
            await interaction.response.send_message(texto, ephemeral=True)
    except discord.HTTPException:
        log.exception("Também não foi possível enviar a mensagem de erro")




# ============== PERFIL: AVATAR E BANNER ==============
LIMITE_MB = 10
LIMITE_BYTES = LIMITE_MB * 1024 * 1024
TIPOS_OK = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/octet-stream"}


def tipo_imagem(dados: bytes) -> str | None:
    """Inspeciona a assinatura do arquivo, não só a extensão/MIME informado."""
    if dados.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if dados.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if dados.startswith(b"\xff\xd8\xff"):
        return "JPG"
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "WEBP"
    return None


def validar_link(link: str) -> str:
    link = link.strip()
    if not 8 <= len(link) <= 1800 or any(c.isspace() for c in link):
        raise ValueError("Informe um link HTTPS direto e válido para a imagem/GIF.")
    try:
        alvo = urlsplit(link)
        host = (alvo.hostname or "").lower().rstrip(".")
        if (alvo.scheme != "https" or not host or alvo.username or alvo.password
                or alvo.port not in (None, 443) or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError("Use um link HTTPS público, sem usuário, senha ou porta especial.")
        try:
            endereco = ipaddress.ip_address(host)
        except ValueError:
            # Domínios públicos serão validados novamente após a resolução DNS.
            if "." not in host:
                raise ValueError("O link precisa usar um domínio público.") from None
        else:
            if not endereco.is_global:
                raise ValueError("Links para redes internas ou locais não são permitidos.")
    except ValueError as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("Use ", "Links ", "O link")):
            raise
        raise ValueError("Informe um link HTTPS direto e válido para a imagem/GIF.") from exc
    return link


class ResolverPublico(AbstractResolver):
    """Impede que links remotos sejam resolvidos para IPs locais/privados."""

    def __init__(self) -> None:
        self._resolver = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = 0):
        enderecos = await self._resolver.resolve(host, port, family)
        if not enderecos or any(
            not ipaddress.ip_address(item["host"]).is_global for item in enderecos
        ):
            raise ValueError("O endereço do link não é público.")
        return enderecos

    async def close(self) -> None:
        await self._resolver.close()


async def eh_dono(bot: discord.Client, usuario: discord.abc.User) -> bool:
    """Só o proprietário da aplicação (ou da equipe proprietária) altera o perfil."""
    info = await bot.application_info()
    dono_id = info.team.owner_id if info.team else info.owner.id
    return usuario.id == dono_id


async def obter_imagem(arquivo: Optional[discord.Attachment], link: Optional[str]) -> bytes:
    if bool(arquivo) == bool(link):
        raise ValueError("Escolha apenas uma opção: envie um **arquivo** OU informe um **link**.")

    if arquivo is not None:
        if arquivo.size > LIMITE_BYTES:
            raise ValueError(f"Arquivo grande demais. Limite: {LIMITE_MB} MB.")
        dados = await arquivo.read()
    else:
        endereco = validar_link(link or "")
        timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=12)
        connector = aiohttp.TCPConnector(resolver=ResolverPublico(), use_dns_cache=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False) as sessao:
            # Links diretos sem redirecionamentos são exigidos para evitar acessar endpoints inesperados.
            async with sessao.get(endereco, allow_redirects=False) as resposta:
                if 300 <= resposta.status < 400:
                    raise ValueError("O link redireciona. Envie o endereço HTTPS direto da imagem/GIF.")
                if resposta.status != 200:
                    raise ValueError(f"O link retornou HTTP {resposta.status}.")
                if resposta.content_type.lower() not in TIPOS_OK:
                    raise ValueError("O link deve apontar para uma imagem PNG, JPG, GIF ou WEBP.")
                if resposta.content_length is not None and resposta.content_length > LIMITE_BYTES:
                    raise ValueError(f"Imagem muito grande. Limite: {LIMITE_MB} MB.")
                partes = bytearray()
                async for pedaco in resposta.content.iter_chunked(65536):
                    partes.extend(pedaco)
                    if len(partes) > LIMITE_BYTES:
                        raise ValueError(f"Imagem muito grande. Limite: {LIMITE_MB} MB.")
                dados = bytes(partes)

    if not dados or len(dados) > LIMITE_BYTES:
        raise ValueError(f"Imagem vazia ou maior que {LIMITE_MB} MB.")
    if tipo_imagem(dados) is None:
        raise ValueError("Formato de arquivo inválido. Use uma imagem PNG, JPG, GIF ou WEBP.")
    return dados


async def atualizar_perfil(
    bot: discord.Client,
    inter: discord.Interaction,
    campo: str,
    rotulo: str,
    arquivo: Optional[discord.Attachment],
    link: Optional[str],
) -> None:
    try:
        permitido = await eh_dono(bot, inter.user)
    except discord.HTTPException:
        await inter.response.send_message(
            "⚠️ Não consegui confirmar o proprietário do bot. Tente novamente.", ephemeral=True
        )
        return

    if not permitido:
        await inter.response.send_message(
            "🚫 Apenas o proprietário do bot pode alterar o avatar ou o banner.", ephemeral=True
        )
        return

    if bool(arquivo) == bool(link):
        await inter.response.send_message(
            "⚠️ Selecione apenas uma origem: anexe uma foto/GIF em `arquivo` OU cole a URL em `link`.",
            ephemeral=True,
        )
        return

    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        dados = await obter_imagem(arquivo, link)
        if bot.user is None:
            raise RuntimeError("O perfil do bot ainda não está disponível.")
        await bot.user.edit(**{campo: dados})
    except ValueError as exc:
        await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
        return
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning("Falha ao obter imagem de %s: %s", campo, exc)
        await inter.followup.send(
            "⚠️ Não consegui baixar a imagem. Confirme se o link HTTPS é direto e público.", ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        log.warning("Discord recusou atualização de %s: %s", campo, exc)
        await inter.followup.send(
            f"⚠️ O Discord recusou a atualização do {rotulo.lower()}. "
            "Verifique formato, tamanho e limite de alterações do perfil.", ephemeral=True
        )
        return
    except (TypeError, RuntimeError) as exc:
        log.warning("Não foi possível atualizar %s: %s", campo, exc)
        await inter.followup.send(
            "⚠️ Não consegui atualizar o perfil. Confira a versão do discord.py e reinicie o bot.",
            ephemeral=True,
        )
        return

    await inter.followup.send(
        f"✅ **{rotulo} atualizado com sucesso!** A imagem foi aplicada ao perfil do bot no Discord.",
        ephemeral=True,
    )


class ModalArquivo(discord.ui.Modal):
    """Solicita um arquivo usando o seletor nativo do Discord (discord.py 2.7+)."""

    def __init__(self, bot: discord.Client, usuario_id: int, campo: str, rotulo: str) -> None:
        super().__init__(title=f"Enviar {rotulo.lower()} por arquivo", timeout=300)
        self.bot, self.usuario_id = bot, usuario_id
        self.campo, self.rotulo = campo, rotulo
        self.entrada = discord.ui.Label(
            text="Selecionar foto ou GIF",
            description=f"PNG, JPG, GIF ou WEBP (até {LIMITE_MB} MB)",
            component=discord.ui.FileUpload(required=True, min_values=1, max_values=1),
        )
        self.add_item(self.entrada)

    async def on_submit(self, inter: discord.Interaction) -> None:
        if inter.user.id != self.usuario_id:
            await inter.response.send_message("🚫 Este formulário pertence a outro usuário.", ephemeral=True)
            return
        upload = self.entrada.component
        assert isinstance(upload, discord.ui.FileUpload)
        arquivos = upload.values
        if not arquivos:
            await inter.response.send_message("⚠️ Selecione uma foto ou GIF.", ephemeral=True)
            return
        await atualizar_perfil(self.bot, inter, self.campo, self.rotulo, arquivos[0], None)


class ModalLink(discord.ui.Modal):
    """Recebe uma URL direta de imagem/GIF."""

    def __init__(self, bot: discord.Client, usuario_id: int, campo: str, rotulo: str) -> None:
        super().__init__(title=f"Definir {rotulo.lower()} por link", timeout=300)
        self.bot, self.usuario_id = bot, usuario_id
        self.campo, self.rotulo = campo, rotulo
        self.entrada = discord.ui.Label(
            text="Link direto da foto ou GIF",
            description="URL HTTPS pública de PNG, JPG, GIF ou WEBP",
            component=discord.ui.TextInput(
                placeholder="https://exemplo.com/imagem.gif",
                style=discord.TextStyle.short,
                required=True,
                max_length=1800,
            ),
        )
        self.add_item(self.entrada)

    async def on_submit(self, inter: discord.Interaction) -> None:
        if inter.user.id != self.usuario_id:
            await inter.response.send_message("🚫 Este formulário pertence a outro usuário.", ephemeral=True)
            return
        campo_texto = self.entrada.component
        assert isinstance(campo_texto, discord.ui.TextInput)
        await atualizar_perfil(self.bot, inter, self.campo, self.rotulo, None, campo_texto.value)


class OpcoesPerfil(discord.ui.View):
    """Dois botões de entrada, sem parâmetros extras no slash command."""

    def __init__(self, bot: discord.Client, usuario_id: int, campo: str, rotulo: str) -> None:
        super().__init__(timeout=300)
        self.bot, self.usuario_id = bot, usuario_id
        self.campo, self.rotulo = campo, rotulo

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.usuario_id:
            await inter.response.send_message(
                "🚫 Apenas quem abriu este menu pode usá-lo.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Enviar foto/GIF por arquivo", emoji="📁", style=discord.ButtonStyle.primary)
    async def por_arquivo(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        await inter.response.send_modal(
            ModalArquivo(self.bot, self.usuario_id, self.campo, self.rotulo)
        )

    @discord.ui.button(label="Adicionar por link", emoji="🔗", style=discord.ButtonStyle.secondary)
    async def por_link(self, inter: discord.Interaction, button: discord.ui.Button) -> None:
        await inter.response.send_modal(
            ModalLink(self.bot, self.usuario_id, self.campo, self.rotulo)
        )


async def abrir_menu_perfil(
    bot: discord.Client, inter: discord.Interaction, campo: str, rotulo: str
) -> None:
    try:
        permitido = await eh_dono(bot, inter.user)
    except discord.HTTPException:
        await inter.response.send_message(
            "⚠️ Não consegui confirmar o proprietário do bot. Tente novamente.", ephemeral=True
        )
        return
    if not permitido:
        await inter.response.send_message(
            "🚫 Apenas o proprietário do bot pode alterar o avatar ou banner.", ephemeral=True
        )
        return

    await inter.response.send_message(
        f"🎨 **Personalizar {rotulo.lower()}**\n"
        "Escolha como deseja enviar sua foto ou GIF:",
        view=OpcoesPerfil(bot, inter.user.id, campo, rotulo),
        ephemeral=True,
    )


def registrar_comandos_perfil(bot: discord.Client) -> None:
    """Registra os dois comandos de personalização."""

    @bot.tree.command(name="avatar", description="Personalizar o avatar do bot com foto ou GIF")
    async def avatar(inter: discord.Interaction) -> None:
        await abrir_menu_perfil(bot, inter, "avatar", "Avatar")

    @bot.tree.command(name="banner", description="Personalizar o banner do bot com foto ou GIF")
    async def banner(inter: discord.Interaction) -> None:
        await abrir_menu_perfil(bot, inter, "banner", "Banner")


registrar_comandos_perfil(bot)


# Comando único para mudar presença e texto do bot.
# Apenas o proprietário da aplicação pode mudar um status global.
@bot.tree.command(
    name="bot_status",
    description="Personaliza o texto e a presença do bot (Disponível, Ausente, Ocupado ou Invisível)",
)
@app_commands.describe(
    estado="Como o bot deve aparecer para os membros",
    texto="Texto personalizado do perfil (opcional; 'limpar' remove o texto)",
)
@app_commands.choices(
    estado=[
        app_commands.Choice(name="🟢 Disponível", value="disponivel"),
        app_commands.Choice(name="🌙 Ausente", value="ausente"),
        app_commands.Choice(name="🔴 Ocupado", value="ocupado"),
        app_commands.Choice(name="⚫ Invisível", value="invisivel"),
    ],
)
async def bot_status(
    inter: discord.Interaction,
    estado: app_commands.Choice[str],
    texto: str | None = None,
) -> None:
    try:
        permitido = await eh_dono(bot, inter.user)
    except discord.HTTPException:
        await inter.response.send_message(
            "⚠️ Não consegui verificar o proprietário do bot. Tente novamente.",
            ephemeral=True,
        )
        return
    if not permitido:
        await inter.response.send_message(
            "🔒 Apenas o proprietário do bot pode alterar sua presença.",
            ephemeral=True,
        )
        return

    if texto is not None and len(texto) > 128:
        await inter.response.send_message(
            "⚠️ O texto deve ter no máximo 128 caracteres.", ephemeral=True
        )
        return

    await inter.response.defer(ephemeral=True, thinking=True)
    async with trava_presenca_bot:
        anterior = dict(presenca_bot)
        novo = {
            "estado": estado.value,
            "texto": (
                anterior["texto"] if texto is None else
                "" if texto.strip().casefold() == "limpar" else texto.strip()
            ),
        }
        try:
            # Altera já, sem reiniciar o bot.
            await bot.change_presence(
                status=ESTADOS_BOT[novo["estado"]],
                activity=atividade_presenca(novo["texto"]),
            )
        except (discord.DiscordException, TypeError, OSError) as exc:
            log.warning("Falha ao mudar presença do bot: %s", exc)
            await inter.followup.send(
                "⚠️ Não consegui alterar a presença agora. Confira o console do FadeHost.",
                ephemeral=True,
            )
            return

        try:
            salvar_presenca(novo)
        except OSError as exc:
            log.exception("Não foi possível persistir a presença do bot: %s", exc)
            try:
                await bot.change_presence(
                    status=ESTADOS_BOT[anterior["estado"]],
                    activity=atividade_presenca(anterior["texto"]),
                )
            except discord.DiscordException:
                log.exception("Não foi possível restaurar a presença anterior")
            await inter.followup.send(
                "⚠️ A presença não foi salva em /data. Veja as permissões da hospedagem.",
                ephemeral=True,
            )
            return

        presenca_bot.update(novo)

    resposta = (
        f"✅ **Presença atualizada!**\n"
        f"**Estado:** {ROTULOS_ESTADOS_BOT[novo['estado']]}\n"
        f"**Texto:** {discord.utils.escape_markdown(novo['texto']) if novo['texto'] else 'Sem texto'}\n"
        "💾 Configuração salva e restaurada automaticamente após reiniciar o bot."
    )
    if novo["estado"] == "invisivel":
        resposta += (
            "\n👁️ O modo Invisível faz o bot parecer offline, "
            "mas seus comandos continuam funcionando."
        )
    await inter.followup.send(
        resposta,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )



# ======================= PARTIDA AO VIVO (SPECTATOR-V5) =======================
# Usa as contas já vinculadas com /link. Nada novo para o membro configurar.
# /aovivo mostra (só para quem pediu) a partida atual de um membro vinculado.
# Com /aovivo_canal configurado, o bot avisa sozinho no canal quando um membro
# vinculado entra em partida (a Riot libera a partida a partir da tela de carregamento).
import math
from discord.ext import tasks

ARQUIVO_AOVIVO = Path("/data/aovivo.json")
AOVIVO_CONSULTAS_POR_MIN = 20   # Orçamento da vigilância; sobra limite para /estatisticas.
AOVIVO_INTERVALO_MIN = 60       # Segundos entre as checagens (sobe com muitas contas).
AOVIVO_PARTIDA_NOVA = 5 * 60    # Só anuncia partidas com menos de 5 min (evita repetir após reinício).
NOMES_TIME_LOL = {100: ("🔵", "Time Azul", 0x3B82F6), 200: ("🔴", "Time Vermelho", 0xEF4444)}


def carregar_aovivo() -> dict[str, Any]:
    padrao: dict[str, Any] = {"canais": {}, "sem_aviso": []}
    if not ARQUIVO_AOVIVO.exists():
        return padrao
    try:
        dados = json.loads(ARQUIVO_AOVIVO.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("A raiz deve ser um objeto JSON.")
        padrao.update(dados)
        return padrao
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        raise SystemExit(f"Erro ao ler aovivo.json: {exc}") from exc


aovivo_config = carregar_aovivo()


def salvar_aovivo() -> None:
    ARQUIVO_AOVIVO.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_AOVIVO.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(aovivo_config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(ARQUIVO_AOVIVO)


_aovivo_ultimo_jogo: dict[str, int | None] = {}
_aovivo_anunciados: dict[tuple[int, int], float] = {}
_aovivo_ranques: dict[str, tuple[float, list[Any]]] = {}
_aovivo_proxima = 0.0
_nomes_feiticos: dict[int, str] = {}


async def nomes_feiticos() -> dict[int, str]:
    """Nomes dos feitiços de invocador (Flash, Incendiar...) pelo Data Dragon."""
    if _nomes_feiticos:
        return _nomes_feiticos
    await nomes_campeoes()  # Garante a versão do Data Dragon.
    if not _versao_ddragon:
        return _nomes_feiticos
    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sessao:
            async with sessao.get(
                f"https://ddragon.leagueoflegends.com/cdn/{_versao_ddragon}/data/pt_BR/summoner.json"
            ) as r:
                dados = await r.json()
        for feitico in dados["data"].values():
            _nomes_feiticos[int(feitico["key"])] = feitico["name"]
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
        log.warning("Não consegui carregar os feitiços: %s", exc)
    return _nomes_feiticos


async def partida_ativa(puuid: str) -> dict[str, Any] | None:
    """Partida em andamento da conta, ou None se não estiver jogando."""
    try:
        dados = await chamar_riot(
            f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/spectator/v5/active-games/"
            f"by-summoner/{quote(puuid, safe='')}"
        )
    except ErroRiot as exc:
        if exc.status == 404:
            return None
        raise
    return dados if isinstance(dados, dict) and dados.get("participants") else None


async def ranques_conta(puuid: str) -> list[Any]:
    guardado = _aovivo_ranques.get(puuid)
    if guardado and time.time() - guardado[0] < ELO_VALIDADE_SEGUNDOS:
        return guardado[1]
    try:
        dados = await chamar_riot(
            f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/league/v4/entries/by-puuid/"
            f"{quote(puuid, safe='')}"
        )
    except ErroRiot as exc:
        if exc.status != 404:
            raise
        dados = []
    dados = dados if isinstance(dados, list) else []
    _aovivo_ranques[puuid] = (time.time(), dados)
    while len(_aovivo_ranques) > 2000:
        _aovivo_ranques.pop(next(iter(_aovivo_ranques)))
    return dados


_aovivo_maestrias: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def maestrias_conta(puuid: str) -> list[dict[str, Any]]:
    """Todas as maestrias da conta (1 consulta), da maior para a menor."""
    guardado = _aovivo_maestrias.get(puuid)
    if guardado and time.time() - guardado[0] < ELO_VALIDADE_SEGUNDOS:
        return guardado[1]
    try:
        dados = await chamar_riot(
            f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/champion-mastery/v4/"
            f"champion-masteries/by-puuid/{quote(puuid, safe='')}"
        )
    except ErroRiot as exc:
        if exc.status != 404:
            raise
        dados = []
    lista = [m for m in dados if isinstance(m, dict)] if isinstance(dados, list) else []
    lista.sort(key=lambda m: -int(m.get("championPoints", 0) or 0))
    _aovivo_maestrias[puuid] = (time.time(), lista)
    while len(_aovivo_maestrias) > 2000:
        _aovivo_maestrias.pop(next(iter(_aovivo_maestrias)))
    return lista


def _pontos(valor: Any) -> str:
    return f"{int(valor or 0):,}".replace(",", ".")


_aovivo_historico: dict[str, tuple[float, list[str]]] = {}
AOVIVO_HISTORICO_PARTIDAS = 20   # Partidas recentes comparadas para achar quem joga junto.
AOVIVO_MINIMO_JUNTOS = 2         # Partidas em comum para considerar que estão em grupo.
EMOJIS_GRUPO = ("🟦", "🟩", "🟨", "🟪", "🟧")


async def partidas_recentes_ids(puuid: str) -> set[str]:
    """IDs das últimas partidas (só os IDs: 1 consulta por jogador)."""
    return set(await partidas_recentes_lista(puuid))


_aovivo_em_andamento: dict[str, asyncio.Task] = {}


async def em_andamento(chave: str, criar) -> Any:
    """Se a mesma consulta já está sendo feita, espera por ela em vez de repetir."""
    tarefa = _aovivo_em_andamento.get(chave)
    if tarefa is None:
        tarefa = asyncio.ensure_future(criar())
        _aovivo_em_andamento[chave] = tarefa
        tarefa.add_done_callback(lambda _t: _aovivo_em_andamento.pop(chave, None))
    return await asyncio.shield(tarefa)


async def partidas_recentes_lista(puuid: str) -> list[str]:
    """Mesmo que acima, da partida mais nova para a mais antiga."""
    guardado = _aovivo_historico.get(puuid)
    if guardado and time.time() - guardado[0] < ESTATISTICAS_VALIDADE_SEGUNDOS:
        return guardado[1]
    return await em_andamento(f"ids:{puuid}", lambda: _buscar_partidas_recentes(puuid))


async def _buscar_partidas_recentes(puuid: str) -> list[str]:
    dados = await chamar_riot(
        f"https://{RIOT_REGIAO}.api.riotgames.com/lol/match/v5/matches/by-puuid/"
        f"{quote(puuid, safe='')}/ids?start=0&count={AOVIVO_HISTORICO_PARTIDAS}"
    )
    ids = [str(i) for i in dados] if isinstance(dados, list) else []
    _aovivo_historico[puuid] = (time.time(), ids)
    while len(_aovivo_historico) > 2000:
        _aovivo_historico.pop(next(iter(_aovivo_historico)))
    return ids


def grupos_provaveis(
    participantes: list[dict[str, Any]], historicos: list[set[str] | None]
) -> dict[int, tuple[int, int]]:
    """Índice do jogador -> (número do grupo, partidas juntos), por time.

    Dois jogadores do mesmo time com várias partidas recentes em comum
    provavelmente entraram juntos (duo/premade). É uma estimativa.
    """
    pai = list(range(len(participantes)))
    juntos_max = [0] * len(participantes)

    def raiz(i: int) -> int:
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    for i in range(len(participantes)):
        for j in range(i + 1, len(participantes)):
            if participantes[i].get("teamId") != participantes[j].get("teamId"):
                continue
            a, b = historicos[i], historicos[j]
            comuns = len(a & b) if a and b else 0
            # Dois membros vinculados do servidor no mesmo time contam como grupo.
            ambos_do_servidor = all(
                participantes[k].get("puuid") and dono_da_conta_riot(participantes[k]["puuid"])
                for k in (i, j)
            )
            if comuns >= AOVIVO_MINIMO_JUNTOS or ambos_do_servidor:
                pai[raiz(i)] = raiz(j)
                juntos_max[i] = max(juntos_max[i], comuns)
                juntos_max[j] = max(juntos_max[j], comuns)
    membros: dict[int, list[int]] = {}
    for i in range(len(participantes)):
        membros.setdefault(raiz(i), []).append(i)
    resultado: dict[int, tuple[int, int]] = {}
    numero = 0
    for grupo in membros.values():
        if len(grupo) < 2:
            continue
        for i in grupo:
            resultado[i] = (numero, juntos_max[i])
        numero += 1
    return resultado


_filas_riot: dict[int, str] = {}
# Nomes em português para filas que não estão em NOMES_FILAS_LOL.
TRADUCOES_FILA = (
    ("mayhem", "ARAM: Desordem"),
    ("aram", "ARAM"),
    ("arena", "Arena"),
    ("swiftplay", "Escolha Rápida"),
    ("urf", "URF"),
    ("one for all", "Um Por Todos"),
    ("nexus blitz", "Nexus Blitz"),
    ("ultimate spellbook", "Grimório Supremo"),
    ("brawl", "Confronto"),
    ("tutorial", "Tutorial"),
    ("co-op", "Cooperativo vs. IA"),
    ("bot", "Cooperativo vs. IA"),
)


def eh_personalizada(jogo: dict[str, Any]) -> bool:
    """Personalizadas vêm sem fila (0 ou negativa) ou com gameType de custom."""
    try:
        fila = int(jogo.get("gameQueueConfigId") or 0)
    except (TypeError, ValueError):
        fila = 0
    return fila <= 0 or "CUSTOM" in str(jogo.get("gameType") or "").upper()


async def nome_fila_lol(jogo: dict[str, Any]) -> str:
    """Nome do modo: lista do bot, depois a lista oficial de filas da Riot."""
    fila_id = int(jogo.get("gameQueueConfigId") or 0)
    if eh_personalizada(jogo):
        modo = str(jogo.get("gameMode") or "").upper()
        return "Personalizada (ARAM)" if modo == "ARAM" else "Personalizada"
    if fila_id in NOMES_FILAS_LOL:
        return NOMES_FILAS_LOL[fila_id]
    if not _filas_riot:
        timeout = aiohttp.ClientTimeout(total=8, connect=4)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sessao:
                async with sessao.get(
                    "https://static.developer.riotgames.com/docs/lol/queues.json"
                ) as r:
                    for fila in await r.json(content_type=None):
                        _filas_riot[int(fila["queueId"])] = str(fila.get("description") or "")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
            log.warning("Não consegui carregar a lista de filas da Riot: %s", exc)
    descricao = (_filas_riot.get(fila_id) or "").strip()
    texto = f"{descricao} {jogo.get('gameMode') or ''}".casefold()
    for chave, nome in TRADUCOES_FILA:
        if chave in texto:
            return nome
    if descricao:
        return descricao.removesuffix(" games").strip()
    log.info("Fila desconhecida: id=%s modo=%s", fila_id, jogo.get("gameMode"))
    return f"Outro modo ({fila_id})"


def linhas_ranque(entradas: list[Any]) -> list[str]:
    linhas = []
    for fila, rotulo in (("RANKED_SOLO_5x5", "Solo/Duo"), ("RANKED_FLEX_SR", "Flex")):
        e = next((x for x in entradas if isinstance(x, dict) and x.get("queueType") == fila), None)
        if not e:
            continue
        tier = str(e.get("tier", ""))
        nome = NOMES_TIER.get(tier, tier.title())
        divisao = "" if tier in ("MASTER", "GRANDMASTER", "CHALLENGER") else f" {e.get('rank', '')}"
        v, d = int(e.get("wins", 0) or 0), int(e.get("losses", 0) or 0)
        fogo = " 🔥" if e.get("hotStreak") else ""
        linhas.append(
            f"🏅 {rotulo}: **{nome}{divisao}** {int(e.get('leaguePoints', 0) or 0)} PDL • "
            f"{_porcentagem(v, v + d)}% ({v}V {d}D){fogo}"
        )
    return linhas or ["🏅 Sem ranque"]


def dono_da_conta_riot(puuid: str) -> int | None:
    for uid, registro in contas_riot.items():
        if any(c.get("puuid") == puuid for c in (registro or {}).get("contas", [])):
            return int(uid)
    return None


def caber_no_discord(embeds: list[discord.Embed], limite: int = 5900) -> None:
    """O Discord recusa mensagens com mais de 6000 caracteres em embeds.
    Se passar, remove primeiro as linhas menos importantes."""
    for prefixo in ("✨", "🔁", "👑", "👤"):
        if sum(len(e) for e in embeds) <= limite:
            return
        for e in embeds:
            for i, campo in enumerate(e.fields):
                nova = "\n".join(
                    linha for linha in campo.value.split("\n") if not linha.startswith(prefixo)
                )
                if nova != campo.value:
                    e.set_field_at(i, name=campo.name, value=nova or "-", inline=campo.inline)
    for e in embeds:
        for i, campo in enumerate(e.fields):
            if sum(len(x) for x in embeds) <= limite:
                return
            e.set_field_at(i, name=campo.name[:60], value=campo.value[:400], inline=campo.inline)


async def montar_aovivo(
    jogo: dict[str, Any], puuid_ref: str, com_desempenho: bool = True
) -> list[discord.Embed]:
    """Um embed por time. Sem desempenho fica pronto em poucos segundos."""
    participantes = list(jogo.get("participants", []))[:10]
    nomes = await nomes_campeoes()
    feiticos = await nomes_feiticos()
    icones = await icones_campeoes({int(p.get("championId", 0)) for p in participantes})
    nomes = dict(nomes)

    async def dados_jogador(p: dict[str, Any]) -> tuple[list[Any] | None, list[dict[str, Any]] | None]:
        puuid = p.get("puuid")
        if not puuid or p.get("bot"):
            return None, None
        try:
            return await asyncio.gather(
                ranques_conta(puuid), maestrias_conta(puuid)
            )
        except (ErroRiot, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("Ao vivo: falha ao consultar jogador: %s", exc)
            return None, None

    async def historico(p: dict[str, Any]) -> set[str] | None:
        if not p.get("puuid") or p.get("bot"):
            return None
        try:
            return await partidas_recentes_ids(p["puuid"])
        except (ErroRiot, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("Ao vivo: falha no histórico: %s", exc)
            return None

    async def desempenho(p: dict[str, Any]) -> str | None:
        if not p.get("puuid") or p.get("bot"):
            return None
        try:
            return await texto_desempenho(p["puuid"], int(p.get("championId", 0)), nomes)
        except (ErroRiot, aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            log.warning("Ao vivo: falha no desempenho: %s", exc)
            return "📈 Desempenho recente indisponível"

    async def nada() -> None:
        return None

    infos, historicos, desempenhos = await asyncio.gather(
        asyncio.gather(*(dados_jogador(p) for p in participantes)),
        asyncio.gather(*(historico(p) for p in participantes)),
        asyncio.gather(*(desempenho(p) if com_desempenho else nada() for p in participantes)),
    )
    grupos = grupos_provaveis(participantes, list(historicos))
    time_ref = next(
        (p.get("teamId") for p in participantes if p.get("puuid") == puuid_ref), 100
    )
    times = sorted({p.get("teamId") for p in participantes}, key=lambda t: t != time_ref)

    embeds = []
    for time_id in times:
        emoji, titulo, cor = NOMES_TIME_LOL.get(time_id, ("⚪", f"Time {time_id}", 0x99AAB5))
        embed = discord.Embed(title=f"{emoji} {titulo}", color=cor)
        for indice, (p, (ranques, maestrias)) in enumerate(zip(participantes, infos)):
            if p.get("teamId") != time_id:
                continue
            cid = int(p.get("championId", 0))
            nome_campeao = nomes.get(cid, f"Campeão {cid}")
            riot_id = p.get("riotId") or "Jogador"
            linhas = []
            primeira = f"{icones.get(cid, '')} **{nome_campeao}**".strip()
            dono = dono_da_conta_riot(p.get("puuid") or "")
            if dono:
                primeira += f" • <@{dono}>"
            linhas.append(primeira)
            if indice in grupos:
                numero_grupo, vezes = grupos[indice]
                companheiros = [
                    participantes[k].get("riotId", "?").split("#")[0]
                    for k, (g, _v) in grupos.items() if g == numero_grupo and k != indice
                ]
                linhas.append(
                    f"{EMOJIS_GRUPO[numero_grupo % len(EMOJIS_GRUPO)]} Jogando junto com "
                    f"{discord.utils.escape_markdown(', '.join(companheiros))[:200]} "
                    + (f"({vezes} das últimas {AOVIVO_HISTORICO_PARTIDAS})"
                       if vezes >= AOVIVO_MINIMO_JUNTOS else "(membros do servidor)")
                )
            elif not p.get("bot"):
                linhas.append("👤 Sem grupo identificado (provavelmente sozinho)")
            if p.get("bot"):
                linhas.append("🤖 Bot")
            else:
                linhas.extend(linhas_ranque(ranques) if ranques is not None else ["🏅 Elo indisponível"])
                if maestrias is not None:
                    atual = next(
                        (m for m in maestrias if int(m.get("championId", 0)) == cid), None
                    )
                    if atual:
                        ultima = int(atual.get("lastPlayTime") or 0) // 1000
                        linhas.append(
                            f"⭐ Maestria {atual.get('championLevel', 0)} • "
                            f"{_pontos(atual.get('championPoints'))} pts"
                            + (f" • jogou <t:{ultima}:R>" if ultima else "")
                        )
                    else:
                        linhas.append("⭐ Primeira vez (sem maestria nesse campeão)")
                    if maestrias:
                        principal = maestrias[0]
                        pid = int(principal.get("championId", 0))
                        if pid == cid:
                            linhas.append(
                                f"👑 Principal: {nomes.get(pid, f'Campeão {pid}')} (é o campeão "
                                f"atual • {_pontos(principal.get('championPoints'))} pts)"
                            )
                        else:
                            linhas.append(
                                f"👑 Principal: {nomes.get(pid, f'Campeão {pid}')} "
                                f"({_pontos(principal.get('championPoints'))} pts)"
                            )
                    else:
                        linhas.append("👑 Principal: sem maestria em nenhum campeão")
            if desempenhos[indice]:
                linhas.append(desempenhos[indice])
            nomes_f = [feiticos.get(int(p.get(k) or 0)) for k in ("spell1Id", "spell2Id")]
            if any(nomes_f):
                linhas.append("✨ " + " / ".join(n for n in nomes_f if n))
            embed.add_field(
                name=discord.utils.escape_markdown(riot_id)[:256],
                value="\n".join(linhas)[:1024], inline=False,
            )
        banidos = [
            nomes.get(int(b.get("championId", 0)), "?") for b in jogo.get("bannedChampions", [])
            if b.get("teamId") == time_id and int(b.get("championId", 0)) > 0
        ]
        if banidos:
            embed.add_field(name="🚫 Banimentos", value=", ".join(banidos)[:1024], inline=False)
        embeds.append(embed)

    fila = await nome_fila_lol(jogo)
    minutos = max(0, int(jogo.get("gameLength") or 0)) // 60
    if embeds:
        embeds[0].description = f"🎮 **{fila}** • {minutos} min de jogo"
        if not com_desempenho:
            embeds[0].description += "\n⏳ Carregando vitórias e derrotas recentes..."
        if grupos:
            embeds[0].description += (
                "\n-# Grupos são estimados pelas partidas recentes em comum; a Riot não informa isso."
            )
    caber_no_discord(embeds)
    return embeds


AOVIVO_PARTIDAS_DESEMPENHO = 5  # Partidas recentes abertas por jogador.


async def jogos_recentes(puuid: str) -> list[dict[str, Any]]:
    """Últimas partidas do jogador (campeão, vitória, rota, modo e horário)."""
    jogos = []
    ids = (await partidas_recentes_lista(puuid))[:AOVIVO_PARTIDAS_DESEMPENHO]
    resumos = await asyncio.gather(
        *(em_andamento(f"partida:{m}", lambda m=m: resumo_partida_lol(m)) for m in ids),
        return_exceptions=True,
    )
    for resumo in resumos:
        if isinstance(resumo, ErroRiot):
            if resumo.status in (0, 401, 403, 429):
                raise resumo
            continue
        if isinstance(resumo, BaseException) or not resumo:
            continue
        if int(resumo.get("fila") or 0) == 0:
            # Regra da Riot: histórico de personalizadas não é exibido sem consentimento.
            continue
        eu = next((x for x in resumo["participantes"] if x["puuid"] == puuid), None)
        if eu:
            jogos.append({**eu, "fila": resumo["fila"], "fim": resumo["fim"]})
    return jogos


async def texto_desempenho(puuid: str, cid: int, nomes: dict[int, str]) -> str:
    """Vitórias/derrotas recentes, desempenho com o campeão atual e o mais jogado."""
    jogos = await jogos_recentes(puuid)
    if not jogos:
        return "📈 Sem partidas recentes"
    v = sum(1 for j in jogos if j["vitoria"])
    d = len(jogos) - v
    linhas = [f"📈 Últimas {len(jogos)}: **{v}V {d}D** ({_porcentagem(v, len(jogos))}%)"]
    com = [j for j in jogos if j["campeao_id"] == cid]
    if com:
        vc = sum(1 for j in com if j["vitoria"])
        frequente = " • 🔥 jogando muito com ele" if len(com) >= 3 else ""
        linhas.append(
            f"🎯 Com ele: {len(com)} de {len(jogos)} • {vc}V {len(com) - vc}D "
            f"({_porcentagem(vc, len(com))}%){frequente}"
        )
    else:
        linhas.append(f"🎯 Não jogou com ele nas últimas {len(jogos)}")
    contagem: dict[int, list[int]] = {}
    for j in jogos:
        contagem.setdefault(j["campeao_id"], [0, 0])
        contagem[j["campeao_id"]][0] += 1
        contagem[j["campeao_id"]][1] += int(j["vitoria"])
    mais_id, (qtd, vit) = max(contagem.items(), key=lambda item: (item[1][0], item[1][1]))
    if qtd >= 2:
        atual_txt = " (é o atual)" if mais_id == cid else ""
        linhas.append(
            f"🔁 Mais jogado: {nomes.get(mais_id, f'Campeão {mais_id}')}{atual_txt} • {qtd} jogos • "
            f"{vit}V {qtd - vit}D"
        )
    else:
        linhas.append(f"🔁 Mais jogado: nenhum repetido nas últimas {len(jogos)}")
    return "\n".join(linhas)


async def detalhes_jogador(p: dict[str, Any], nomes: dict[int, str], icones: dict[int, str]) -> str:
    """Bloco completo de um jogador. Usa os dados já guardados pelo /aovivo."""
    puuid = p["puuid"]
    cid = int(p.get("championId", 0))
    nome_atual = nomes.get(cid, f"Campeão {cid}")
    ranques, maestrias, jogos = await asyncio.gather(
        ranques_conta(puuid), maestrias_conta(puuid), jogos_recentes(puuid)
    )
    linhas = [f"{icones.get(cid, '')} Jogando de **{nome_atual}**".strip()]
    linhas.extend(linhas_ranque(ranques))

    atual = next((m for m in maestrias if int(m.get("championId", 0)) == cid), None)
    if atual:
        ultima = int(atual.get("lastPlayTime") or 0) // 1000
        posicao = next(
            (i for i, m in enumerate(maestrias, 1) if int(m.get("championId", 0)) == cid), 0
        )
        linhas.append(
            f"⭐ {nome_atual}: maestria {atual.get('championLevel', 0)} • "
            f"{_pontos(atual.get('championPoints'))} pts • {posicao}º da conta"
            + (f" • jogou <t:{ultima}:R>" if ultima else "")
        )
    else:
        linhas.append(f"⭐ {nome_atual}: primeira vez (sem maestria)")
    if maestrias:
        top = " • ".join(
            f"{i}. {nomes.get(int(m.get('championId', 0)), '?')} ({_pontos(m.get('championPoints'))})"
            for i, m in enumerate(maestrias[:3], 1)
        )
        linhas.append(f"👑 Top maestria: {top}")

    if not jogos:
        linhas.append("📈 Sem partidas recentes")
        return "\n".join(linhas)
    v = sum(1 for j in jogos if j["vitoria"])
    linhas.append(
        f"📈 Últimas {len(jogos)}: **{v}V {len(jogos) - v}D** ({_porcentagem(v, len(jogos))}%) "
        + "".join("✅" if j["vitoria"] else "❌" for j in jogos)
    )
    com = [j for j in jogos if j["campeao_id"] == cid]
    if com:
        vc = sum(1 for j in com if j["vitoria"])
        linhas.append(
            f"🎯 Com {nome_atual}: {len(com)} de {len(jogos)} • {vc}V {len(com) - vc}D "
            f"({_porcentagem(vc, len(com))}%)"
            + (" • 🔥 jogando muito com ele" if len(com) >= 3 else "")
        )
    else:
        linhas.append(f"🎯 Não jogou de {nome_atual} nas últimas {len(jogos)}")
    contagem: dict[int, list[int]] = {}
    for j in jogos:
        contagem.setdefault(j["campeao_id"], [0, 0])
        contagem[j["campeao_id"]][0] += 1
        contagem[j["campeao_id"]][1] += int(j["vitoria"])
    ranking = sorted(contagem.items(), key=lambda item: (-item[1][0], -item[1][1]))[:3]
    linhas.append("🔁 Mais jogados: " + " • ".join(
        f"{nomes.get(c, f'Campeão {c}')} {q}j ({w}V {q - w}D)" for c, (q, w) in ranking
    ))
    partidas = []
    for j in jogos:
        modo = await nome_fila_lol({"gameQueueConfigId": j.get("fila") or 0})
        rota = NOMES_ROTA.get(j.get("rota", ""), "")
        quando = f" <t:{j['fim']}:R>" if j.get("fim") else ""
        partidas.append(
            f"{'✅' if j['vitoria'] else '❌'} {nomes.get(j['campeao_id'], j.get('campeao') or '?')}"
            f" • {modo}{' • ' + rota if rota else ''}{quando}"
        )
    linhas.append("🗂️ Partidas:\n" + "\n".join(partidas))
    return "\n".join(linhas)


async def montar_detalhes(jogo: dict[str, Any], puuid_ref: str) -> list[discord.Embed]:
    """Um embed por time com todos os detalhes (enviados em mensagens separadas)."""
    participantes = list(jogo.get("participants", []))[:10]
    nomes = await nomes_campeoes()
    icones = await icones_campeoes({int(p.get("championId", 0)) for p in participantes})

    async def bloco(p: dict[str, Any]) -> str:
        if not p.get("puuid") or p.get("bot"):
            return "🤖 Bot"
        try:
            return await detalhes_jogador(p, nomes, icones)
        except ErroRiot as exc:
            return f"⚠️ {exc}"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            log.warning("Ao vivo: falha nos detalhes: %s", exc)
            return "⚠️ Não consegui falar com a Riot agora."

    blocos = await asyncio.gather(*(bloco(p) for p in participantes))
    time_ref = next((p.get("teamId") for p in participantes if p.get("puuid") == puuid_ref), 100)
    embeds = []
    for time_id in sorted({p.get("teamId") for p in participantes}, key=lambda t: t != time_ref):
        emoji, titulo, cor = NOMES_TIME_LOL.get(time_id, ("⚪", f"Time {time_id}", 0x99AAB5))
        embed = discord.Embed(title=f"📊 {emoji} {titulo} | detalhes completos", color=cor)
        for p, texto in zip(participantes, blocos):
            if p.get("teamId") == time_id:
                embed.add_field(
                    name=discord.utils.escape_markdown(p.get("riotId") or "Jogador")[:256],
                    value=texto[:1024], inline=False,
                )
        embed.set_footer(text=f"Últimas {AOVIVO_PARTIDAS_DESEMPENHO} partidas de cada jogador.")
        caber_no_discord([embed])
        embeds.append(embed)
    return embeds


class DetalhesAoVivoView(discord.ui.View):
    """Botão '📊 Detalhes completos': mostra tudo, só para quem clicou."""

    def __init__(self, jogo: dict[str, Any], puuid_ref: str) -> None:
        super().__init__(timeout=3 * 3600)
        self.jogo = jogo
        self.puuid_ref = puuid_ref
        self.trava = asyncio.Lock()
        self.pronto: tuple[float, list[discord.Embed]] | None = None

    @discord.ui.button(label="Detalhes completos", emoji="📊", style=discord.ButtonStyle.primary)
    async def detalhes(self, inter: discord.Interaction, botao: discord.ui.Button) -> None:
        await inter.response.defer(ephemeral=True, thinking=True)
        async with self.trava:
            if self.pronto is None or time.time() - self.pronto[0] > ESTATISTICAS_VALIDADE_SEGUNDOS:
                self.pronto = (time.time(), await montar_detalhes(self.jogo, self.puuid_ref))
        # Um time por mensagem para caber no limite de tamanho do Discord.
        for embed in self.pronto[1]:
            await inter.followup.send(
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )


@bot.tree.command(
    name="aovivo", description="Mostra os jogadores da partida que você (ou um membro) está jogando agora"
)
@app_commands.describe(
    jogador="Membro com conta vinculada (se vazio, você)",
    conta="Qual conta Riot ver (se vazio, procura em todas as contas vinculadas)",
)
@app_commands.autocomplete(conta=autocompletar_conta_riot)
@app_commands.guild_only()
async def aovivo(
    inter: discord.Interaction,
    jogador: discord.Member | None = None,
    conta: Optional[str] = None,
) -> None:
    if not RIOT_API_KEY:
        await inter.response.send_message(
            "⚠️ A API da Riot ainda não foi configurada no bot (RIOT_API_KEY).", ephemeral=True
        )
        return
    alvo = jogador or inter.user
    contas = contas_do_usuario(alvo.id)
    if conta:
        escolhida = encontrar_conta_riot(contas, conta)
        contas = [escolhida] if escolhida else []
    if not contas:
        await inter.response.send_message(
            f"🔗 {alvo.mention} não tem essa conta vinculada. Use `/link Nick#TAG`.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    for c in contas:
        try:
            jogo = await partida_ativa(c["puuid"])
        except ErroRiot as exc:
            await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("Ao vivo: falha ao consultar a partida: %s", exc)
            await inter.followup.send("⚠️ Não consegui falar com a Riot agora.", ephemeral=True)
            return
        if jogo:
            embeds = await montar_aovivo(jogo, c["puuid"], com_desempenho=False)
            mensagem = await inter.followup.send(
                content=f"🎮 **{discord.utils.escape_markdown(c.get('riot_id', ''))}** está em partida:",
                embeds=embeds[:10], ephemeral=True, wait=True,
                view=DetalhesAoVivoView(jogo, c["puuid"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            try:
                completo = await montar_aovivo(jogo, c["puuid"])
                await mensagem.edit(embeds=completo[:10])
            except (discord.HTTPException, ErroRiot, aiohttp.ClientError,
                    asyncio.TimeoutError, ValueError) as exc:
                log.warning("Ao vivo: não consegui completar o desempenho: %s", exc)
            return
    await inter.followup.send(
        f"💤 {alvo.mention} não está em partida agora. "
        "A partida aparece a partir da tela de carregamento.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="aovivo_canal",
    description="Canal onde o bot avisa quando um membro vinculado entra em partida (vazio desliga)",
)
@app_commands.describe(canal="Canal de texto dos avisos. Deixe vazio para desligar os avisos")
@app_commands.guild_only()
async def aovivo_canal(inter: discord.Interaction, canal: discord.TextChannel | None = None) -> None:
    if inter.guild is None or not (autorizado(inter) or await pode_gerenciar_contas(inter)):
        await inter.response.send_message(
            "🔒 Só quem tem **Gerenciar Canais**, o dono ou o cargo autorizado.", ephemeral=True
        )
        return
    gid = str(inter.guild.id)
    anterior = aovivo_config["canais"].get(gid)
    if canal is None:
        aovivo_config["canais"].pop(gid, None)
    else:
        permissoes = canal.permissions_for(inter.guild.me)
        if not (permissoes.send_messages and permissoes.embed_links):
            await inter.response.send_message(
                f"⚠️ Preciso de **Enviar Mensagens** e **Inserir Links** em {canal.mention}.",
                ephemeral=True,
            )
            return
        aovivo_config["canais"][gid] = canal.id
    try:
        salvar_aovivo()
    except OSError as exc:
        if anterior is None:
            aovivo_config["canais"].pop(gid, None)
        else:
            aovivo_config["canais"][gid] = anterior
        log.exception("Não consegui salvar aovivo.json: %s", exc)
        await inter.response.send_message("⚠️ Não consegui salvar em /data/aovivo.json.", ephemeral=True)
        return
    if canal is None:
        texto = "🔕 Avisos de partida ao vivo **desligados** neste servidor."
    else:
        texto = (
            f"🔔 Avisos de partida ao vivo em {canal.mention}.\n"
            "Quando alguém com conta vinculada (`/link`) entrar em partida, "
            "eu posto os 10 jogadores lá. Quem não quiser ser avisado usa `/aovivo_avisos`."
        )
    await inter.response.send_message(texto, ephemeral=True)


@bot.tree.command(
    name="aovivo_avisos", description="Liga ou desliga os avisos automáticos das SUAS partidas"
)
@app_commands.describe(avisos="Se o bot deve anunciar quando você entrar em partida")
@app_commands.choices(avisos=[
    app_commands.Choice(name="🔔 Ligado", value="ligado"),
    app_commands.Choice(name="🔕 Desligado", value="desligado"),
])
async def aovivo_avisos(inter: discord.Interaction, avisos: app_commands.Choice[str]) -> None:
    uid = str(inter.user.id)
    lista = [str(x) for x in aovivo_config.get("sem_aviso", []) if str(x) != uid]
    if avisos.value == "desligado":
        lista.append(uid)
    anterior = aovivo_config.get("sem_aviso", [])
    aovivo_config["sem_aviso"] = lista
    try:
        salvar_aovivo()
    except OSError as exc:
        aovivo_config["sem_aviso"] = anterior
        log.exception("Não consegui salvar aovivo.json: %s", exc)
        await inter.response.send_message("⚠️ Não consegui salvar a preferência.", ephemeral=True)
        return
    await inter.response.send_message(
        "🔔 Suas partidas voltarão a ser anunciadas." if avisos.value == "ligado"
        else "🔕 Suas partidas não serão mais anunciadas. O `/aovivo` continua funcionando.",
        ephemeral=True,
    )


async def completar_aviso(mensagem: discord.Message, jogo: dict[str, Any], puuid_ref: str) -> None:
    """Acrescenta o desempenho recente ao aviso já publicado."""
    try:
        completo = await montar_aovivo(jogo, puuid_ref)
        await mensagem.edit(embeds=completo[:10])
    except (discord.HTTPException, ErroRiot, aiohttp.ClientError,
            asyncio.TimeoutError, ValueError, KeyError) as exc:
        log.warning("Ao vivo: não consegui completar o aviso: %s", exc)


@tasks.loop(seconds=10)
async def vigiar_partidas_aovivo() -> None:
    global _aovivo_proxima
    if not RIOT_API_KEY or not aovivo_config.get("canais") or time.time() < _aovivo_proxima:
        return
    try:
        sem_aviso = {str(x) for x in aovivo_config.get("sem_aviso", [])}
        alvos: dict[str, int] = {}
        for uid, registro in contas_riot.items():
            if uid in sem_aviso:
                continue
            for c in (registro or {}).get("contas", []):
                if c.get("puuid"):
                    alvos[c["puuid"]] = int(uid)
        intervalo = max(AOVIVO_INTERVALO_MIN, math.ceil(len(alvos) * 60 / AOVIVO_CONSULTAS_POR_MIN))
        _aovivo_proxima = time.time() + intervalo

        novos: dict[str, dict[str, Any]] = {}
        for puuid in alvos:
            try:
                jogo = await partida_ativa(puuid)
            except ErroRiot as exc:
                log.warning("Ao vivo: %s", exc)
                if exc.status in (0, 401, 403, 429):
                    _aovivo_proxima = time.time() + 300
                    break
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                continue
            gid = jogo.get("gameId") if jogo else None
            if (gid and _aovivo_ultimo_jogo.get(puuid) != gid
                    and len(jogo.get("participants", [])) <= 10
                    and int(jogo.get("gameLength") or 0) < AOVIVO_PARTIDA_NOVA):
                novos[puuid] = jogo
            _aovivo_ultimo_jogo[puuid] = gid

        agora = time.time()
        for chave in [k for k, t in _aovivo_anunciados.items() if agora - t > 6 * 3600]:
            del _aovivo_anunciados[chave]
        if not novos:
            return

        for guild_id, canal_id in list(aovivo_config["canais"].items()):
            guild = bot.get_guild(int(guild_id))
            canal = guild.get_channel(int(canal_id)) if guild else None
            if not isinstance(canal, discord.TextChannel):
                continue
            grupos: dict[int, list[tuple[int, str, dict[str, Any]]]] = {}
            for puuid, jogo in novos.items():
                uid = alvos[puuid]
                if guild.get_member(uid) is not None:
                    grupos.setdefault(int(jogo["gameId"]), []).append((uid, puuid, jogo))
            for game_id, membros in grupos.items():
                if (guild.id, game_id) in _aovivo_anunciados:
                    continue
                _aovivo_anunciados[(guild.id, game_id)] = agora
                try:
                    _uid, puuid_ref, jogo = membros[0]
                    embeds = await montar_aovivo(jogo, puuid_ref, com_desempenho=False)
                    quem = ", ".join(dict.fromkeys(f"<@{u}>" for u, _p, _j in membros))
                    verbo = "entraram" if len({u for u, _p, _j in membros}) > 1 else "entrou"
                    mensagem_aviso = await canal.send(
                        content=f"🎮 {quem} {verbo} em partida!", embeds=embeds[:10],
                        view=DetalhesAoVivoView(jogo, puuid_ref),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
                    asyncio.create_task(completar_aviso(mensagem_aviso, jogo, puuid_ref))
                except (discord.HTTPException, ErroRiot, aiohttp.ClientError,
                        asyncio.TimeoutError, ValueError, KeyError) as exc:
                    log.warning("Ao vivo: não consegui anunciar a partida: %s", exc)
    except Exception:
        # Um erro inesperado não pode desligar a vigilância.
        log.exception("Ao vivo: erro inesperado na checagem")


@vigiar_partidas_aovivo.before_loop
async def _antes_vigiar_aovivo() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_connect() -> None:
    if not vigiar_partidas_aovivo.is_running():
        vigiar_partidas_aovivo.start()
    if not vigiar_personalizadas.is_running():
        vigiar_personalizadas.start()
    iniciar_recursos_extras()
    # Botões de vencedor continuam funcionando depois de reiniciar o bot.
    for chave, partida in list(partidas.items()):
        botoes = partida.get("lol_botoes_msg")
        if botoes and not partida.get("vencedor"):
            try:
                bot.add_view(BotoesVencedorView(chave, partida), message_id=int(botoes[1]))
            except (ValueError, TypeError) as exc:
                log.warning("Não consegui restaurar os botões de vencedor: %s", exc)




# ================= VENCEDOR AUTOMÁTICO DAS PERSONALIZADAS =================
# Quando os jogadores de uma partida interna do bot entram numa personalizada do LoL,
# o bot liga as duas. Quando a partida do LoL termina, ele busca o resultado na Riot e
# finaliza a partida interna com encerrar_partida(): registra o vencedor, devolve todos
# para a call de origem e apaga os canais (igual ao /partida_finalizar).
# /partida_finalizar e /partida_cancelar continuam funcionando normalmente.
AUTO_MINIMO_VINCULADOS = 2         # Membros vinculados da partida que precisam estar no jogo.


def _somar_devolvidos(mensagem: str, ja_devolvidos: int) -> str:
    """Soma quem já voltou para a call no fim da partida ao número da mensagem final."""
    if not ja_devolvidos:
        return mensagem
    return re.sub(
        r"(Pessoas devolvidas ao canal original: \*\*)(\d+)(\*\*)",
        lambda m: f"{m.group(1)}{int(m.group(2)) + ja_devolvidos}{m.group(3)}",
        mensagem, count=1,
    )


def _texto_final(guild: discord.Guild, mensagem: str, varias: bool) -> str:
    """Com uma partida só, tira a linha '🎮 Partida N' e começa pelo vencedor."""
    if not varias:
        primeira, _sep, resto = mensagem.partition("\n")
        if primeira.startswith("🎮") and resto:
            return resto
    return mensagem


class BotoesVencedorView(discord.ui.View):
    """Botões de vencedor quando a Riot não informa o resultado da personalizada."""

    def __init__(self, chave: str, partida: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.chave = chave
        times = list((partida.get("canais") or {}).keys()) or ["vermelho", "azul"]
        for time_chave in times:
            emoji, nome = next(((e, n) for c, e, n in EQUIPES if c == time_chave), ("🏆", time_chave))
            botao = discord.ui.Button(
                label=f"{nome} venceu", emoji=emoji, style=discord.ButtonStyle.secondary,
                custom_id=f"vencedor:{chave}:{time_chave}"[:100],
            )
            botao.callback = self._criar_callback(time_chave)
            self.add_item(botao)

    def _criar_callback(self, vencedor: str):
        async def callback(inter: discord.Interaction) -> None:
            guild = inter.guild
            if guild is None or not (autorizado(inter) or await pode_gerenciar_contas(inter)):
                await inter.response.send_message(
                    "⛔ Apenas membros com **Gerenciar Canais** podem registrar o vencedor.", ephemeral=True
                )
                return
            await inter.response.defer()
            async with trava_servidor(guild.id):
                if self.chave not in partidas:
                    await inter.followup.send("ℹ️ Essa partida já foi finalizada.", ephemeral=True)
                    try:
                        await inter.message.edit(view=None)
                    except (discord.HTTPException, AttributeError):
                        pass
                    return
                varias = len(partidas_do_servidor(guild.id)) > 1
                partida_atual = partidas[self.chave]
                ja = int(partida_atual.get("lol_devolvidos") or 0)
                mensagem = _somar_devolvidos(await encerrar_partida(guild, self.chave, vencedor), ja)
            try:
                await inter.message.edit(content=_texto_final(guild, mensagem, varias),
                                         view=view_revanche(partida_atual),
                                         allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, AttributeError):
                await inter.followup.send(_texto_final(guild, mensagem, varias),
                                          allowed_mentions=discord.AllowedMentions.none())
        return callback


def _puuids_da_partida(partida: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """puuid -> (id do membro, time no bot) dos participantes com conta vinculada."""
    resultado: dict[str, tuple[int, str]] = {}
    for uid, cadastro in (partida.get("participantes") or {}).items():
        for conta in (contas_riot.get(str(uid)) or {}).get("contas", []):
            if conta.get("puuid"):
                resultado[conta["puuid"]] = (int(uid), str((cadastro or {}).get("time", "")))
    return resultado


def _canal_de_aviso(guild: discord.Guild, partida: dict[str, Any]) -> discord.TextChannel | None:
    for canal_id in (partida.get("assistir_channel_id"), aovivo_config["canais"].get(str(guild.id))):
        canal = guild.get_channel(int(canal_id)) if canal_id else None
        if isinstance(canal, discord.TextChannel):
            return canal
    return guild.system_channel


def _nome_time(chave: str) -> str:
    return next((f"{emoji} {nome}" for c, emoji, nome in EQUIPES if c == chave), chave)


async def _ligar_partida_lol(guild: discord.Guild, chave: str, partida: dict[str, Any]) -> None:
    """Procura a personalizada do LoL em que os jogadores desta partida estão."""
    vinculados = _puuids_da_partida(partida)
    minimo = 1 if partida.get("teste") else AUTO_MINIMO_VINCULADOS
    if len(vinculados) < minimo:
        return
    for puuid in list(vinculados)[:3]:  # Basta achar um jogador na partida.
        jogo = await partida_ativa(puuid)
        if not jogo:
            continue
        personalizada = eh_personalizada(jogo)
        log.info(
            "Auto vencedor: partida encontrada fila=%s tipo=%s modo=%s",
            jogo.get("gameQueueConfigId"), jogo.get("gameType"), jogo.get("gameMode"),
        )
        presentes = [p for p in jogo.get("participants", []) if p.get("puuid") in vinculados]
        if not personalizada or len(presentes) < minimo:
            return
        partida["lol_game_id"] = int(jogo["gameId"])
        partida["lol_desde"] = time.time()
        partida["lol_puuids"] = [p["puuid"] for p in presentes][:3]
        partida.pop("lol_terminou_em", None)
        salvar_partidas()
        canal = _canal_de_aviso(guild, partida)
        if canal:
            try:
                await canal.send(
                    f"🔗 **Partida {partida.get('numero', 1)}** ligada à personalizada do LoL. "
                    "Quando ela acabar, eu devolvo todos para a call e anuncio o vencedor.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                log.warning("Auto vencedor: não consegui avisar: %s", exc)
        return


AUTO_ESPERA_RESULTADO = 10 * 60   # Depois do fim, tempo esperando a Riot publicar o resultado.


async def _pedir_vencedor_com_botoes(
    guild: discord.Guild, chave: str, partida: dict[str, Any], parar: bool = True
) -> None:
    """Posta os botões de vencedor (igual ao /partida_finalizar).
    parar=False: continua tentando o resultado automático em paralelo."""
    if parar:
        partida.pop("lol_game_id", None)
        partida["lol_desistiu"] = True
        salvar_partidas()
    if partida.get("lol_botoes_msg"):
        return  # Os botões já estão no canal.
    canal = _canal_de_aviso(guild, partida)
    if canal is None:
        return
    titulo = f"🎮 **Partida {partida.get('numero', 1)}**\n" if len(partidas_do_servidor(guild.id)) > 1 else ""
    mensagem = await canal.send(
        titulo + (
            f"🏁 **A partida acabou!** ↩️ **{partida['lol_devolvidos']}** pessoa(s) já voltaram para a call.\n"
            if partida.get("lol_devolvidos") else "🏁 **A partida acabou!**\n"
        ) + "A Riot não informou o resultado desta personalizada. **Quem venceu?**",
        view=BotoesVencedorView(chave, partida),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    partida["lol_botoes_msg"] = [canal.id, mensagem.id]
    salvar_partidas()


async def _conferir_resultado(guild: discord.Guild, chave: str, partida: dict[str, Any]) -> None:
    """Se a partida do LoL terminou, registra o vencedor e finaliza a partida interna."""
    # 1) Ainda está em jogo? (mesma consulta do /aovivo)
    if not partida.get("lol_terminou_em"):
        for puuid in partida.get("lol_puuids") or list(_puuids_da_partida(partida))[:3]:
            jogo = await partida_ativa(puuid)
            if jogo and int(jogo.get("gameId") or 0) == int(partida["lol_game_id"]):
                return  # Ainda jogando.
        partida["lol_terminou_em"] = time.time()
        log.info("Auto vencedor: a partida %s acabou.", partida["lol_game_id"])
        # Devolve todos para a call na hora (sem apagar os canais ainda).
        async with trava_servidor(guild.id):
            if chave not in partidas:
                return
            devolvidos, problemas = await devolver_jogadores(guild, partida)
        partida["lol_devolvidos"] = int(partida.get("lol_devolvidos") or 0) + devolvidos
        salvar_partidas()
        if problemas:
            log.warning("Auto vencedor: pendências ao devolver: %s", "; ".join(problemas[:5]))

    # 2) Acabou: tenta o resultado oficial.
    match_id = f"{RIOT_PLATAFORMA.upper()}_{partida['lol_game_id']}"
    try:
        dados = await chamar_riot(
            f"https://{RIOT_REGIAO}.api.riotgames.com/lol/match/v5/matches/{quote(match_id, safe='')}"
        )
    except ErroRiot as exc:
        if exc.status not in (403, 404):
            raise
        # A Riot ainda não publicou (ou não publica personalizadas).
        log.info("Auto vencedor: resultado de %s ainda indisponível (HTTP %s).", match_id, exc.status)
        espera = time.time() - float(partida["lol_terminou_em"])
        if espera > 90:
            # Todos já estão na call; pergunta o vencedor e segue tentando por mais um tempo.
            await _pedir_vencedor_com_botoes(
                guild, chave, partida, parar=espera > AUTO_ESPERA_RESULTADO
            )
        return

    info = dados.get("info", {}) if isinstance(dados, dict) else {}
    canal = _canal_de_aviso(guild, partida)
    if not info.get("participants"):
        return
    if int(info.get("gameDuration") or 0) < 300:
        # Remake: desfaz a ligação para detectar a próxima personalizada.
        partida.pop("lol_game_id", None)
        partida.pop("lol_terminou_em", None)
        botoes = partida.pop("lol_botoes_msg", None)
        salvar_partidas()
        if botoes:
            try:
                await guild.get_channel(botoes[0]).get_partial_message(botoes[1]).delete()
            except (discord.HTTPException, AttributeError):
                pass
        if canal:
            await canal.send(
                f"🔁 A personalizada da **Partida {partida.get('numero', 1)}** foi refeita (remake). "
                "Vou esperar a próxima."
            )
        return

    vinculados = _puuids_da_partida(partida)
    votos: dict[str, int] = {}
    for p in info["participants"]:
        if p.get("win") and p.get("puuid") in vinculados:
            time_bot = vinculados[p["puuid"]][1]
            votos[time_bot] = votos.get(time_bot, 0) + 1
    if not votos:
        await _pedir_vencedor_com_botoes(guild, chave, partida)
        return
    ordenados = sorted(votos.items(), key=lambda item: -item[1])
    if len(ordenados) > 1 and ordenados[0][1] == ordenados[1][1]:
        await _pedir_vencedor_com_botoes(guild, chave, partida)
        return
    await finalizar_com_vencedor(guild, chave, partida, ordenados[0][0])


async def finalizar_com_vencedor(
    guild: discord.Guild, chave: str, partida: dict[str, Any], vencedor: str
) -> bool:
    """Registra o vencedor, devolve todos, apaga os canais e anuncia. False se já acabou."""
    canal = _canal_de_aviso(guild, partida)
    async with trava_servidor(guild.id):
        if chave not in partidas:  # Alguém finalizou enquanto isso.
            return False
        varias = len(partidas_do_servidor(guild.id)) > 1
        botoes = partida.get("lol_botoes_msg")
        ja = int(partida.get("lol_devolvidos") or 0)
        mensagem = _texto_final(
            guild, _somar_devolvidos(await encerrar_partida(guild, chave, vencedor), ja), varias
        )
    revanche = view_revanche(partida)
    if botoes:
        try:
            await guild.get_channel(botoes[0]).get_partial_message(botoes[1]).edit(
                content=mensagem, view=revanche, allowed_mentions=discord.AllowedMentions.none()
            )
            return True
        except (discord.HTTPException, AttributeError):
            pass
    if canal:
        await canal.send(mensagem, view=revanche, allowed_mentions=discord.AllowedMentions.none())
    return True


@tasks.loop(seconds=30)
async def vigiar_personalizadas() -> None:
    if not RIOT_API_KEY or not partidas:
        return
    for chave, partida in list(partidas.items()):
        guild = bot.get_guild(int(partida.get("guild_id") or 0))
        if guild is None or partida.get("vencedor") or partida.get("lol_desistiu"):
            continue
        try:
            if partida.get("lol_game_id"):
                await _conferir_resultado(guild, chave, partida)
            else:
                await _ligar_partida_lol(guild, chave, partida)
        except ErroRiot as exc:
            log.warning("Auto vencedor: %s", exc)
            if exc.status in (0, 401, 403, 429):
                return
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError,
                discord.HTTPException) as exc:
            log.warning("Auto vencedor: falha na partida %s: %s", chave, exc)
        except Exception:
            log.exception("Auto vencedor: erro inesperado")


@vigiar_personalizadas.before_loop
async def _antes_vigiar_personalizadas() -> None:
    await bot.wait_until_ready()




# ============ VINCULAR NO BOTÃO ENTRAR DA /queue (primeira vez) ============
class _RespostaDepoisDoModal:
    """Faz o botão Entrar funcionar depois da janela: responde e edita o painel da fila."""

    def __init__(self, inter: discord.Interaction) -> None:
        self._inter = inter

    def is_done(self) -> bool:
        return True

    async def send_message(self, content: str | None = None, **kwargs: Any) -> None:
        kwargs.pop("ephemeral", None)
        await self._inter.followup.send(content, ephemeral=True, **kwargs)

    async def edit_message(self, **kwargs: Any) -> None:
        if self._inter.message is not None:
            await self._inter.message.edit(**kwargs)


class _InteracaoDepoisDoModal:
    def __init__(self, inter: discord.Interaction) -> None:
        self._inter = inter
        self.response = _RespostaDepoisDoModal(inter)

    def __getattr__(self, nome: str) -> Any:
        return getattr(self._inter, nome)


class ModalVincularAoEntrar(discord.ui.Modal):
    """Aparece no primeiro clique em Entrar de quem ainda não vinculou uma conta."""

    def __init__(self, view_fila: "FilaView") -> None:
        super().__init__(title="Vincule sua conta para entrar", timeout=600)
        self.view_fila = view_fila
        self.entrada = discord.ui.Label(
            text="Seu Riot ID (Nick#TAG)",
            description="Usado no elo da fila, nas estatísticas e no vencedor das personalizadas.",
            component=discord.ui.TextInput(
                placeholder="Nick#BR1", min_length=5, max_length=40, required=True
            ),
        )
        self.add_item(self.entrada)

    async def on_submit(self, inter: discord.Interaction) -> None:
        campo = self.entrada.component
        assert isinstance(campo, discord.ui.TextInput)
        separado = separar_riot_id(campo.value)
        if separado is None:
            await inter.response.send_message(
                "⚠️ Use o formato **Nick#TAG** (ex: Nick#BR1). Clique em **Entrar** de novo.",
                ephemeral=True,
            )
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            riot_nome, elo, _total = await vincular_conta_riot(inter.user.id, *separado)
        except ErroVinculo as exc:
            await inter.followup.send(
                f"⚠️ {exc}\nClique em **Entrar** de novo para tentar.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await inter.followup.send(
            f"✅ Conta vinculada: **{discord.utils.escape_markdown(riot_nome)}** • 🏅 {elo}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        # Continua exatamente como o botão Entrar faria.
        await FilaView.entrar(self.view_fila, _InteracaoDepoisDoModal(inter), None)
        if inter.guild is not None:
            try:
                await atualizar_paineis_do_jogador(inter.guild, inter.user.id)
            except (discord.HTTPException, ValueError, KeyError) as exc:
                log.warning("Não consegui atualizar os painéis após vincular: %s", exc)



# ======================= /teste DO VENCEDOR AUTOMÁTICO =======================
@bot.tree.command(
    name="teste",
    description="Cria uma partida interna de teste para conferir o vencedor automático da personalizada",
)
@app_commands.describe(
    amigo="Opcional: um amigo para o outro time (vocês jogam 1x1 e ele se rende ou perde)",
)
@app_commands.guild_only()
async def teste(inter: discord.Interaction, amigo: discord.Member | None = None) -> None:
    guild = inter.guild
    if guild is None or not (autorizado(inter) or await pode_gerenciar_contas(inter)):
        await inter.response.send_message(
            "🔒 Só quem tem **Gerenciar Canais**, o dono ou o cargo autorizado.", ephemeral=True
        )
        return
    if not RIOT_API_KEY:
        await inter.response.send_message("⚠️ A RIOT_API_KEY não está configurada.", ephemeral=True)
        return
    jogadores = [inter.user] + ([amigo] if amigo and amigo.id != inter.user.id and not amigo.bot else [])
    sem_conta = [m for m in jogadores if not contas_do_usuario(m.id)]
    if sem_conta:
        await inter.response.send_message(
            "🔗 Precisa ter conta vinculada: " + ", ".join(m.mention for m in sem_conta)
            + ". Use `/link Nick#TAG`.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    estado = getattr(inter.user, "voice", None)
    origem = estado.channel if estado else None
    if not isinstance(origem, discord.VoiceChannel):
        await inter.response.send_message("🎙️ Entre em um canal de voz antes de usar o /teste.", ephemeral=True)
        return
    eu = guild.me
    if eu is None or not (eu.guild_permissions.manage_channels and eu.guild_permissions.move_members):
        await inter.response.send_message(
            "⚠️ O bot precisa de **Gerenciar Canais** e **Mover Membros**.", ephemeral=True
        )
        return

    await inter.response.defer(thinking=True)
    async with trava_servidor(guild.id):
        chave = f"teste:{inter.id}"
        numero = numero_livre_partida(guild.id)
        times = {"vermelho": [inter.user]}
        if len(jogadores) > 1:
            times["azul"] = [jogadores[1]]
        partida: dict[str, Any] = {
            "guild_id": guild.id,
            "numero": numero,
            "origem_id": origem.id,
            "categoria_id": None,
            "canais": {},
            "vencedor": None,
            "espectadores": {},
            "assistir_channel_id": inter.channel_id,
            "assistir_message_id": None,
            "teste": True,
            "participantes": {
                str(m.id): {"origem_id": origem.id, "time": time_chave}
                for time_chave, membros in times.items() for m in membros
            },
        }
        partidas[chave] = partida
        salvar_partidas()
        try:
            categoria = await guild.create_category(
                f"🧪 TESTE {numero} EM ANDAMENTO", reason="/teste do vencedor automático"
            )
            partida["categoria_id"] = categoria.id
            salvar_partidas()
            for time_chave, membros in times.items():
                emoji, nome = next((e, n) for c, e, n in EQUIPES if c == time_chave)
                canal = await guild.create_voice_channel(
                    f"{emoji} Time {nome} 🔒", category=categoria,
                    overwrites=permissoes_de_time(guild, eu, membros),
                    reason="/teste do vencedor automático",
                )
                partida["canais"][time_chave] = canal.id
                salvar_partidas()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.exception("Falha ao criar a partida de teste: %s", exc)
            await remover_estrutura(guild, partida)
            partidas.pop(chave, None)
            salvar_partidas()
            await inter.followup.send("❌ Não consegui criar os canais do teste. Confira as permissões.")
            return

        movidos = 0
        for time_chave, membros in times.items():
            destino = buscar_voz(guild, partida["canais"][time_chave])
            for membro in membros:
                if membro.voice and membro.voice.channel and destino:
                    try:
                        await membro.move_to(destino, reason="/teste do vencedor automático")
                        movidos += 1
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("Teste: não consegui mover %s: %s", membro.id, exc)

    linhas = [
        f"🧪 **Partida de teste {numero} criada** ({movidos} movido(s)).",
        "🔴 Time Vermelho: " + inter.user.mention,
    ]
    if len(jogadores) > 1:
        linhas.append("🔵 Time Azul: " + jogadores[1].mention)
    linhas += [
        "",
        "**Agora:**",
        "1. Crie uma **partida personalizada** no LoL"
        + (" com o seu amigo no time adversário." if len(jogadores) > 1 else " (pode ser contra bots)."),
        "2. Quando abrir a tela de carregamento, em até 30 s eu aviso aqui que liguei a partida.",
        "3. Jogue **mais de 5 minutos** (antes disso a Riot conta como remake) e termine a partida.",
        "4. Quando acabar, devolvo todos para a call e anuncio o vencedor aqui. "
        "Se a Riot não informar o resultado, pergunto quem venceu.",
        "",
        "Para desistir do teste: `/partida_cancelar`.",
    ]
    await inter.followup.send("\n".join(linhas), allowed_mentions=discord.AllowedMentions.none())



# ============== APP DO PC: VENCEDOR NA HORA (Live Client Data da Riot) ==============
# O app "LoL Resultado" roda no PC de um jogador só enquanto estiver aberto.
# No fim da partida ele manda, por um webhook deste servidor, o Riot ID e Win/Lose.
# Os dados vão no NOME do webhook ("lolres Win Nick#TAG"), que o bot lê sem precisar
# da permissão de conteúdo de mensagens. O bot então finaliza a partida da /queue.
PREFIXO_APP = "lolres"


@bot.tree.command(
    name="app_resultado",
    description="Gera o link para o app do PC que avisa o vencedor das personalizadas na hora",
)
@app_commands.guild_only()
async def app_resultado(inter: discord.Interaction) -> None:
    guild = inter.guild
    if guild is None or not (autorizado(inter) or await pode_gerenciar_contas(inter)):
        await inter.response.send_message(
            "🔒 Só quem tem **Gerenciar Canais**, o dono ou o cargo autorizado.", ephemeral=True
        )
        return
    canal = inter.channel
    if not isinstance(canal, discord.TextChannel):
        await inter.response.send_message("Use este comando em um canal de texto.", ephemeral=True)
        return
    if not canal.permissions_for(guild.me).manage_webhooks:
        await inter.response.send_message(
            f"⚠️ Preciso da permissão **Gerenciar Webhooks** em {canal.mention}.", ephemeral=True
        )
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    webhooks = aovivo_config.setdefault("webhooks", {})
    gancho = None
    try:
        for existente in await canal.webhooks():
            if existente.id == webhooks.get(str(guild.id)):
                gancho = existente
                break
        if gancho is None:
            gancho = await canal.create_webhook(name="LoL Resultado", reason="/app_resultado")
    except discord.HTTPException as exc:
        log.warning("Não consegui criar o webhook do app: %s", exc)
        await inter.followup.send("⚠️ Não consegui criar o webhook neste canal.", ephemeral=True)
        return
    webhooks[str(guild.id)] = gancho.id
    try:
        salvar_aovivo()
    except OSError as exc:
        log.exception("Não consegui salvar aovivo.json: %s", exc)
    await inter.followup.send(
        "🔗 **Link do app LoL Resultado** (não compartilhe fora da staff):\n"
        f"```{gancho.url}```\n"
        "1. Abra o app no PC (**Abrir LoL Resultado.bat**).\n"
        "2. Cole este link no campo do app e clique em **Salvar**.\n"
        "3. Deixe o app aberto durante a personalizada. Quando o nexus cair, "
        "eu anuncio o vencedor e devolvo todos para a call.\n"
        "4. Terminou de jogar? Clique em **Desligar** no app.\n\n"
        "-# Use este comando em um canal privado da staff: o app posta lá e eu apago na hora.",
        ephemeral=True,
    )


async def _processar_resultado_app(guild: discord.Guild, resultado: str, riot_id: str) -> str:
    """Finaliza a partida da /queue do jogador que mandou o resultado pelo app."""
    alvo = riot_id.strip().casefold()
    uid = next(
        (int(u) for u, reg in contas_riot.items()
         if any(str(c.get("riot_id", "")).casefold() == alvo for c in (reg or {}).get("contas", []))),
        None,
    )
    if uid is None:
        return f"⚠️ O app mandou **{discord.utils.escape_markdown(riot_id)}**, mas essa conta não está vinculada."
    candidatas = [
        (chave, p) for chave, p in partidas_do_servidor(guild.id)
        if str(uid) in (p.get("participantes") or {}) and not p.get("vencedor")
    ]
    if not candidatas:
        return f"ℹ️ <@{uid}> não está em nenhuma partida da /queue em andamento."
    # Prefere a partida já ligada a uma personalizada; senão, a mais recente.
    chave, partida = sorted(candidatas, key=lambda item: (not item[1].get("lol_game_id"), -int(item[1].get("numero", 0))))[0]
    meu_time = str(partida["participantes"][str(uid)].get("time", ""))
    times = list((partida.get("canais") or {}).keys()) or ["vermelho", "azul"]
    if resultado == "win":
        vencedor = meu_time
    else:
        outros = [t for t in times if t != meu_time]
        if len(outros) != 1:
            await _pedir_vencedor_com_botoes(guild, chave, partida)
            return ""
        vencedor = outros[0]
    await finalizar_com_vencedor(guild, chave, partida, vencedor)
    return ""


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.guild is None or not message.webhook_id:
        return
    if aovivo_config.get("webhooks", {}).get(str(message.guild.id)) != message.webhook_id:
        return
    partes = (message.author.name or "").split(" ", 2)
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    if len(partes) == 3 and partes[0] == PREFIXO_APP and partes[1].lower() == "test":
        log.info("App resultado: teste de conexão recebido.")
        try:
            await message.channel.send("✅ **App LoL Resultado conectado ao bot!**", delete_after=20)
        except discord.HTTPException:
            pass
        return
    if len(partes) != 3 or partes[0] != PREFIXO_APP or partes[1].lower() not in ("win", "lose"):
        log.info("App resultado: mensagem ignorada (%s).", message.author.name)
        return
    log.info("App resultado: recebido %s", message.author.name)
    try:
        # O "#" do Riot ID chega como "|" (o Discord não aceita "#" no nome do webhook).
        nome, _sep, tag = partes[2].rpartition("|")
        riot_id = f"{nome}#{tag}" if nome else partes[2]
        aviso = await _processar_resultado_app(message.guild, partes[1].lower(), riot_id)
    except (discord.HTTPException, ValueError, KeyError) as exc:
        log.warning("App resultado: falha ao finalizar: %s", exc)
        aviso = "⚠️ Recebi o resultado do app, mas não consegui finalizar a partida."
    if aviso and isinstance(message.channel, discord.TextChannel):
        try:
            await message.channel.send(aviso, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


# =====================================================================
# ===================== AVISOS: CANAL/TÓPICO + MENSAGEM =================
# =====================================================================
# Cada recurso (atualização, rotação, status, quiz, perfil) tem um destino
# (canal de texto ou tópico) e uma mensagem personalizada opcional, que pode
# ter cargos (@cargo), emojis (inclusive animados) e quebras de linha (\n).
import copy
import io
import random

ARQUIVO_AVISOS = Path("/data/avisos.json")
TIPOS_AVISO = {
    "patch": "📋 Notas da atualização",
    "rotacao": "🔄 Rotação grátis",
    "status": "🛠️ Status dos servidores",
    "quiz": "🃏 Quiz automático",
    "perfil": "📊 Cartões de perfil",
}
VARIAVEIS_AVISO = {
    "patch": "{versao}",
    "rotacao": "{data}",
    "status": "{titulo}",
    "quiz": "",
    "perfil": "{membro}",
}


def _carregar_json(arquivo: Path, padrao: Any) -> Any:
    if not arquivo.exists():
        return padrao
    try:
        return json.loads(arquivo.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"Erro ao ler {arquivo.name}: {exc}") from exc


def _salvar_json(arquivo: Path, dados: Any) -> None:
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(arquivo)


avisos: dict[str, Any] = _carregar_json(ARQUIVO_AVISOS, {})


def config_aviso(guild_id: int, tipo: str) -> dict[str, Any] | None:
    return (avisos.get(str(guild_id)) or {}).get(tipo)


def destino_aviso(guild: discord.Guild, tipo: str) -> discord.TextChannel | discord.Thread | None:
    cfg = config_aviso(guild.id, tipo)
    if not cfg:
        return None
    canal = guild.get_channel_or_thread(int(cfg.get("destino") or 0))
    return canal if isinstance(canal, (discord.TextChannel, discord.Thread)) else None


def mensagem_aviso(guild_id: int, tipo: str, **valores: str) -> str | None:
    cfg = config_aviso(guild_id, tipo) or {}
    texto = str(cfg.get("mensagem") or "").replace("\\n", "\n")
    for chave, valor in valores.items():
        texto = texto.replace("{" + chave + "}", str(valor))
    return texto[:2000] or None


MENCOES_AVISO = discord.AllowedMentions(everyone=True, roles=True, users=True)


async def enviar_aviso(
    guild: discord.Guild, tipo: str, *, embeds: list[discord.Embed] | None = None,
    arquivo: discord.File | None = None, view: discord.ui.View | None = None, **valores: str,
) -> discord.Message | None:
    destino = destino_aviso(guild, tipo)
    if destino is None:
        return None
    kwargs: dict[str, Any] = {"content": mensagem_aviso(guild.id, tipo, **valores),
                              "allowed_mentions": MENCOES_AVISO}
    if embeds:
        kwargs["embeds"] = embeds[:10]
    if arquivo:
        kwargs["file"] = arquivo
    if view:
        kwargs["view"] = view
    try:
        return await destino.send(**kwargs)
    except discord.HTTPException as exc:
        log.warning("Aviso %s: não consegui postar em %s: %s", tipo, destino.id, exc)
        return None


async def _pode_configurar(inter: discord.Interaction) -> bool:
    return inter.guild is not None and (autorizado(inter) or await pode_gerenciar_contas(inter))


grupo_avisos = app_commands.Group(
    name="avisos", description="Onde e como o bot posta atualizações, rotação, status, quiz e perfis",
    guild_only=True,
)
ESCOLHAS_AVISO = [app_commands.Choice(name=nome, value=chave) for chave, nome in TIPOS_AVISO.items()]


@grupo_avisos.command(name="configurar", description="Escolhe o canal ou tópico e a mensagem de um aviso")
@app_commands.describe(
    tipo="Qual aviso configurar",
    destino="Canal de texto ou tópico onde o bot vai postar",
    mensagem="Opcional: texto junto do aviso. Aceita @cargos, emojis animados e \\n para pular linha",
    intervalo_horas="Só para o quiz automático: de quantas em quantas horas (padrão 6)",
)
@app_commands.choices(tipo=ESCOLHAS_AVISO)
async def avisos_configurar(
    inter: discord.Interaction, tipo: app_commands.Choice[str],
    destino: discord.TextChannel | discord.Thread,
    mensagem: Optional[str] = None,
    intervalo_horas: Optional[app_commands.Range[int, 1, 168]] = None,
) -> None:
    if not await _pode_configurar(inter):
        await inter.response.send_message("🔒 Só quem tem **Gerenciar Canais**, o dono ou o cargo autorizado.", ephemeral=True)
        return
    perm = destino.permissions_for(inter.guild.me)
    pode_enviar = perm.send_messages_in_threads if isinstance(destino, discord.Thread) else perm.send_messages
    faltando = [nome for nome, ok in (("Enviar mensagens", pode_enviar), ("Inserir links", perm.embed_links),
                                      ("Anexar arquivos", perm.attach_files)) if not ok]
    if faltando:
        await inter.response.send_message(
            f"⚠️ Preciso de **{', '.join(faltando)}** em {destino.mention}.", ephemeral=True
        )
        return
    cfg = avisos.setdefault(str(inter.guild.id), {}).setdefault(tipo.value, {})
    cfg["destino"] = destino.id
    if mensagem is not None:
        cfg["mensagem"] = mensagem.strip()
    if tipo.value == "quiz":
        cfg["intervalo"] = int(intervalo_horas or cfg.get("intervalo") or 6)
        cfg.setdefault("proximo", time.time() + cfg["intervalo"] * 3600)
    _salvar_json(ARQUIVO_AVISOS, avisos)
    aviso_cargo = ""
    if mensagem and "<@&" in mensagem and not perm.mention_everyone:
        aviso_cargo = ("\n-# Para marcar cargos que não são mencionáveis, dê ao bot a permissão "
                       "**Mencionar @everyone, @here e todos os cargos**.")
    extra = f"\n⏱️ A cada **{cfg['intervalo']}h**." if tipo.value == "quiz" else ""
    variavel = VARIAVEIS_AVISO.get(tipo.value)
    dica = f"\n-# Na mensagem, você pode usar `{variavel}`." if variavel else ""
    await inter.response.send_message(
        f"✅ **{tipo.name}** em {destino.mention}.{extra}\n"
        f"💬 Mensagem: {cfg.get('mensagem') or '*(nenhuma)*'}{dica}{aviso_cargo}\n"
        f"Use `/avisos testar` para ver como fica.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@grupo_avisos.command(name="mensagem", description="Muda só a mensagem personalizada de um aviso")
@app_commands.describe(tipo="Qual aviso", mensagem="Novo texto. Deixe vazio para tirar a mensagem")
@app_commands.choices(tipo=ESCOLHAS_AVISO)
async def avisos_mensagem(inter: discord.Interaction, tipo: app_commands.Choice[str], mensagem: Optional[str] = None) -> None:
    if not await _pode_configurar(inter):
        await inter.response.send_message("🔒 Sem permissão.", ephemeral=True)
        return
    cfg = config_aviso(inter.guild.id, tipo.value)
    if not cfg:
        await inter.response.send_message("Configure primeiro com `/avisos configurar`.", ephemeral=True)
        return
    cfg["mensagem"] = (mensagem or "").strip()
    _salvar_json(ARQUIVO_AVISOS, avisos)
    await inter.response.send_message(
        f"💬 Mensagem de **{tipo.name}**: {cfg['mensagem'] or '*(nenhuma)*'}",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
    )


@grupo_avisos.command(name="desligar", description="Desliga um aviso automático")
@app_commands.choices(tipo=ESCOLHAS_AVISO)
async def avisos_desligar(inter: discord.Interaction, tipo: app_commands.Choice[str]) -> None:
    if not await _pode_configurar(inter):
        await inter.response.send_message("🔒 Sem permissão.", ephemeral=True)
        return
    (avisos.get(str(inter.guild.id)) or {}).pop(tipo.value, None)
    _salvar_json(ARQUIVO_AVISOS, avisos)
    await inter.response.send_message(f"🔕 **{tipo.name}** desligado.", ephemeral=True)


@grupo_avisos.command(name="ver", description="Mostra onde cada aviso está configurado")
async def avisos_ver(inter: discord.Interaction) -> None:
    linhas = []
    for chave, nome in TIPOS_AVISO.items():
        cfg = config_aviso(inter.guild.id, chave)
        if not cfg:
            linhas.append(f"{nome}: *desligado*")
            continue
        extra = f" • a cada {cfg.get('intervalo', 6)}h" if chave == "quiz" else ""
        msg = f"\n  💬 {cfg['mensagem']}" if cfg.get("mensagem") else ""
        linhas.append(f"{nome}: <#{cfg['destino']}>{extra}{msg}")
    await inter.response.send_message("\n".join(linhas)[:2000], ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())


@grupo_avisos.command(name="testar", description="Posta um aviso agora no destino configurado")
@app_commands.choices(tipo=ESCOLHAS_AVISO)
async def avisos_testar(inter: discord.Interaction, tipo: app_commands.Choice[str]) -> None:
    if not await _pode_configurar(inter):
        await inter.response.send_message("🔒 Sem permissão.", ephemeral=True)
        return
    destino = destino_aviso(inter.guild, tipo.value)
    if destino is None:
        await inter.response.send_message("Configure primeiro com `/avisos configurar`.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        if tipo.value == "patch":
            versao, embeds = await montar_patch()
            ok = await enviar_aviso(inter.guild, "patch", embeds=embeds, view=await PatchSelecaoView.criar(),
                                    versao=".".join(nome_do_patch(versao)))
        elif tipo.value == "rotacao":
            ok = await enviar_aviso(inter.guild, "rotacao", embeds=[await montar_rotacao()],
                                    data=time.strftime("%d/%m"))
        elif tipo.value == "status":
            embed = await montar_status_atual()
            ok = await enviar_aviso(inter.guild, "status", embeds=[embed], titulo=embed.title or "")
        elif tipo.value == "quiz":
            ok = await iniciar_quiz(destino, mensagem_aviso(inter.guild.id, "quiz"))
        else:
            ok = await enviar_aviso(inter.guild, "perfil", embeds=[discord.Embed(
                description="📊 Os cartões do `/perfil` vão aparecer aqui.", color=0xC8AA6E)],
                membro=inter.user.mention)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, ErroRiot) as exc:
        log.warning("Avisos testar %s: %s", tipo.value, exc)
        ok = None
    await inter.followup.send(f"✅ Enviado em {destino.mention}." if ok else "⚠️ Não consegui enviar agora.",
                              ephemeral=True)


bot.tree.add_command(grupo_avisos)


# =====================================================================
# ========================= /patch (só em português) ====================
# =====================================================================
PATCH_URL = "https://www.leagueoflegends.com/pt-br/news/game-updates/league-of-legends-patch-{ano}-{numero}-notes/"
ROTULOS_ATRIBUTOS = {
    "hp": "Vida", "hpperlevel": "Vida/nível", "mp": "Mana", "mpperlevel": "Mana/nível",
    "armor": "Armadura", "armorperlevel": "Armadura/nível", "spellblock": "Resist. mágica",
    "spellblockperlevel": "Resist. mágica/nível", "attackdamage": "Dano de ataque",
    "attackdamageperlevel": "Dano de ataque/nível", "attackspeed": "Vel. de ataque",
    "attackspeedperlevel": "Vel. de ataque/nível", "movespeed": "Vel. de movimento",
    "attackrange": "Alcance", "hpregen": "Regen. de vida", "mpregen": "Regen. de mana",
}
_patch_cache: dict[str, list[discord.Embed]] = {}
_ddragon_cache: dict[str, Any] = {}


def nome_do_patch(versao: str) -> tuple[str, str]:
    """Versão do Data Dragon '16.23.1' -> ('26', '23'), o nome que a Riot usa (26.23)."""
    partes = versao.split(".")
    return str(int(partes[0]) + 10), str(int(partes[1]))


async def _baixar(url: str, json_: bool = True, binario: bool = False) -> Any:
    timeout = aiohttp.ClientTimeout(total=40, connect=8)
    cabecalhos = {"User-Agent": "Mozilla/5.0 (bot de Discord privado)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=cabecalhos) as sessao:
        async with sessao.get(url) as r:
            if r.status != 200:
                raise ValueError(f"HTTP {r.status} em {url}")
            if binario:
                return await r.read()
            return await (r.json(content_type=None) if json_ else r.text())


async def versoes_ddragon() -> list[str]:
    guardado = _ddragon_cache.get("versoes")
    if guardado and time.time() - guardado[0] < 1800:
        return guardado[1]
    versoes = await _baixar("https://ddragon.leagueoflegends.com/api/versions.json")
    _ddragon_cache["versoes"] = (time.time(), versoes)
    return versoes


async def versoes_de_patch() -> list[str]:
    """Versões do ano atual (uma por atualização), da mais nova para a mais antiga. Máx. 25."""
    versoes = await versoes_ddragon()
    major = versoes[0].split(".")[0]
    vistas, saida = set(), []
    for v in versoes:
        partes = v.split(".")
        if partes[0] != major or partes[1] in vistas:
            continue
        vistas.add(partes[1])
        saida.append(v)
    return saida[:25]


async def campeoes_completos(versao: str) -> dict[str, Any]:
    chave = f"full:{versao}"
    if chave not in _ddragon_cache:
        _ddragon_cache[chave] = await _baixar(
            f"https://ddragon.leagueoflegends.com/cdn/{versao}/data/pt_BR/championFull.json"
        )
        # Guarda só as duas versões mais recentes pedidas (os arquivos são grandes).
        cheias = [k for k in _ddragon_cache if k.startswith("full:")]
        for velha in cheias[:-3]:
            _ddragon_cache.pop(velha, None)
    return _ddragon_cache[chave]


def _numeros(valor: Any) -> str:
    if isinstance(valor, list):
        valores = [_numeros(v) for v in valor]
        return valores[0] if len(set(valores)) == 1 else "/".join(valores)
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).replace(".", ",")


async def mudancas_data_dragon(antigo: dict[str, Any], novo: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(campeões novos, skins novas, mudanças) com ÍCONES dos campeões no lugar dos nomes."""
    dados_novos = novo.get("data", {})
    ids = {int(c.get("key", 0)) for c in dados_novos.values()}
    icones = await icones_campeoes(ids)

    def icone(campeao: dict[str, Any]) -> str:
        return icones.get(int(campeao.get("key", 0))) or f"**{campeao.get('name', '?')}**"

    campeoes_novos, skins_novas, mudancas = [], [], []
    velhos = antigo.get("data", {})
    for chave, campeao in sorted(dados_novos.items(), key=lambda item: item[1].get("name", "")):
        anterior = velhos.get(chave)
        if anterior is None:
            campeoes_novos.append(icone(campeao))
            continue
        skins_antigas = {s.get("num") for s in anterior.get("skins", [])}
        for skin in campeao.get("skins", []):
            if skin.get("num") not in skins_antigas and skin.get("name") != "default":
                skins_novas.append(f"{icone(campeao)} {skin.get('name', '')}")
        linhas = []
        for atributo, rotulo in ROTULOS_ATRIBUTOS.items():
            a, b = anterior.get("stats", {}).get(atributo), campeao.get("stats", {}).get(atributo)
            if a is not None and b is not None and a != b:
                linhas.append(f"{'🔺' if b > a else '🔻'} {rotulo} {_numeros(a)} → {_numeros(b)}")
        for i, (magia_a, magia_b) in enumerate(zip(anterior.get("spells", []), campeao.get("spells", []))):
            for campo, rotulo in (("cooldown", "recarga"), ("cost", "custo"), ("range", "alcance")):
                a, b = magia_a.get(campo), magia_b.get(campo)
                if a and b and a != b:
                    linhas.append(f"🔄 {'QWER'[i] if i < 4 else '?'} {rotulo} {_numeros(a)} → {_numeros(b)}")
        if linhas:
            resto = f" • … +{len(linhas) - 3}" if len(linhas) > 3 else ""
            mudancas.append(f"{icone(campeao)} " + " • ".join(linhas[:3]) + resto)
    return campeoes_novos, skins_novas, mudancas


def imagens_da_pagina(html: str) -> tuple[str | None, list[str]]:
    """(imagem de destaques, artes das skins) da página oficial das notas."""
    corte = html.find('id="__NEXT_DATA__"')
    visivel = html[:corte] if corte > 0 else html
    padrao = r'https://cmsassets\.rgpub\.io/sanity/images/[^"\'\s<>()?]+?-(\d+)x(\d+)\.(?:png|jpe?g|webp)'

    def imagens(trecho: str) -> list[tuple[str, int, int]]:
        vistas, saida = set(), []
        for m in re.finditer(padrao, trecho):
            if m.group(0) not in vistas:
                vistas.add(m.group(0))
                saida.append((m.group(0), int(m.group(1)), int(m.group(2))))
        return saida

    baixo = visivel.lower()
    capa = re.search(r'property="og:image"\s+content="([^"]+)"', html)
    capa_url = capa.group(1).split("?")[0] if capa else ""
    destaque = None
    inicio = next((baixo.find(t) for t in ("destaques da atualiza", "destaques do patch", "patch highlights")
                   if baixo.find(t) >= 0), -1)
    for url, largura, altura in imagens(visivel[inicio:] if inicio >= 0 else visivel):
        if url != capa_url and largura >= 1200 and 1.6 <= largura / altura <= 1.9:
            destaque = url
            break
    skins = []
    ini = next((baixo.find(t) for t in ("cromas e skins futur", "skins e cromas futur", "próximas skins",
                                        "upcoming skins") if baixo.find(t) >= 0), -1)
    if ini >= 0:
        fim = next((baixo.find(t, ini) for t in ("artigos relacionados", "related articles")
                    if baixo.find(t, ini) >= 0), len(visivel))
        for url, largura, altura in imagens(visivel[ini:fim]):
            if largura >= 1200 and largura / altura <= 2.2 and url != destaque:  # skins, não as tiras de cromas
                skins.append(url)
    return destaque, skins


async def montar_patch(versao: str | None = None) -> tuple[str, list[discord.Embed]]:
    versoes = await versoes_ddragon()
    versao = versao or versoes[0]
    if versao in _patch_cache:
        return versao, _patch_cache[versao]
    anterior = versoes[versoes.index(versao) + 1] if versao in versoes and versoes.index(versao) + 1 < len(versoes) else None
    ano, numero = nome_do_patch(versao)

    link, html = None, ""
    try:
        url = PATCH_URL.format(ano=ano, numero=numero)
        html = await _baixar(url, json_=False)
        link = url
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        pass
    destaque, artes = imagens_da_pagina(html) if html else (None, [])

    novos, skins, mudancas = [], [], []
    if anterior:
        try:
            velho, atual = await asyncio.gather(campeoes_completos(anterior), campeoes_completos(versao))
            novos, skins, mudancas = await mudancas_data_dragon(velho, atual)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            log.warning("Patch: não consegui comparar o Data Dragon: %s", exc)

    partes = []
    if novos:
        partes.append("🆕 **Campeão novo:** " + " ".join(novos))
    if skins:
        partes.append("🎨 **Skins novas**\n" + "\n".join(skins[:10]) + ("\n…" if len(skins) > 10 else ""))
    if mudancas:
        texto = "\n".join(mudancas)
        if len(texto) > 2400:
            texto = texto[:2400].rsplit("\n", 1)[0] + "\n… veja o resto nas notas."
        partes.append("📊 **Mudanças em números**\n" + texto)
    if link:
        partes.append(f"🔗 **[Ler as notas completas]({link})**")
    else:
        partes.append("⏳ As notas em português desta atualização ainda não foram publicadas.")

    principal = discord.Embed(title=f"📋 Notas da Atualização {ano}.{numero}", url=link,
                              description="\n\n".join(partes)[:4096], color=0xC8AA6E)
    if destaque:
        principal.set_image(url=destaque)
    embeds = [principal]
    galeria = (link or "https://www.leagueoflegends.com/pt-br/") + "#skins"
    for i, arte in enumerate(artes[:4]):
        e = discord.Embed(url=galeria, color=0xC8AA6E)
        if i == 0:
            e.title = "🎨 Skins desta atualização"
        e.set_image(url=arte)
        embeds.append(e)
    embeds[-1].set_footer(text="Imagens e notas: Riot Games • Números: Data Dragon")
    caber_no_discord(embeds)
    if link:
        _patch_cache[versao] = embeds
    return versao, embeds


async def _opcoes_patch() -> list[discord.SelectOption]:
    opcoes = []
    for i, v in enumerate(await versoes_de_patch()):
        ano, numero = nome_do_patch(v)
        opcoes.append(discord.SelectOption(label=f"Atualização {ano}.{numero}", value=v,
                                           description="Mais recente" if i == 0 else None, emoji="📋"))
    return opcoes


class PatchSelecaoView(discord.ui.View):
    """Menu 'Ver atualizações anteriores' (continua funcionando depois de reiniciar)."""

    def __init__(self, opcoes: list[discord.SelectOption] | None = None) -> None:
        super().__init__(timeout=None)
        self.seletor = discord.ui.Select(
            custom_id="patch:anteriores", placeholder="📚 Ver atualizações anteriores",
            options=opcoes or [discord.SelectOption(label="Carregando...", value="carregando")],
        )
        self.seletor.callback = self.escolher
        self.add_item(self.seletor)

    @classmethod
    async def criar(cls) -> "PatchSelecaoView":
        try:
            return cls(await _opcoes_patch())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("Patch: não consegui montar a lista de atualizações: %s", exc)
            return cls()

    async def escolher(self, inter: discord.Interaction) -> None:
        versao = (inter.data or {}).get("values", [None])[0]
        if not versao or versao == "carregando":
            # Mensagem antiga, sem a lista: manda um menu novo já carregado.
            await inter.response.defer(ephemeral=True, thinking=True)
            view = await PatchSelecaoView.criar()
            if any(o.value != "carregando" for o in view.seletor.options):
                await inter.followup.send("📚 Escolha a atualização:", view=view, ephemeral=True)
            else:
                await inter.followup.send("⚠️ Não consegui carregar a lista agora. Tente de novo em alguns minutos.",
                                          ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            _v, embeds = await montar_patch(versao)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
            await inter.followup.send("⚠️ Não consegui buscar essa atualização agora.", ephemeral=True)
            return
        await inter.followup.send(embeds=embeds[:10], ephemeral=True)


async def autocompletar_patch(inter: discord.Interaction, atual: str) -> list[app_commands.Choice[str]]:
    try:
        versoes = await versoes_de_patch()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    escolhas = []
    for i, v in enumerate(versoes):
        nome = "Atualização {}.{}".format(*nome_do_patch(v)) + (" (mais recente)" if i == 0 else "")
        if atual.strip().lower() in nome.lower():
            escolhas.append(app_commands.Choice(name=nome, value=v))
    return escolhas[:25]


@bot.tree.command(name="patch", description="Notas da atualização do LoL (deixe vazio para a mais recente)")
@app_commands.describe(atualizacao="Escolha uma atualização anterior. Vazio = a mais recente")
@app_commands.autocomplete(atualizacao=autocompletar_patch)
async def patch(inter: discord.Interaction, atualizacao: Optional[str] = None) -> None:
    await inter.response.defer(thinking=True)
    try:
        if atualizacao and atualizacao not in await versoes_ddragon():
            atualizacao = None
        _versao, embeds = await montar_patch(atualizacao)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
        log.warning("Patch: falhou: %s", exc)
        await inter.followup.send("⚠️ Não consegui buscar a atualização agora. Tente de novo em alguns minutos.")
        return
    await inter.followup.send(embeds=embeds[:10], view=await PatchSelecaoView.criar())


async def checar_patch_novo() -> None:
    servidores = [g for g in bot.guilds if destino_aviso(g, "patch")]
    if not servidores:
        return
    versao = (await versoes_ddragon())[0]
    estado = avisos.setdefault("_estado", {})
    if estado.get("patch") is None:
        estado["patch"] = versao  # Primeira vez: só as próximas atualizações.
        _salvar_json(ARQUIVO_AVISOS, avisos)
        return
    if estado["patch"] == versao:
        return
    versao, embeds = await montar_patch(versao)
    if versao not in _patch_cache:
        return  # As notas em português ainda não saíram; tenta de novo depois.
    view = await PatchSelecaoView.criar()
    for guild in servidores:
        await enviar_aviso(guild, "patch", embeds=embeds, view=view, versao=".".join(nome_do_patch(versao)))
    estado["patch"] = versao
    _salvar_json(ARQUIVO_AVISOS, avisos)


# =====================================================================
# ================= ROTAÇÃO GRÁTIS (champion-v3, oficial) ===============
# =====================================================================
async def _icones_ou_nomes(ids: list[int]) -> list[str]:
    icones = await icones_campeoes(set(ids))
    nomes = await nomes_campeoes()
    return [icones.get(i) or f"**{nomes.get(i, i)}**" for i in ids]


async def _ids_de_campeoes(valor: Any) -> list[int]:
    """Aceita lista de IDs (números ou texto), nomes/chaves de campeão ou objetos com o ID."""
    if not isinstance(valor, list):
        return []
    nomes = await nomes_campeoes()
    por_nome = {_normalizar_nome(n): i for i, n in nomes.items()}
    try:
        versao = (await versoes_ddragon())[0]
        for c in (await campeoes_completos(versao)).get("data", {}).values():
            por_nome[_normalizar_nome(c.get("id", ""))] = int(c["key"])
            por_nome[_normalizar_nome(c.get("name", ""))] = int(c["key"])
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        pass
    ids = []
    for item in valor:
        if isinstance(item, dict):
            item = item.get("championId", item.get("id", item.get("key", item.get("name"))))
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, str) and item.strip().isdigit():
            ids.append(int(item.strip()))
        elif isinstance(item, str) and _normalizar_nome(item) in por_nome:
            ids.append(por_nome[_normalizar_nome(item)])
    return list(dict.fromkeys(ids))


def _normalizar_nome(texto: str) -> str:
    import unicodedata
    texto = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in texto.lower() if ch.isalnum())


async def ler_rotacao() -> tuple[list[int], list[int], int]:
    """A Riot mudou o formato desta resposta em 2026 ('sr'/'newplayer' no lugar dos nomes antigos)."""
    dados = await chamar_riot(
        f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/platform/v3/champion-rotations"
    )
    if not isinstance(dados, dict):
        dados = {}
    gratis = await _ids_de_campeoes(dados.get("freeChampionIds") or dados.get("sr"))
    novatos = await _ids_de_campeoes(dados.get("freeChampionIdsForNewPlayers") or dados.get("newplayer")
                                     or dados.get("newPlayer"))
    if not gratis and not novatos:
        # Formato desconhecido: tenta qualquer lista que tenha campeões.
        for chave, valor in dados.items():
            ids = await _ids_de_campeoes(valor)
            if ids:
                log.info("Rotação: usando o campo '%s' da resposta da Riot.", chave)
                gratis = ids
                break
    if not gratis and not novatos:
        log.warning("Rotação: a Riot respondeu sem campeões: %s", str(dados)[:500])
    nivel = dados.get("maxNewPlayerLevel") or dados.get("newPlayerMaxLevel") or 10
    return gratis, novatos, int(nivel) if str(nivel).isdigit() else 10


async def montar_rotacao() -> discord.Embed:
    nomes = await nomes_campeoes()
    ids_gratis, ids_novatos, nivel = await ler_rotacao()
    gratis = sorted(ids_gratis, key=lambda i: nomes.get(i, ""))
    novatos = sorted(ids_novatos, key=lambda i: nomes.get(i, ""))
    embed = discord.Embed(title="🔄 Rotação grátis da semana", color=0x0AC8B9,
                          description=" ".join(await _icones_ou_nomes(gratis))[:4000]
                          or "⚠️ A API oficial da Riot não está informando a rotação desta semana.")
    if novatos:
        embed.add_field(
            name=f"🌱 Para novos jogadores (até o nível {nivel})",
            value=" ".join(await _icones_ou_nomes(novatos))[:1024], inline=False,
        )
    embed.set_footer(text="Dados oficiais da Riot Games")
    return embed


@bot.tree.command(name="rotacao", description="Campeões grátis desta semana")
async def rotacao(inter: discord.Interaction) -> None:
    await inter.response.defer(thinking=True)
    try:
        await inter.followup.send(embed=await montar_rotacao())
    except (ErroRiot, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        await inter.followup.send(f"⚠️ Não consegui buscar a rotação agora. {exc if isinstance(exc, ErroRiot) else ''}")


async def checar_rotacao_nova() -> None:
    servidores = [g for g in bot.guilds if destino_aviso(g, "rotacao")]
    if not servidores:
        return
    gratis, _novatos, _nivel = await ler_rotacao()
    atual = sorted(gratis)
    if not atual:
        return  # Riot sem dados: não manda aviso vazio.
    estado = avisos.setdefault("_estado", {})
    if estado.get("rotacao") is None:
        estado["rotacao"] = atual  # Primeira vez: só avisa quando mudar (toda terça).
        _salvar_json(ARQUIVO_AVISOS, avisos)
        return
    if estado["rotacao"] == atual:
        return
    embed = await montar_rotacao()
    for guild in servidores:
        await enviar_aviso(guild, "rotacao", embeds=[embed], data=time.strftime("%d/%m"))
    estado["rotacao"] = atual
    _salvar_json(ARQUIVO_AVISOS, avisos)


# =====================================================================
# ============== STATUS DOS SERVIDORES (lol-status-v4, oficial) =========
# =====================================================================
def _traduzir(itens: list[dict[str, Any]], campo: str = "content") -> str:
    for preferido in ("pt_BR", "pt-BR", "en_US", "en-US"):
        for item in itens or []:
            if str(item.get("locale", "")).replace("-", "_") == preferido.replace("-", "_"):
                return str(item.get(campo, ""))
    return str((itens or [{}])[0].get(campo, "")) if itens else ""


def _embed_ocorrencia(ocorrencia: dict[str, Any], tipo: str, resolvido: bool = False) -> discord.Embed:
    titulo = _traduzir(ocorrencia.get("titles", [])) or ("Manutenção" if tipo == "maintenances" else "Problema")
    atualizacoes = ocorrencia.get("updates") or []
    texto = _traduzir(atualizacoes[0].get("translations", [])) if atualizacoes else ""
    if resolvido:
        emoji, cor = "✅", 0x4ADE80
    elif tipo == "maintenances":
        emoji, cor = "🛠️", 0xF5C542
    else:
        gravidade = str(ocorrencia.get("incident_severity") or "").lower()
        emoji, cor = ("🔴", 0xE84057) if gravidade == "critical" else ("⚠️", 0xF59E0B)
    embed = discord.Embed(
        title=f"{emoji} {'Resolvido: ' if resolvido else ''}{titulo}"[:256],
        description=(texto or "Sem detalhes.")[:4000], color=cor,
    )
    plataformas = ", ".join(ocorrencia.get("platforms") or [])
    embed.set_footer(text=f"Servidor {RIOT_PLATAFORMA.upper()}" + (f" • {plataformas}" if plataformas else ""))
    return embed


async def _status_bruto() -> dict[str, Any]:
    return await chamar_riot(f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/status/v4/platform-data")


async def montar_status_atual() -> discord.Embed:
    dados = await _status_bruto()
    ocorrencias = [(o, "maintenances") for o in dados.get("maintenances", [])] + \
                  [(o, "incidents") for o in dados.get("incidents", [])]
    if not ocorrencias:
        return discord.Embed(title=f"🟢 Servidor {dados.get('name', RIOT_PLATAFORMA.upper())} funcionando normalmente",
                             color=0x4ADE80)
    embed = _embed_ocorrencia(*ocorrencias[0])
    for o, t in ocorrencias[1:4]:
        extra = _embed_ocorrencia(o, t)
        embed.add_field(name=extra.title or "—", value=(extra.description or "—")[:1024], inline=False)
    return embed


@bot.tree.command(name="status_lol", description="Mostra se o servidor BR do LoL está com problema ou em manutenção")
async def status_lol(inter: discord.Interaction) -> None:
    await inter.response.defer(thinking=True)
    try:
        await inter.followup.send(embed=await montar_status_atual())
    except (ErroRiot, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        await inter.followup.send(f"⚠️ Não consegui consultar agora. {exc if isinstance(exc, ErroRiot) else ''}")


async def checar_status_novo() -> None:
    servidores = [g for g in bot.guilds if destino_aviso(g, "status")]
    if not servidores:
        return
    dados = await _status_bruto()
    estado = avisos.setdefault("_estado", {})
    vistos: dict[str, Any] = estado.setdefault("status", {})  # id -> {"tipo", "atualizacoes", "ocorrencia"}
    atuais = {}
    for tipo in ("maintenances", "incidents"):
        for o in dados.get(tipo, []):
            atuais[str(o.get("id"))] = (o, tipo)
    embeds = []
    for oid, (o, tipo) in atuais.items():
        n_atualizacoes = len(o.get("updates") or [])
        anterior = vistos.get(oid)
        if anterior is None or anterior.get("atualizacoes", 0) < n_atualizacoes:
            embeds.append(_embed_ocorrencia(o, tipo))
        vistos[oid] = {"tipo": tipo, "atualizacoes": n_atualizacoes, "ocorrencia": o}
    for oid in [i for i in vistos if i not in atuais]:
        embeds.append(_embed_ocorrencia(vistos[oid].get("ocorrencia") or {}, vistos[oid].get("tipo", "incidents"),
                                        resolvido=True))
        vistos.pop(oid, None)
    if not estado.get("status_iniciado"):
        estado["status_iniciado"] = True  # Primeira vez: não repete o que já estava aberto.
        _salvar_json(ARQUIVO_AVISOS, avisos)
        return
    _salvar_json(ARQUIVO_AVISOS, avisos)
    for embed in embeds[:5]:
        for guild in servidores:
            await enviar_aviso(guild, "status", embeds=[embed], titulo=embed.title or "")


# =====================================================================
# =============================== /quiz ===============================
# =====================================================================
ARQUIVO_QUIZ = Path("/data/quiz.json")
quiz_pontos: dict[str, dict[str, int]] = _carregar_json(ARQUIVO_QUIZ, {})
QUIZ_SEGUNDOS = 30
_quiz_ativo: set[int] = set()
CLASSES_PT = {"Fighter": "Lutador", "Mage": "Mago", "Assassin": "Assassino", "Marksman": "Atirador",
              "Support": "Suporte", "Tank": "Tanque"}


async def gerar_pergunta() -> dict[str, Any]:
    versao = (await versoes_ddragon())[0]
    dados = (await campeoes_completos(versao)).get("data", {})
    campeoes = [c for c in dados.values() if c.get("spells")]
    certo = random.choice(campeoes)
    errados = random.sample([c for c in campeoes if c["id"] != certo["id"]], 3)
    base_img = f"https://ddragon.leagueoflegends.com/cdn/{versao}/img"
    tipo = random.choice(["titulo", "habilidade", "habilidade", "passiva", "tecla", "lore", "classe"])
    pergunta: dict[str, Any] = {"imagem": None}

    if tipo == "tecla":
        i = random.randrange(4)
        magia = certo["spells"][i]
        pergunta.update(texto=f"Em qual tecla fica **{magia['name']}** de **{certo['name']}**?",
                        opcoes=[("Q", None), ("W", None), ("E", None), ("R", None)], correta=i,
                        imagem=f"{base_img}/spell/{magia['image']['full']}")
        return pergunta
    if tipo == "classe":
        classe = CLASSES_PT.get((certo.get("tags") or ["Fighter"])[0], "Lutador")
        outras = random.sample([c for c in CLASSES_PT.values() if c != classe], 3)
        opcoes = [classe] + outras
        random.shuffle(opcoes)
        pergunta.update(texto=f"Qual é a classe principal de **{certo['name']}**?",
                        opcoes=[(o, None) for o in opcoes], correta=opcoes.index(classe),
                        imagem=f"{base_img}/champion/{certo['image']['full']}")
        return pergunta

    opcoes_c = [certo] + errados
    random.shuffle(opcoes_c)
    icones = await icones_campeoes({int(c["key"]) for c in opcoes_c})
    pergunta["opcoes"] = [(c["name"], icones.get(int(c["key"]))) for c in opcoes_c]
    pergunta["correta"] = opcoes_c.index(certo)
    if tipo == "titulo":
        pergunta["texto"] = f"Qual campeão é conhecido como **{certo.get('title', '').capitalize()}**?"
    elif tipo == "habilidade":
        magia = random.choice(certo["spells"])
        pergunta["texto"] = f"De qual campeão é a habilidade **{magia['name']}**?"
        pergunta["imagem"] = f"{base_img}/spell/{magia['image']['full']}"
    elif tipo == "passiva":
        passiva = certo.get("passive") or {}
        pergunta["texto"] = f"De qual campeão é a passiva **{passiva.get('name', '?')}**?"
        if passiva.get("image"):
            pergunta["imagem"] = f"{base_img}/passive/{passiva['image']['full']}"
    else:  # lore
        historia = re.sub(re.escape(certo["name"]), "█████", certo.get("blurb", ""), flags=re.I)
        pergunta["texto"] = f"De quem é esta história?\n> {historia[:600]}"
    return pergunta


class QuizView(discord.ui.View):
    def __init__(self, pergunta: dict[str, Any]) -> None:
        super().__init__(timeout=QUIZ_SEGUNDOS + 30)
        self.pergunta = pergunta
        self.respostas: dict[int, int] = {}
        self.ordem: list[int] = []
        for i, (rotulo, emoji) in enumerate(pergunta["opcoes"]):
            botao = discord.ui.Button(label=rotulo[:80], style=discord.ButtonStyle.secondary,
                                      emoji=discord.PartialEmoji.from_str(emoji) if emoji else None)
            botao.callback = self._responder(i)
            self.add_item(botao)

    def _responder(self, indice: int):
        async def callback(inter: discord.Interaction) -> None:
            if inter.user.id in self.respostas:
                await inter.response.send_message("Você já respondeu esta! 😉", ephemeral=True)
                return
            self.respostas[inter.user.id] = indice
            self.ordem.append(inter.user.id)
            await inter.response.send_message("✅ Resposta registrada! O resultado sai em instantes.", ephemeral=True)
        return callback

    def resultado(self, guild_id: int) -> str:
        certa = self.pergunta["correta"]
        acertaram = [u for u in self.ordem if self.respostas[u] == certa]
        pontos = quiz_pontos.setdefault(str(guild_id), {})
        for i, uid in enumerate(acertaram):
            pontos[str(uid)] = pontos.get(str(uid), 0) + (2 if i == 0 else 1)
        if acertaram:
            _salvar_json(ARQUIVO_QUIZ, quiz_pontos)
        rotulo = self.pergunta["opcoes"][certa][0]
        if not acertaram:
            return f"✅ Resposta: **{rotulo}**\n😶 Ninguém acertou dessa vez."
        quem = f"🥇 <@{acertaram[0]}> (+2)"
        if len(acertaram) > 1:
            quem += " • " + ", ".join(f"<@{u}>" for u in acertaram[1:15]) + " (+1)"
        return f"✅ Resposta: **{rotulo}**\n{quem}"

    def revelar(self) -> None:
        for i, item in enumerate(self.children):
            if isinstance(item, discord.ui.Button):
                item.disabled = True
                item.style = discord.ButtonStyle.success if i == self.pergunta["correta"] else discord.ButtonStyle.secondary


async def iniciar_quiz(canal: discord.abc.Messageable, mensagem: str | None = None) -> bool:
    canal_id = getattr(canal, "id", 0)
    if canal_id in _quiz_ativo:
        return False
    _quiz_ativo.add(canal_id)
    try:
        pergunta = await gerar_pergunta()
        embed = discord.Embed(title="🃏 Quiz de LoL", description=pergunta["texto"], color=0xC8AA6E)
        if pergunta.get("imagem"):
            embed.set_thumbnail(url=pergunta["imagem"])
        fim = int(time.time()) + QUIZ_SEGUNDOS
        embed.add_field(name="⏱️ Tempo", value=f"Termina <t:{fim}:R> • 1º a acertar ganha 2 pontos, os outros 1")
        view = QuizView(pergunta)
        msg = await canal.send(content=mensagem, embed=embed, view=view, allowed_mentions=MENCOES_AVISO)
    except Exception:
        _quiz_ativo.discard(canal_id)
        raise

    async def encerrar() -> None:
        try:
            await asyncio.sleep(QUIZ_SEGUNDOS)
            guild_id = getattr(getattr(canal, "guild", None), "id", 0)
            texto = view.resultado(guild_id)
            view.revelar()
            embed.set_field_at(0, name="🏁 Resultado", value=texto[:1024], inline=False)
            await msg.edit(embed=embed, view=view)
            view.stop()
        except discord.HTTPException as exc:
            log.warning("Quiz: não consegui encerrar: %s", exc)
        finally:
            _quiz_ativo.discard(canal_id)

    asyncio.create_task(encerrar())
    return True


@bot.tree.command(name="quiz", description="Uma pergunta de LoL valendo pontos no ranking do servidor")
@app_commands.guild_only()
async def quiz(inter: discord.Interaction) -> None:
    if inter.channel_id in _quiz_ativo:
        await inter.response.send_message("⏳ Já tem um quiz rolando neste canal!", ephemeral=True)
        return
    await inter.response.send_message("🃏 Preparando a pergunta...", ephemeral=True)
    try:
        await iniciar_quiz(inter.channel)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, discord.HTTPException) as exc:
        log.warning("Quiz: falhou: %s", exc)
        await inter.followup.send("⚠️ Não consegui montar a pergunta agora.", ephemeral=True)


@bot.tree.command(name="quiz_ranking", description="Ranking de pontos do quiz no servidor")
@app_commands.guild_only()
async def quiz_ranking(inter: discord.Interaction) -> None:
    pontos = quiz_pontos.get(str(inter.guild_id), {})
    if not pontos:
        await inter.response.send_message("Ninguém pontuou ainda. Use `/quiz`!", ephemeral=True)
        return
    medalhas = ["🥇", "🥈", "🥉"]
    linhas = [f"{medalhas[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — **{p}** pts"
              for i, (uid, p) in enumerate(sorted(pontos.items(), key=lambda x: -x[1])[:15])]
    await inter.response.send_message(
        embed=discord.Embed(title="🃏 Ranking do Quiz", description="\n".join(linhas), color=0xC8AA6E),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def checar_quiz_automatico() -> None:
    agora = time.time()
    for guild in bot.guilds:
        cfg = config_aviso(guild.id, "quiz")
        destino = destino_aviso(guild, "quiz")
        if not cfg or destino is None or agora < float(cfg.get("proximo") or 0):
            continue
        cfg["proximo"] = agora + int(cfg.get("intervalo") or 6) * 3600
        _salvar_json(ARQUIVO_AVISOS, avisos)
        try:
            await iniciar_quiz(destino, mensagem_aviso(guild.id, "quiz"))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, discord.HTTPException) as exc:
            log.warning("Quiz automático: %s", exc)


# =====================================================================
# ============ HISTÓRICO DAS PARTIDAS INTERNAS + REVANCHE ===============
# =====================================================================
# Todo vencedor registrado (pelo app, pelos botões ou pelo /partida_finalizar)
# entra no histórico, que alimenta o /perfil. O anúncio ganha o botão Revanche.
ARQUIVO_HISTORICO = Path("/data/historico_interno.json")
historico_interno: list[dict[str, Any]] = _carregar_json(ARQUIVO_HISTORICO, [])
_encerrar_partida_original = encerrar_partida


async def encerrar_partida(guild: discord.Guild, chave_partida: str, vencedor: str | None) -> str:  # noqa: F811
    """Mesma função do bot, mas guarda o resultado no histórico interno."""
    partida = partidas.get(chave_partida)
    mensagem = await _encerrar_partida_original(guild, chave_partida, vencedor)
    if partida and partida.get("vencedor") and not partida.get("_registro_id"):
        registro_id = int(time.time() * 1000)
        historico_interno.append({
            "id": registro_id, "guild": guild.id, "data": int(time.time()),
            "numero": partida.get("numero", 1), "vencedor": partida["vencedor"],
            "times": {uid: c.get("time") for uid, c in (partida.get("participantes") or {}).items()},
            "origens": {uid: c.get("origem_id") for uid, c in (partida.get("participantes") or {}).items()},
            "origem_id": partida.get("origem_id"),
            "equipes": list((partida.get("canais") or {}).keys()) or ["vermelho", "azul"],
            "canal_aviso": partida.get("assistir_channel_id"),
        })
        del historico_interno[:-5000]
        partida["_registro_id"] = registro_id
        if chave_partida in partidas:
            salvar_partidas()
        try:
            _salvar_json(ARQUIVO_HISTORICO, historico_interno)
        except OSError as exc:
            log.warning("Não consegui salvar o histórico interno: %s", exc)
    return mensagem


async def criar_partida_interna(
    guild: discord.Guild, times: dict[str, list[discord.Member]], origens: dict[str, int],
    origem_padrao: int | None, canal_aviso_id: int | None,
) -> tuple[str, dict[str, Any], int]:
    """Cria categoria e canais de voz privados (igual à /queue) e move quem está em call."""
    eu = guild.me
    chave = f"revanche:{int(time.time() * 1000)}"
    numero = numero_livre_partida(guild.id)
    partida: dict[str, Any] = {
        "guild_id": guild.id, "numero": numero, "origem_id": origem_padrao, "categoria_id": None,
        "canais": {}, "vencedor": None, "espectadores": {}, "assistir_channel_id": canal_aviso_id,
        "assistir_message_id": None,
        "participantes": {str(m.id): {"origem_id": origens.get(str(m.id), origem_padrao), "time": t}
                          for t, membros in times.items() for m in membros},
    }
    partidas[chave] = partida
    salvar_partidas()
    try:
        categoria = await guild.create_category(f"🔒 PARTIDA {numero} EM ANDAMENTO", reason="Revanche")
        partida["categoria_id"] = categoria.id
        salvar_partidas()
        for chave_time, emoji, nome in EQUIPES:
            if chave_time not in times:
                continue
            canal = await guild.create_voice_channel(
                f"{emoji} Time {nome} 🔒", category=categoria,
                overwrites=permissoes_de_time(guild, eu, times[chave_time]), user_limit=0,
                reason=f"Revanche: canal privado do Time {nome}",
            )
            partida["canais"][chave_time] = canal.id
            salvar_partidas()
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.exception("Revanche: falha ao criar canais: %s", exc)
        await remover_estrutura(guild, partida)
        partidas.pop(chave, None)
        salvar_partidas()
        raise
    movidos = 0
    for chave_time, membros in times.items():
        destino = buscar_voz(guild, partida["canais"].get(chave_time))
        for membro in membros:
            if membro.voice and membro.voice.channel and destino:
                try:
                    await membro.move_to(destino, reason="Revanche")
                    movidos += 1
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Revanche: não consegui mover %s: %s", membro.id, exc)
    return chave, partida, movidos


class BotaoRevanche(discord.ui.DynamicItem[discord.ui.Button], template=r"revanche:(?P<id>\d+)"):
    def __init__(self, registro_id: int) -> None:
        super().__init__(discord.ui.Button(label="Revanche", emoji="🔁", style=discord.ButtonStyle.primary,
                                           custom_id=f"revanche:{registro_id}"))
        self.registro_id = registro_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, inter: discord.Interaction) -> None:
        guild = inter.guild
        registro = next((r for r in historico_interno if r.get("id") == self.registro_id), None)
        if guild is None or registro is None or registro.get("guild") != guild.id:
            await inter.response.send_message("ℹ️ Não encontrei essa partida.", ephemeral=True)
            return
        participante = str(inter.user.id) in registro["times"]
        if not (participante or autorizado(inter) or await pode_gerenciar_contas(inter)):
            await inter.response.send_message("⛔ Só quem jogou a partida ou a staff pode pedir revanche.", ephemeral=True)
            return
        if registro.get("revanche_usada"):
            await inter.response.send_message("🔁 A revanche desta partida já foi criada.", ephemeral=True)
            return
        await inter.response.defer()
        async with trava_servidor(guild.id):
            ocupados = {uid for _c, p in partidas_do_servidor(guild.id) for uid in (p.get("participantes") or {})}
            em_jogo = [uid for uid in registro["times"] if uid in ocupados]
            if em_jogo:
                await inter.followup.send(
                    "⚠️ " + ", ".join(f"<@{u}>" for u in em_jogo[:10]) + " já está em outra partida.",
                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return
            times: dict[str, list[discord.Member]] = {}
            origens: dict[str, int] = {}
            for uid, time_chave in registro["times"].items():
                membro = guild.get_member(int(uid))
                if membro is None or not time_chave:
                    continue
                times.setdefault(time_chave, []).append(membro)
                atual = membro.voice.channel.id if membro.voice and membro.voice.channel else None
                origens[uid] = atual or registro["origens"].get(uid) or registro.get("origem_id")
            if not times:
                await inter.followup.send("⚠️ Nenhum jogador da partida está no servidor.", ephemeral=True)
                return
            registro["revanche_usada"] = True
            _salvar_json(ARQUIVO_HISTORICO, historico_interno)
            try:
                _chave, partida, movidos = await criar_partida_interna(
                    guild, times, origens, registro.get("origem_id"), inter.channel_id)
            except (discord.Forbidden, discord.HTTPException):
                registro["revanche_usada"] = False
                _salvar_json(ARQUIVO_HISTORICO, historico_interno)
                await inter.followup.send("❌ Não consegui criar os canais. Confira as permissões do bot.")
                return
        try:
            await inter.message.edit(view=None)
        except (discord.HTTPException, AttributeError):
            pass
        linhas = [f"🔁 **Revanche!** Partida {partida['numero']} criada com os mesmos times."]
        for chave_time, emoji, nome in EQUIPES:
            if chave_time in times:
                linhas.append(f"{emoji} **{nome}:** " + ", ".join(m.mention for m in times[chave_time]))
        linhas.append(f"↪️ Pessoas movidas: **{movidos}**.")
        await inter.followup.send("\n".join(linhas), allowed_mentions=discord.AllowedMentions.none())


def view_revanche(partida: dict[str, Any]) -> discord.ui.View | None:
    registro_id = partida.get("_registro_id")
    if not registro_id:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(BotaoRevanche(int(registro_id)))
    return view


# =====================================================================
# ======================== /perfil (cartão em imagem) ===================
# =====================================================================
# Precisa do Pillow (requirements.txt) e das fontes da pasta "fonts" ao lado do bot.
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # O resto do bot funciona sem o Pillow.
    Image = None  # type: ignore[assignment]

PASTA_FONTES = Path(__file__).resolve().parent / "fonts"
CORES_TIER = {
    "IRON": "#7A6E6A", "BRONZE": "#A0694A", "SILVER": "#9FB0BC", "GOLD": "#E0B95B",
    "PLATINUM": "#4FC3B0", "EMERALD": "#34C77B", "DIAMOND": "#7A8CF0", "MASTER": "#B266E0",
    "GRANDMASTER": "#E05555", "CHALLENGER": "#F4D27A",
}


import functools
import math


@functools.lru_cache(maxsize=128)
def _fonte(tamanho: int, negrito: bool = False):
    arquivo = PASTA_FONTES / ("DejaVuSans-Bold.ttf" if negrito else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(str(arquivo), tamanho)
    except OSError:
        return ImageFont.load_default(size=tamanho)


def estatisticas_internas(guild_id: int, uid: int) -> dict[str, Any]:
    jogos = [r for r in historico_interno if r.get("guild") == guild_id and str(uid) in r.get("times", {})]
    jogos.sort(key=lambda r: r.get("data", 0))
    vitorias = derrotas = seq = melhor = 0
    parceiros: dict[str, int] = {}
    for r in jogos:
        meu_time = r["times"][str(uid)]
        venceu = r.get("vencedor") == meu_time
        if venceu:
            vitorias += 1
            seq = seq + 1 if seq >= 0 else 1
            for outro, t in r["times"].items():
                if outro != str(uid) and t == meu_time:
                    parceiros[outro] = parceiros.get(outro, 0) + 1
        else:
            derrotas += 1
            seq = seq - 1 if seq <= 0 else -1
        melhor = max(melhor, seq)
    return {"vitorias": vitorias, "derrotas": derrotas, "jogos": len(jogos), "sequencia": seq,
            "melhor_sequencia": melhor, "parceiro": max(parceiros.items(), key=lambda x: x[1]) if parceiros else None}


def medalhas_do_membro(guild_id: int, uid: int, est: dict[str, Any], pontos_main: int) -> list[str]:
    medalhas = []
    if est["vitorias"] >= 1:
        medalhas.append("Primeira vitória")
    for meta in (50, 25, 10):
        if est["vitorias"] >= meta:
            medalhas.append(f"{meta} vitórias")
            break
    if est["sequencia"] >= 3:
        medalhas.append(f"Em chamas ({est['sequencia']} seguidas)")
    elif est["melhor_sequencia"] >= 5:
        medalhas.append(f"Recorde: {est['melhor_sequencia']} seguidas")
    if est["jogos"] >= 30:
        medalhas.append("Veterano")
    if est["parceiro"] and est["parceiro"][1] >= 5:
        medalhas.append("Dupla imbatível")
    vit_por_membro: dict[str, int] = {}
    for r in historico_interno:
        if r.get("guild") == guild_id:
            for outro, t in r["times"].items():
                if t == r.get("vencedor"):
                    vit_por_membro[outro] = vit_por_membro.get(outro, 0) + 1
    if est["vitorias"] >= 3 and vit_por_membro and max(vit_por_membro, key=vit_por_membro.get) == str(uid):
        medalhas.insert(0, "Rei do servidor")
    pontos_quiz = quiz_pontos.get(str(guild_id), {})
    if pontos_quiz.get(str(uid), 0) >= 5 and max(pontos_quiz, key=pontos_quiz.get) == str(uid):
        medalhas.append("Mestre do quiz")
    if pontos_main >= 500_000:
        medalhas.append("Fiel ao main")
    return medalhas[:6]


# Temas do cartão: combinações fofas (o primeiro é o padrão).
TEMAS_CARTAO: dict[str, dict[str, str]] = {
    # ---- Fofos (rosa) ----
    "rosa_lilas":    {"nome": "🌸 Rosa & Lilás", "acento": "#FF9EC4", "segunda": "#C9A7FF", "escuro": "#24122E",
                      "texto": "#FFF4FA", "suave": "#E3C3DA", "pilula": "#3A1D47", "estilo": "fofo"},
    "rosa_azul":     {"nome": "🩵 Rosa & Azul-bebê", "acento": "#FFA6CB", "segunda": "#A8D8FF", "escuro": "#161A33",
                      "texto": "#FFF6FB", "suave": "#D6CBE6", "pilula": "#26294A", "estilo": "fofo"},
    "rosa_menta":    {"nome": "🍃 Rosa & Menta", "acento": "#FF9EC4", "segunda": "#9EEFD0", "escuro": "#132528",
                      "texto": "#FFF6FA", "suave": "#CFE3DD", "pilula": "#1F3A3C", "estilo": "fofo"},
    "rosa_dourado":  {"nome": "✨ Rosa & Dourado", "acento": "#FFB1C8", "segunda": "#F2CC8F", "escuro": "#2A141F",
                      "texto": "#FFF6F0", "suave": "#E8CFC4", "pilula": "#3E1F2C", "estilo": "fofo"},
    "morango":       {"nome": "🍓 Morango com Creme", "acento": "#FF6F91", "segunda": "#FFD6E2", "escuro": "#330D1C",
                      "texto": "#FFF2F6", "suave": "#F3C4D2", "pilula": "#4A1428", "estilo": "fofo"},
    "algodao_doce":  {"nome": "🍭 Algodão-doce", "acento": "#FF8FD0", "segunda": "#8FD3FF", "escuro": "#221433",
                      "texto": "#FFF7FD", "suave": "#DCC8EC", "pilula": "#34204D", "estilo": "fofo"},
    # ---- Anime fofinho ----
    "sakura_kawaii": {"nome": "🌸 Anime: Sakura Kawaii", "acento": "#FFB7D5", "segunda": "#FFE4EF", "escuro": "#3A1E33",
                      "texto": "#FFF7FB", "suave": "#F1CFE0", "pilula": "#52284A", "estilo": "anime"},
    "garota_magica": {"nome": "🪄 Anime: Garota Mágica", "acento": "#FF7EC8", "segunda": "#B5F2FF", "escuro": "#2A1640",
                      "texto": "#FFF5FD", "suave": "#DCCBF0", "pilula": "#3D2160", "estilo": "anime"},
    "chibi_pastel":  {"nome": "🍡 Anime: Chibi Pastel", "acento": "#FFC4E1", "segunda": "#C4F0D8", "escuro": "#2E2438",
                      "texto": "#FFF8FC", "suave": "#E6D6E8", "pilula": "#463657", "estilo": "anime"},
    # ---- Cyberpunk ----
    "neon_kawaii":   {"nome": "💗 Cyberpunk Kawaii", "acento": "#FF3DB8", "segunda": "#2DF6FF", "escuro": "#0B0620",
                      "texto": "#FFF0FB", "suave": "#C9B6E8", "pilula": "#1E0F3D", "estilo": "cyber"},
    "neon_city":     {"nome": "🌃 Cyberpunk Neon", "acento": "#F5F03A", "segunda": "#FF2E88", "escuro": "#0A0A18",
                      "texto": "#FFFDF0", "suave": "#C9C3D8", "pilula": "#1A1A33", "estilo": "cyber"},
    # ---- Gótico ----
    "gotico_lolita": {"nome": "🖤 Gótico Lolita", "acento": "#E8B7D4", "segunda": "#C2275A", "escuro": "#120A10",
                      "texto": "#FBEFF5", "suave": "#CBB3C2", "pilula": "#2A1420", "estilo": "gotico"},
    "vampira":       {"nome": "🦇 Vampira Fofa", "acento": "#FF4D6D", "segunda": "#E6E1F0", "escuro": "#14060B",
                      "texto": "#FFF0F3", "suave": "#D8B9C2", "pilula": "#2E0E18", "estilo": "gotico"},
    # ---- Dark ----
    "noite_estrelada": {"nome": "🌙 Dark: Noite Estrelada", "acento": "#B9A6FF", "segunda": "#FFE8A3", "escuro": "#070A1A",
                      "texto": "#F4F1FF", "suave": "#B7B3D6", "pilula": "#151A36", "estilo": "dark"},
    "abismo":        {"nome": "🌌 Dark: Abismo", "acento": "#8C7CF0", "segunda": "#6EE7F2", "escuro": "#05050A",
                      "texto": "#EEF0FF", "suave": "#A3A6C2", "pilula": "#12122A", "estilo": "dark"},
    # ---- Terror ----
    "terror_fofo":   {"nome": "👻 Terror Fofo", "acento": "#FF6B8B", "segunda": "#B8F5A0", "escuro": "#100A0E",
                      "texto": "#FFF2F5", "suave": "#D6C1C8", "pilula": "#2A1219", "estilo": "terror"},
    "halloween":     {"nome": "🎃 Halloween Doce", "acento": "#FF9F45", "segunda": "#C59BFF", "escuro": "#160B1E",
                      "texto": "#FFF6EC", "suave": "#DCC6D9", "pilula": "#2E1838", "estilo": "terror"},
    # ---- Futurista ----
    "holograma":     {"nome": "🔮 Futurista: Holograma", "acento": "#9AF7FF", "segunda": "#F7A8FF", "escuro": "#0A1024",
                      "texto": "#F0FDFF", "suave": "#B6CBE0", "pilula": "#13213F", "estilo": "futuro"},
    "cromado":       {"nome": "🛸 Futurista: Cromo", "acento": "#D8E3F0", "segunda": "#7DF9C6", "escuro": "#0C1118",
                      "texto": "#FFFFFF", "suave": "#A9B6C4", "pilula": "#1B2430", "estilo": "futuro"},
    # ---- Clássico ----
    "classico":      {"nome": "🏆 Clássico do LoL", "acento": "#C8AA6E", "segunda": "#0AC8B9", "escuro": "#0A0E13",
                      "texto": "#F0E6D2", "suave": "#A09B8C", "pilula": "#1A2029", "estilo": "classico"},
}
SIMBOLOS_ESTILO = {"fofo": "♥", "anime": "✿", "cyber": "◆", "gotico": "✝", "dark": "☾",
                   "terror": "☠", "futuro": "◈", "classico": "★"}
TEMA_PADRAO = "rosa_lilas"


def _rgb(hexa: str) -> tuple[int, int, int]:
    hexa = hexa.lstrip("#")
    return tuple(int(hexa[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _rgba(hexa: str, alfa: int) -> tuple[int, int, int, int]:
    return (*_rgb(hexa), max(0, min(255, int(alfa))))


def _misturar(a: str, b: str, t: float) -> tuple[int, int, int]:
    ra, rb = _rgb(a), _rgb(b)
    return tuple(int(x + (y - x) * t) for x, y in zip(ra, rb))  # type: ignore[return-value]


def _cobrir(arte: "Image.Image", largura: int, altura: int) -> "Image.Image":
    """Corta uma imagem para preencher o tamanho (sem distorcer)."""
    arte = arte.convert("RGB")
    escala = max(largura / arte.width, altura / arte.height)
    arte = arte.resize((max(1, int(arte.width * escala)), max(1, int(arte.height * escala))))
    x = (arte.width - largura) // 2
    y = max(0, (arte.height - altura) // 3)
    return arte.crop((x, y, x + largura, y + altura))


def _encaixar(dados: bytes, largura: int, altura: int) -> "Image.Image":
    arte = Image.open(io.BytesIO(dados))
    arte.seek(0)
    return _cobrir(arte, largura, altura)


def _quadros_gif(dados: bytes, largura: int, altura: int, maximo: int = 24) -> tuple[list["Image.Image"], int]:
    """Quadros de um GIF já recortados (no máx. `maximo`, pulando quadros se precisar) + duração média."""
    arte = Image.open(io.BytesIO(dados))
    total = getattr(arte, "n_frames", 1)
    passo = max(1, -(-total // maximo))
    quadros, duracoes = [], []
    for i in range(0, total, passo):
        arte.seek(i)
        quadros.append(_cobrir(arte.convert("RGBA"), largura, altura))
        duracoes.append(int(arte.info.get("duration", 80) or 80) * passo)
    return quadros, max(40, min(150, sum(duracoes) // max(1, len(duracoes))))


def _veu(fundo: "Image.Image", tema: dict[str, str]) -> "Image.Image":
    """Escurece à esquerda (texto) e dá um brilho do tema na base."""
    L, A = fundo.size
    veu = Image.new("L", (L, 1))
    for x in range(L):
        veu.putpixel((x, 0), int(238 - 165 * (x / L) ** 1.5))
    fundo = Image.composite(Image.new("RGB", (L, A), tema["escuro"]), fundo.convert("RGB"), veu.resize((L, A)))
    luz = Image.new("L", (1, A))
    for y in range(A):
        luz.putpixel((0, y), int(55 * max(0.0, (y - A * 0.55) / (A * 0.45))))
    return Image.composite(Image.new("RGB", (L, A), tema["acento"]), fundo, luz.resize((L, A)))


def _fundo_cores(tema: dict[str, str], L: int, A: int) -> "Image.Image":
    cantos = Image.new("RGB", (2, 2))
    cantos.putpixel((0, 0), _rgb(tema["escuro"]))
    cantos.putpixel((0, 1), _misturar(tema["escuro"], tema["acento"], 0.25))
    cantos.putpixel((1, 0), _misturar(tema["escuro"], tema["segunda"], 0.55))
    cantos.putpixel((1, 1), _misturar(tema["escuro"], tema["acento"], 0.7))
    return cantos.resize((L, A), Image.BILINEAR)


def fundos_do_cartao(d: dict[str, Any], tema: dict[str, str], L: int, A: int,
                     animado: bool = False) -> tuple[list["Image.Image"], int, bool]:
    """(quadros do fundo já com véu, duração, só_cores). GIF só quando animado=True."""
    if d.get("fundo_tipo") != "cores":
        if animado and d.get("fundo_gif"):
            try:
                quadros, duracao = _quadros_gif(d["fundo_gif"], L, A)
                return [_veu(q, tema) for q in quadros], duracao, False
            except OSError:
                pass
        arte = d.get("fundo_imagem") or d.get("splash")
        if arte:
            try:
                return [_veu(_encaixar(arte, L, A), tema)], 80, False
            except OSError:
                pass
    return [_veu(_fundo_cores(tema, L, A), tema)], 80, True


def camada_enfeites(tema: dict[str, str], so_cores: bool, semente: int, L: int, A: int) -> "Image.Image":
    """Enfeites fixos de cada estilo, numa camada transparente."""
    camada = Image.new("RGBA", (L, A), (0, 0, 0, 0))
    estilo = tema.get("estilo", "fofo")
    if estilo == "classico":
        return camada
    g = ImageDraw.Draw(camada)
    acento, segunda = tema["acento"], tema["segunda"]
    sorte = random.Random(semente)
    forte = 1.0 if so_cores else 0.75
    zona = (int(L * 0.70), 34, L - 46, A - 118)

    def espalhar(simbolos: list[str], quantos: int, tamanhos: tuple[int, ...] = (14, 18, 22, 28)) -> None:
        for i in range(quantos):
            x, y = sorte.randint(zona[0], zona[2]), sorte.randint(zona[1], zona[3])
            g.text((x, y), sorte.choice(simbolos), font=_fonte(sorte.choice(tamanhos), True),
                   fill=_rgba(acento if i % 2 else segunda, sorte.randint(150, 235) * forte), anchor="mm")

    if estilo == "fofo":
        espalhar(["♥", "♡", "✦", "✧", "⋆"], 10 if so_cores else 6)
    elif estilo == "anime":
        for _ in range(16 if so_cores else 10):
            x, y = sorte.randint(int(L * 0.72), L - 40), sorte.randint(24, A - 120)
            w, h = sorte.randint(10, 18), sorte.randint(6, 10)
            petala = Image.new("RGBA", (w * 2, w * 2), (0, 0, 0, 0))
            ImageDraw.Draw(petala).ellipse((w // 2, w - h // 2, w // 2 + w, w + h // 2), fill=_rgba(acento, 200 * forte))
            camada.alpha_composite(petala.rotate(sorte.randint(0, 180)), (x, y))
        espalhar(["✿", "❀", "✦", "♡"], 8 if so_cores else 5)
        g.text((L - 70, 56), "✿", font=_fonte(40, True), fill=_rgba(segunda, 230), anchor="mm")
    elif estilo == "cyber":
        horizonte, base = int(A * 0.62), A - 10
        for i in range(-3, 9):
            g.line((L * 0.84 + i * 14, horizonte, L * 0.84 + i * 60, base), fill=_rgba(segunda, 70), width=1)
        for k in range(7):
            yy = horizonte + (base - horizonte) * (k / 6) ** 1.8
            g.line((L * 0.70, yy, L - 12, yy), fill=_rgba(acento, 80), width=1)
        for yy in range(0, A, 4):
            g.line((0, yy, L, yy), fill=(0, 0, 0, 26))
        espalhar(["◆", "◇", "⚡", "✚"], 6, (12, 16, 20))
        g.rectangle((L - 160, 30, L - 40, 34), fill=_rgba(acento, 220))
        g.rectangle((L - 120, 40, L - 40, 43), fill=_rgba(segunda, 220))
    elif estilo == "gotico":
        for (x, y) in ((L - 44, 44), (L - 44, A // 2 - 20)):
            g.text((x, y), "✝", font=_fonte(30, True), fill=_rgba(acento, 170), anchor="mm")
        g.text((L - 110, 64), "☾", font=_fonte(54, True), fill=_rgba(segunda, 210), anchor="mm")
        espalhar(["✝", "♰", "✦", "♡"], 7 if so_cores else 4, (14, 18, 22))
        for i in range(0, L, 22):
            g.ellipse((i, A - 22, i + 22, A + 2), outline=_rgba(acento, 90), width=2)
    elif estilo == "dark":
        for _ in range(70 if so_cores else 45):
            x, y = sorte.randint(int(L * 0.45), L - 20), sorte.randint(16, A - 100)
            r = sorte.choice([1, 1, 1, 2])
            g.ellipse((x - r, y - r, x + r, y + r), fill=_rgba("#FFFFFF", sorte.randint(90, 230)))
        g.text((L - 100, 78), "☾", font=_fonte(70, True), fill=_rgba(segunda, 225), anchor="mm")
        espalhar(["✦", "⋆", "✧"], 7, (12, 16, 20))
    elif estilo == "terror":
        for x in range(30, L - 30, 34):
            comp = sorte.randint(6, 26)
            g.rounded_rectangle((x, 8, x + 9, 8 + comp), radius=4, fill=_rgba(acento, 200))
            g.ellipse((x - 2, 4 + comp, x + 11, 17 + comp), fill=_rgba(acento, 200))
        espalhar(["☠", "♡", "✦", "☁"], 7 if so_cores else 5, (16, 20, 26))
        g.text((L - 90, A - 150), "☠", font=_fonte(48, True), fill=_rgba(segunda, 190), anchor="mm")
    elif estilo == "futuro":
        c = _rgba(acento, 210)
        for (x, y, dx, dy) in ((24, 24, 1, 1), (L - 24, 24, -1, 1), (24, A - 24, 1, -1), (L - 24, A - 24, -1, -1)):
            g.line((x, y, x + 46 * dx, y), fill=c, width=3)
            g.line((x, y, x, y + 46 * dy), fill=c, width=3)
        for _ in range(9 if so_cores else 6):
            x, y = sorte.randint(int(L * 0.66), L - 80), sorte.randint(50, A - 140)
            x2 = x + sorte.randint(30, 70)
            g.line((x, y, x2, y), fill=_rgba(segunda, 150), width=2)
            g.line((x2, y, x2 + 14, y + 14), fill=_rgba(segunda, 150), width=2)
            g.ellipse((x - 4, y - 4, x + 4, y + 4), outline=_rgba(acento, 200), width=2)
        for yy in range(0, A, 6):
            g.line((int(L * 0.5), yy, L, yy), fill=_rgba(acento, 10))
        espalhar(["◈", "⬡", "◇"], 5, (14, 18))
    return camada


def camada_conteudo(d: dict[str, Any], tema: dict[str, str], L: int, A: int) -> "Image.Image":
    """Bordas, ícones, textos e medalhas numa camada transparente."""
    acento, segunda, texto, suave = tema["acento"], tema["segunda"], tema["texto"], tema["suave"]
    simbolo = SIMBOLOS_ESTILO.get(tema.get("estilo", "fofo"), "♥")
    camada = Image.new("RGBA", (L, A), (0, 0, 0, 0))
    g = ImageDraw.Draw(camada)
    g.rounded_rectangle((6, 6, L - 7, A - 7), radius=26, outline=acento, width=4)
    g.rounded_rectangle((14, 14, L - 15, A - 15), radius=20, outline=segunda, width=1)

    if d.get("icone"):
        try:
            icone = Image.open(io.BytesIO(d["icone"])).convert("RGBA").resize((124, 124))
            mascara = Image.new("L", (124, 124), 0)
            ImageDraw.Draw(mascara).ellipse((0, 0, 123, 123), fill=255)
            camada.paste(icone, (40, 40), mascara)
        except OSError:
            pass
    g.ellipse((34, 34, 170, 170), outline=segunda, width=2)
    g.ellipse((37, 37, 167, 167), outline=acento, width=4)
    if d.get("nivel"):
        rotulo = str(d["nivel"])
        f = _fonte(18, True)
        w = g.textlength(rotulo, font=f) + 24
        g.rounded_rectangle((102 - w / 2, 152, 102 + w / 2, 182), radius=15, fill=tema["pilula"], outline=acento, width=2)
        g.text((102, 167), rotulo, font=f, fill=texto, anchor="mm")

    nome = d["nome"][:22]
    f_nome = _fonte(42, True)
    if tema.get("estilo") == "cyber":
        g.text((193, 54), nome, font=f_nome, fill=segunda)
        g.text((199, 58), nome, font=f_nome, fill=acento)
    g.text((196, 56), nome, font=f_nome, fill=texto)
    fim_nome = 196 + g.textlength(nome, font=f_nome)
    g.text((fim_nome + 22, 70), simbolo, font=_fonte(26, True), fill=acento, anchor="mm")
    g.text((fim_nome + 46, 58), "✦", font=_fonte(16, True), fill=segunda, anchor="mm")
    g.text((198, 112), d["riot_id"][:32], font=_fonte(22), fill=suave)

    y = 212
    for rot, ranque in (("SOLO/DUO", d.get("solo")), ("FLEX", d.get("flex"))):
        g.text((40, y), rot, font=_fonte(16, True), fill=segunda)
        if ranque:
            g.text((40, y + 22), ranque["nome"], font=_fonte(28, True), fill=CORES_TIER.get(ranque["tier"], texto))
            g.text((40, y + 58), ranque["detalhe"], font=_fonte(18), fill=texto)
        else:
            g.text((40, y + 22), "Sem ranque", font=_fonte(26, True), fill=suave)
        y += 100

    x0 = 400
    g.text((x0, 212), "PRINCIPAL", font=_fonte(16, True), fill=segunda)
    if d.get("main_icone"):
        try:
            mi = Image.open(io.BytesIO(d["main_icone"])).convert("RGBA").resize((76, 76))
            mascara = Image.new("L", (76, 76), 0)
            ImageDraw.Draw(mascara).rounded_rectangle((0, 0, 75, 75), radius=16, fill=255)
            camada.paste(mi, (x0, 238), mascara)
            g.rounded_rectangle((x0 - 3, 235, x0 + 78, 316), radius=18, outline=acento, width=3)
        except OSError:
            pass
    if d.get("main_nome"):
        g.text((x0 + 94, 242), d["main_nome"][:18], font=_fonte(26, True), fill=texto)
        g.text((x0 + 94, 278), d["main_detalhe"], font=_fonte(17), fill=suave)

    est = d["internas"]
    g.text((x0, 334), "PERSONALIZADAS DO SERVIDOR", font=_fonte(16, True), fill=segunda)
    total = est["vitorias"] + est["derrotas"]
    wr = round(100 * est["vitorias"] / total) if total else 0
    g.text((x0, 356), f"{est['vitorias']}V {est['derrotas']}D", font=_fonte(28, True), fill=texto)
    seq = est["sequencia"]
    seq_txt = f"{seq} vitórias seguidas" if seq >= 2 else (f"{-seq} derrotas seguidas" if seq <= -2 else "")
    g.text((x0, 392), f"{wr}% de vitórias" + (f"  •  {seq_txt}" if seq_txt else ""), font=_fonte(18), fill=suave)

    outras = d.get("outras") or []
    if outras:
        g.text((40, A - 132), f"OUTRAS CONTAS ({len(outras)})", font=_fonte(14, True), fill=segunda)
        f_id, f_elo = _fonte(15, True), _fonte(14, True)

        def curto(rid: str, limite: int) -> str:
            return rid if len(rid) <= limite else rid[:limite - 1] + "…"

        def largura(rid: str, elo: str) -> float:
            return 28 + g.textlength(rid, font=f_id) + 12 + g.textlength(elo, font=f_elo) + 16

        # Encurta os Riot IDs até as contas caberem lado a lado.
        limite = 20
        while limite > 6 and sum(largura(curto(c["riot_id"], limite), c.get("elo") or "Sem ranque") + 8
                                 for c in outras[:4]) > L - 80:
            limite -= 1
        cx = 40
        for conta in outras[:4]:
            rid = curto(conta["riot_id"], limite)
            elo = conta.get("elo") or "Sem ranque"
            w = largura(rid, elo)
            if cx + w > L - 40:
                break
            g.rounded_rectangle((cx, A - 112, cx + w, A - 80), radius=16, fill=tema["pilula"], outline=segunda, width=1)
            g.ellipse((cx + 12, A - 100, cx + 20, A - 92), fill=CORES_TIER.get(conta.get("tier") or "", suave))
            g.text((cx + 28, A - 96), rid, font=f_id, fill=texto, anchor="lm")
            g.text((cx + 28 + g.textlength(rid, font=f_id) + 12, A - 96), elo, font=f_elo,
                   fill=CORES_TIER.get(conta.get("tier") or "", suave), anchor="lm")
            cx += w + 8

    x, y = 40, A - 64
    for medalha in d.get("medalhas", []):
        f = _fonte(17, True)
        w = g.textlength(medalha, font=f) + 46
        if x + w > L - 40:
            break
        g.rounded_rectangle((x, y, x + w, y + 36), radius=18, fill=tema["pilula"], outline=acento, width=2)
        g.text((x + 18, y + 18), simbolo, font=_fonte(14, True), fill=segunda, anchor="mm")
        g.text((x + 28, y + 18), medalha, font=f, fill=acento, anchor="lm")
        x += w + 10
    if not d.get("medalhas"):
        g.text((40, y + 8), f"{simbolo} Jogue personalizadas para ganhar medalhas!", font=_fonte(17), fill=suave)
    return camada


def altura_do_cartao(d: dict[str, Any]) -> int:
    return 560 if d.get("outras") else 500


def desenhar_cartao(d: dict[str, Any], rapido: bool = False) -> bytes:
    """Cartão estático (PNG)."""
    L, A = 1100, altura_do_cartao(d)
    tema = TEMAS_CARTAO.get(d.get("tema") or TEMA_PADRAO, TEMAS_CARTAO[TEMA_PADRAO])
    quadros, _dur, so_cores = fundos_do_cartao(d, tema, L, A)
    imagem = quadros[0].convert("RGBA")
    imagem.alpha_composite(camada_enfeites(tema, so_cores, sum(map(ord, d.get("nome", ""))), L, A))
    imagem.alpha_composite(camada_conteudo(d, tema, L, A))
    saida = io.BytesIO()
    if rapido:  # Prévia do painel: JPEG é bem mais leve para enviar (≈10x menor).
        imagem.convert("RGB").save(saida, "JPEG", quality=88)
    else:
        imagem.convert("RGB").save(saida, "PNG", optimize=True)
    return saida.getvalue()


# ======================= EFEITOS PREMIUM (GIF animado) =======================
EFEITOS_PREMIUM = {
    "estrelas": "✨ Brilho Estelar",
    "holografico": "🌈 Holográfico",
    "coracoes": "💖 Corações Flutuantes",
    "sakura": "🌸 Chuva de Sakura",
    "neon": "⚡ Neon Pulsante",
    "aurora": "🌌 Aurora Boreal",
    "neve": "❄️ Neve Mágica",
    "brasas": "🔥 Brasas Encantadas",
    "diamante": "💎 Diamante",
    "noite": "🌙 Noite Encantada",
}


PARTICULAS_ATRAS = {"estrelas", "coracoes", "sakura", "neve", "brasas", "noite"}


def _compor(destino: "Image.Image", fonte: "Image.Image", x: int, y: int) -> None:
    """alpha_composite que aceita posições fora da borda (recorta o que sobra)."""
    x, y = int(x), int(y)
    esq, topo = max(0, -x), max(0, -y)
    dir_ = min(fonte.width, destino.width - x)
    baixo = min(fonte.height, destino.height - y)
    if dir_ <= esq or baixo <= topo:
        return
    destino.alpha_composite(fonte.crop((esq, topo, dir_, baixo)), (max(0, x), max(0, y)))


def _petala(tamanho: int, cor: tuple[int, int, int, int]) -> "Image.Image":
    p = Image.new("RGBA", (tamanho * 2, tamanho * 2), (0, 0, 0, 0))
    ImageDraw.Draw(p).ellipse((tamanho // 2, tamanho - tamanho // 3, tamanho * 3 // 2, tamanho + tamanho // 3), fill=cor)
    return p


def _faixa_arco_iris(largura: int, altura: int, alfa_max: int, cores: list[str] | None = None) -> "Image.Image":
    import colorsys
    faixa = Image.new("RGBA", (largura, 1))
    for x in range(largura):
        perfil = math.sin(math.pi * x / (largura - 1))
        if cores:
            c = _rgb(cores[0])
        else:
            c = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(x / largura, 0.45, 1.0))
        faixa.putpixel((x, 0), (*c, int(alfa_max * perfil ** 2)))
    return faixa.resize((largura, altura))


def quadros_do_efeito(efeito: str, tema: dict[str, str], L: int, A: int, n: int,
                      semente: int) -> list[tuple["Image.Image | None", "Image.Image | None"]]:
    """Para cada quadro: (camada atrás do texto, camada na frente). Loop perfeito em n quadros."""
    sorte = random.Random(semente)
    acento, segunda = tema["acento"], tema["segunda"]
    saida: list[tuple[Any, Any]] = []
    vazio = lambda: Image.new("RGBA", (L, A), (0, 0, 0, 0))  # noqa: E731

    if efeito == "estrelas":
        pontos = [(sorte.randint(20, L - 20), sorte.randint(20, A - 20), sorte.choice([12, 16, 20, 26]),
                   sorte.random(), sorte.choice([acento, segunda, "#FFFFFF"])) for _ in range(38)]
        for i in range(n):
            t, frente = i / n, vazio()
            g = ImageDraw.Draw(frente)
            for x, y, tam, fase, cor in pontos:
                a = max(0.0, math.sin(2 * math.pi * (t + fase))) ** 2
                if a > 0.05:
                    r = int(tam * (0.25 + 0.25 * a))
                    g.ellipse((x - r * 2, y - r * 2, x + r * 2, y + r * 2), fill=_rgba(cor, 40 * a))
                    g.text((x, y), "✦", font=_fonte(int(tam * (0.6 + 0.5 * a)), True), fill=_rgba(cor, 255 * a), anchor="mm")
            saida.append((None, frente))
    elif efeito == "holografico":
        faixa = _faixa_arco_iris(320, A * 2, 120).rotate(18, expand=True)
        for i in range(n):
            t, frente = i / n, vazio()
            x = int(-faixa.width + (L + faixa.width * 2) * t)
            _compor(frente, faixa, x, -A // 2)
            # Borda furta-cor que muda de tom.
            import colorsys
            c = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(t, 0.4, 1.0))
            ImageDraw.Draw(frente).rounded_rectangle((6, 6, L - 7, A - 7), radius=26, outline=(*c, 200), width=4)
            saida.append((None, frente))
    elif efeito == "coracoes":
        cor = [(sorte.uniform(L * 0.30, L - 30), sorte.random(), sorte.choice([1, 1, 2]), sorte.choice(["♥", "♡", "♥"]),
                sorte.choice([16, 20, 26, 34]), sorte.choice([acento, segunda])) for _ in range(26)]
        for i in range(n):
            t, frente = i / n, vazio()
            g = ImageDraw.Draw(frente)
            for x0, fase, vel, simb, tam, c in cor:
                p = (t * vel + fase) % 1
                y = A + 30 - p * (A + 70)
                x = x0 + 14 * math.sin(2 * math.pi * (p * 2 + fase))
                alfa = 235 * min(1.0, p * 5) * min(1.0, (1 - p) * 3)
                g.text((x, y), simb, font=_fonte(tam, True), fill=_rgba(c, alfa), anchor="mm")
            saida.append((None, frente))
    elif efeito == "sakura":
        petalas = [(sorte.uniform(-150, L), sorte.random(), sorte.choice([1, 1, 2]), sorte.randint(9, 16),
                    sorte.choice([acento, segunda, "#FFFFFF"]), sorte.random()) for _ in range(34)]
        modelos = {(tam, c): _petala(tam, _rgba(c, 215)) for _x, _f, _v, tam, c, _g in petalas}
        for i in range(n):
            t, frente = i / n, vazio()
            for x0, fase, vel, tam, c, giro in petalas:
                p = (t * vel + fase) % 1
                y = int(p * (A + 60) - 30)
                x = int(x0 + 140 * p + 18 * math.sin(2 * math.pi * (p * 2 + giro)))
                _compor(frente, modelos[(tam, c)].rotate(360 * (p * 2 + giro)), x, y)
            saida.append((None, frente))
    elif efeito == "neon":
        for i in range(n):
            t = i / n
            pulso = 0.55 + 0.45 * math.sin(2 * math.pi * t)
            if i in (n // 4, n // 4 + 1, int(n * 0.7)):
                pulso *= 0.25  # piscada de lâmpada neon
            brilho = vazio()
            g = ImageDraw.Draw(brilho)
            g.rounded_rectangle((8, 8, L - 9, A - 9), radius=26, outline=_rgba(acento, 255 * pulso), width=12)
            g.rounded_rectangle((26, 26, L - 27, A - 27), radius=18, outline=_rgba(segunda, 200 * pulso), width=6)
            brilho = brilho.filter(ImageFilter.GaussianBlur(9))
            g2 = ImageDraw.Draw(brilho)
            g2.rounded_rectangle((6, 6, L - 7, A - 7), radius=26, outline=_rgba("#FFFFFF", 230 * pulso), width=2)
            saida.append((None, brilho))
    elif efeito == "aurora":
        for i in range(n):
            t, tras = i / n, vazio()
            g = ImageDraw.Draw(tras)
            for k, cor in enumerate((acento, segunda, "#8CFFD9")):
                pontos = []
                for x in range(0, L + 20, 20):
                    y = A * (0.18 + 0.1 * k) + 38 * math.sin(2 * math.pi * (x / L * 1.3 + t + k / 3))
                    pontos.append((x, y))
                g.line(pontos, fill=_rgba(cor, 150), width=46, joint="curve")
            tras = tras.filter(ImageFilter.GaussianBlur(22))
            saida.append((tras, None))
    elif efeito == "neve":
        flocos = [(sorte.uniform(0, L), sorte.random(), sorte.choice([1, 1, 2]), sorte.choice([2, 2, 3, 4, 0]),
                   sorte.random()) for _ in range(70)]
        for i in range(n):
            t, frente = i / n, vazio()
            g = ImageDraw.Draw(frente)
            for x0, fase, vel, r, giro in flocos:
                p = (t * vel + fase) % 1
                y = p * (A + 30) - 15
                x = x0 + 16 * math.sin(2 * math.pi * (p + giro))
                if r == 0:
                    g.text((x, y), "❄", font=_fonte(16, True), fill=_rgba("#FFFFFF", 210), anchor="mm")
                else:
                    g.ellipse((x - r, y - r, x + r, y + r), fill=_rgba("#FFFFFF", 220))
            saida.append((None, frente))
    elif efeito == "brasas":
        brasas = [(sorte.uniform(L * 0.05, L - 20), sorte.random(), sorte.choice([1, 1, 2]), sorte.uniform(1.5, 4),
                   sorte.choice([acento, "#FFB347", segunda])) for _ in range(46)]
        for i in range(n):
            t = i / n
            tras, frente = vazio(), vazio()
            gt = ImageDraw.Draw(tras)
            pulso = 0.6 + 0.4 * math.sin(2 * math.pi * t * 2)
            for k in range(14):
                gt.rectangle((0, A - 14 * (k + 1), L, A - 14 * k), fill=_rgba(acento, (60 - k * 4) * pulso))
            g = ImageDraw.Draw(frente)
            for x0, fase, vel, r, c in brasas:
                p = (t * vel + fase) % 1
                y = A - p * A * 0.85
                x = x0 + 10 * math.sin(2 * math.pi * (p * 3 + fase))
                alfa = 255 * (1 - p) * (0.6 + 0.4 * math.sin(2 * math.pi * (t * 4 + fase)))
                g.ellipse((x - r * 3, y - r * 3, x + r * 3, y + r * 3), fill=_rgba(c, alfa * 0.25))
                g.ellipse((x - r, y - r, x + r, y + r), fill=_rgba(c, alfa))
            saida.append((tras, frente))
    elif efeito == "diamante":
        brilho_branco = _faixa_arco_iris(90, A * 2, 150, cores=["#FFFFFF"]).rotate(22, expand=True)
        perimetro = 2 * (L + A)
        for i in range(n):
            t, frente = i / n, vazio()
            g = ImageDraw.Draw(frente)
            for desloc in (0.0, 0.5):  # dois brilhos correndo pela borda
                d = ((t + desloc) % 1) * perimetro
                if d < L:
                    x, y = d, 8
                elif d < L + A:
                    x, y = L - 8, d - L
                elif d < 2 * L + A:
                    x, y = L - (d - L - A), A - 8
                else:
                    x, y = 8, A - (d - 2 * L - A)
                g.ellipse((x - 26, y - 26, x + 26, y + 26), fill=_rgba("#FFFFFF", 60))
                g.text((x, y), "✦", font=_fonte(34, True), fill=_rgba("#FFFFFF", 255), anchor="mm")
            xb = int(-brilho_branco.width + (L + brilho_branco.width * 2) * ((t * 1.0) % 1))
            _compor(frente, brilho_branco, xb, -A // 2)
            for k, (bx, by) in enumerate(((L - 90, 70), (420, 250), (90, 110), (L - 160, A - 120))):
                a = max(0.0, math.sin(2 * math.pi * (t * 2 + k / 4))) ** 6
                if a > 0.05:
                    g.text((bx, by), "✦", font=_fonte(int(18 + 22 * a), True), fill=_rgba(segunda, 255 * a), anchor="mm")
            saida.append((None, frente))
    elif efeito == "noite":
        estrelas = [(sorte.randint(10, L - 10), sorte.randint(10, A - 10), sorte.random(), sorte.choice([1, 1, 2]))
                    for _ in range(110)]
        for i in range(n):
            t, frente = i / n, vazio()
            g = ImageDraw.Draw(frente)
            for x, y, fase, r in estrelas:
                a = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2 * math.pi * (t * 2 + fase)))
                g.ellipse((x - r, y - r, x + r, y + r), fill=_rgba("#FFFFFF", 230 * a))
            if 0.15 <= t <= 0.55:  # estrela cadente
                p = (t - 0.15) / 0.4
                hx, hy = L * (0.95 - 0.45 * p), 30 + 170 * p
                for k in range(16):
                    tx, ty = hx + k * 9, hy - k * 3.4
                    g.ellipse((tx - 3, ty - 3, tx + 3, ty + 3), fill=_rgba(segunda, 240 * (1 - k / 16) * (1 - p * 0.3)))
                g.text((hx, hy), "✦", font=_fonte(24, True), fill=_rgba("#FFFFFF", 255), anchor="mm")
            saida.append((None, frente))
    else:
        saida = [(None, None)] * n
    return saida


def desenhar_cartao_animado(d: dict[str, Any], efeito: str | None, rapido: bool = False) -> bytes:
    """Cartão premium em GIF: fundo (pode ser GIF) + efeito brilhante por cima de tudo."""
    L, A = 1100, altura_do_cartao(d)
    tema = TEMAS_CARTAO.get(d.get("tema") or TEMA_PADRAO, TEMAS_CARTAO[TEMA_PADRAO])
    fundos, duracao, so_cores = fundos_do_cartao(d, tema, L, A, animado=True)
    k = len(fundos)
    n = k * max(1, -(-20 // k)) if k > 1 else 20
    n = min(n, 16 if rapido else 30)
    if k == 1:
        duracao = 75
    semente = sum(map(ord, d.get("nome", "")))
    enfeites = camada_enfeites(tema, so_cores, semente, L, A)
    conteudo = camada_conteudo(d, tema, L, A)
    efeitos = quadros_do_efeito(efeito or "", tema, L, A, n, semente)

    base_fixa = None
    if k == 1:
        base_fixa = fundos[0].convert("RGBA")
    quadros = []
    for i in range(n):
        q = (base_fixa if base_fixa is not None else fundos[i % k].convert("RGBA")).copy()
        tras, frente = efeitos[i]
        if efeito in PARTICULAS_ATRAS and frente is not None:
            tras, frente = frente, None  # partículas passam por trás dos textos
        if tras is not None:
            q.alpha_composite(tras)
        q.alpha_composite(enfeites)
        q.alpha_composite(conteudo)
        if frente is not None:
            q.alpha_composite(frente)
        quadros.append(q.convert("RGB"))

    tentativas = ((0.55, 128), (0.45, 96)) if rapido else ((0.8, 200), (0.7, 160), (0.6, 128))
    for escala, cores in tentativas:
        tamanho = (int(L * escala), int(A * escala))
        prontos = [q.resize(tamanho, Image.LANCZOS).quantize(colors=cores, method=Image.Quantize.FASTOCTREE,
                                                              dither=Image.Dither.FLOYDSTEINBERG) for q in quadros]
        saida = io.BytesIO()
        prontos[0].save(saida, "GIF", save_all=True, append_images=prontos[1:], duration=duracao, loop=0,
                        disposal=1, optimize=False)
        if saida.tell() <= (2_500_000 if rapido else 7_500_000):  # Cabe no limite de envio do Discord.
            return saida.getvalue()
    return saida.getvalue()


_guardados_cartao: dict[str, tuple[float, Any]] = {}


async def _guardado(chave: str, fabrica: Any, validade: float) -> Any:
    """Guarda o resultado por um tempo: trocar tema/cor no painel não busca tudo de novo."""
    agora = time.monotonic()
    item = _guardados_cartao.get(chave)
    if item and agora - item[0] < validade:
        return item[1]
    valor = await fabrica()
    _guardados_cartao[chave] = (agora, valor)
    if len(_guardados_cartao) > 150:
        for velho in sorted(_guardados_cartao, key=lambda k: _guardados_cartao[k][0])[:50]:
            _guardados_cartao.pop(velho, None)
    return valor


async def _imagem_guardada(url: str) -> bytes | None:
    try:
        return await _guardado(f"img:{url}", lambda: _baixar(url, binario=True), 6 * 3600)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def dados_do_cartao(guild: discord.Guild, membro: discord.Member, conta: dict[str, Any]) -> dict[str, Any]:
    puuid = conta["puuid"]
    versao = (await versoes_ddragon())[0]
    base = f"https://ddragon.leagueoflegends.com/cdn/{versao}/img"
    url_invocador = f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{quote(puuid, safe='')}"
    invocador, ranques, maestrias = await asyncio.gather(
        _guardado(f"inv:{puuid}", lambda: chamar_riot(url_invocador), 300),
        ranques_conta(puuid), maestrias_conta(puuid), return_exceptions=True,
    )
    invocador = invocador if isinstance(invocador, dict) else {}
    ranques = ranques if isinstance(ranques, list) else []
    maestrias = maestrias if isinstance(maestrias, list) else []

    def ranque(fila: str) -> dict[str, Any] | None:
        e = next((x for x in ranques if isinstance(x, dict) and x.get("queueType") == fila), None)
        if not e:
            return None
        tier = str(e.get("tier", ""))
        divisao = "" if tier in ("MASTER", "GRANDMASTER", "CHALLENGER") else f" {e.get('rank', '')}"
        v, dd = int(e.get("wins", 0)), int(e.get("losses", 0))
        return {"tier": tier, "nome": f"{NOMES_TIER.get(tier, tier.title())}{divisao}",
                "detalhe": f"{e.get('leaguePoints', 0)} PDL  •  {_porcentagem(v, v + dd)}%  •  {v}V {dd}D"}

    principal = maestrias[0] if maestrias else None
    dados = (await campeoes_completos(versao)).get("data", {})
    por_id = {int(c["key"]): c for c in dados.values()}
    main = por_id.get(int(principal["championId"])) if principal else None
    estilo = estilos_perfil.get(str(membro.id), {})
    fundo_tipo = estilo.get("fundo", "principal")
    splash = f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{main['id']}_0.jpg" if main else None
    if fundo_tipo == "skin" and estilo.get("skin"):
        splash = f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{estilo['skin']}.jpg"
    fundo_imagem = None
    if fundo_tipo == "imagem":
        try:
            fundo_imagem = (PASTA_FUNDOS / f"{membro.id}.jpg").read_bytes()
        except OSError:
            fundo_imagem = None
    downloads = {
        "icone": f"{base}/profileicon/{invocador.get('profileIconId', 29)}.png",
        "splash": None if fundo_tipo in ("imagem", "cores") else splash,
        "main_icone": f"{base}/champion/{main['image']['full']}" if main else None,
    }
    chaves = [k for k, u in downloads.items() if u]
    baixadas = await asyncio.gather(*[_imagem_guardada(downloads[k]) for k in chaves])
    imagens = dict(zip(chaves, baixadas))
    est = estatisticas_internas(guild.id, membro.id)
    pontos_main = int(principal.get("championPoints", 0)) if principal else 0

    # Outras contas do membro (até 4): Riot ID + melhor elo.
    outras = []
    extras_contas = [c for c in contas_do_usuario(membro.id) if c.get("puuid") != puuid][:4]
    todas_entradas = await asyncio.gather(*[ranques_conta(c["puuid"]) for c in extras_contas], return_exceptions=True)
    for extra, entradas in zip(extras_contas, todas_entradas):
        if not isinstance(entradas, list):
            entradas = []
        melhor = None
        for fila in ("RANKED_SOLO_5x5", "RANKED_FLEX_SR"):
            melhor = melhor or next((x for x in entradas if isinstance(x, dict) and x.get("queueType") == fila), None)
        if melhor:
            tier = str(melhor.get("tier", ""))
            divisao = "" if tier in ("MASTER", "GRANDMASTER", "CHALLENGER") else f" {melhor.get('rank', '')}"
            outras.append({"riot_id": extra.get("riot_id", "?"), "tier": tier,
                           "elo": f"{NOMES_TIER.get(tier, tier.title())}{divisao}"})
        else:
            outras.append({"riot_id": extra.get("riot_id", "?"), "tier": None, "elo": "Sem ranque"})

    fundo_gif = None
    if fundo_tipo == "imagem":
        try:
            fundo_gif = (PASTA_FUNDOS / f"{membro.id}.gif").read_bytes()
        except OSError:
            fundo_gif = None
    return {
        "outras": outras[:4], "fundo_gif": fundo_gif, "efeito": estilo.get("efeito"),
        **imagens, "nome": membro.display_name, "riot_id": conta.get("riot_id", ""),
        "nivel": invocador.get("summonerLevel"), "solo": ranque("RANKED_SOLO_5x5"), "flex": ranque("RANKED_FLEX_SR"),
        "main_nome": main["name"] if main else None,
        "main_detalhe": f"Maestria {principal.get('championLevel', 0)}  •  {_pontos(pontos_main)} pts" if principal else "",
        "internas": est, "medalhas": medalhas_do_membro(guild.id, membro.id, est, pontos_main),
        "tema": estilo.get("tema"), "fundo_tipo": fundo_tipo, "fundo_imagem": fundo_imagem,
    }


# ------------------------------ PREMIUM ------------------------------
# Premium = staff, quem deu boost no servidor ou quem a staff liberar com /premium dar.
ARQUIVO_PREMIUM = Path("/data/premium.json")
premium_liberados: dict[str, list[str]] = _carregar_json(ARQUIVO_PREMIUM, {})


def eh_staff(membro: discord.abc.User) -> bool:
    perms = getattr(membro, "guild_permissions", None)
    return bool(perms and (perms.manage_channels or perms.manage_guild or perms.administrator))


def eh_premium(membro: discord.abc.User) -> bool:
    if not isinstance(membro, discord.Member):
        return False
    if eh_staff(membro) or membro.premium_since is not None:
        return True
    return str(membro.id) in premium_liberados.get(str(membro.guild.id), [])


def motivo_premium(membro: discord.Member) -> str:
    if eh_staff(membro):
        return "staff"
    if membro.premium_since is not None:
        return "boost no servidor"
    return "liberado pela staff"


grupo_premium = app_commands.Group(name="premium", description="Cartão premium (efeitos brilhantes e fundo em GIF)",
                                   guild_only=True)


@grupo_premium.command(name="dar", description="Staff: libera o cartão premium para um membro")
async def premium_dar(inter: discord.Interaction, membro: discord.Member) -> None:
    if not await _pode_configurar(inter):
        await inter.response.send_message("🔒 Só a staff pode liberar o premium.", ephemeral=True)
        return
    lista = premium_liberados.setdefault(str(inter.guild_id), [])
    if str(membro.id) not in lista:
        lista.append(str(membro.id))
        _salvar_json(ARQUIVO_PREMIUM, premium_liberados)
    await inter.response.send_message(f"💎 {membro.mention} agora tem o cartão premium! Use `/perfil_estilo`.",
                                      allowed_mentions=discord.AllowedMentions(users=True))


@grupo_premium.command(name="remover", description="Staff: tira o premium liberado de um membro")
async def premium_remover(inter: discord.Interaction, membro: discord.Member) -> None:
    if not await _pode_configurar(inter):
        await inter.response.send_message("🔒 Só a staff pode remover o premium.", ephemeral=True)
        return
    lista = premium_liberados.get(str(inter.guild_id), [])
    if str(membro.id) in lista:
        lista.remove(str(membro.id))
        _salvar_json(ARQUIVO_PREMIUM, premium_liberados)
    extra = "\n-# Ele continua premium por ser staff ou dar boost." if eh_premium(membro) else ""
    await inter.response.send_message(f"🔕 Premium liberado removido de {membro.mention}.{extra}", ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())


@grupo_premium.command(name="lista", description="Quem tem o cartão premium")
async def premium_lista(inter: discord.Interaction) -> None:
    guild = inter.guild
    liberados = [f"<@{u}>" for u in premium_liberados.get(str(guild.id), [])]
    boosters = [m.mention for m in getattr(guild, "premium_subscribers", [])]
    texto = (f"💎 **Liberados pela staff:** {', '.join(liberados) or 'ninguém'}\n"
             f"🚀 **Boosters:** {', '.join(boosters[:40]) or 'ninguém'}\n"
             "🛡️ **Staff:** todos com Gerenciar Canais/Servidor")
    await inter.response.send_message(texto[:2000], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


bot.tree.add_command(grupo_premium)


def conta_do_cartao(membro_id: int, conta: str | None = None) -> dict[str, Any] | None:
    contas = contas_do_usuario(membro_id)
    if conta:
        return encontrar_conta_riot(contas, conta)
    preferida = estilos_perfil.get(str(membro_id), {}).get("conta")
    return next((c for c in contas if c.get("puuid") == preferida), None) or (contas[0] if contas else None)


async def gerar_arquivo_cartao(guild: discord.Guild, membro: discord.Member, conta: dict[str, Any],
                               nome: str = "perfil", estatico: bool = False, rapido: bool = False) -> discord.File:
    """PNG normal ou GIF animado (premium com efeito ou fundo em GIF). rapido = prévia leve do painel."""
    dados = await dados_do_cartao(guild, membro, conta)
    animado = not estatico and eh_premium(membro) and bool(dados.get("efeito") or dados.get("fundo_gif"))
    if animado:
        gif = await _desenhar(desenhar_cartao_animado, dados, dados.get("efeito"), rapido, animado=True)
        return discord.File(io.BytesIO(gif), filename=f"{nome}.gif")
    imagem = await _desenhar(desenhar_cartao, dados, rapido)
    return discord.File(io.BytesIO(imagem), filename=f"{nome}.{'jpg' if rapido else 'png'}")


@bot.tree.command(name="perfil", description="Cartão de jogador com elo, campeão principal, personalizadas e medalhas")
@app_commands.describe(jogador="De quem é o cartão (vazio = você)", conta="Qual conta mostrar (vazio = a principal do cartão)")
@app_commands.autocomplete(conta=autocompletar_conta_riot)
@app_commands.guild_only()
async def perfil(inter: discord.Interaction, jogador: discord.Member | None = None, conta: Optional[str] = None) -> None:
    if Image is None:
        await inter.response.send_message("⚠️ Falta instalar o Pillow no bot (veja o requirements.txt).", ephemeral=True)
        return
    alvo = jogador or inter.user
    escolhida = conta_do_cartao(alvo.id, conta)
    if escolhida is None:
        await inter.response.send_message(f"🔗 {alvo.mention} não tem conta vinculada. Use `/link Nick#TAG`.",
                                          ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    destino = destino_aviso(inter.guild, "perfil")
    await inter.response.defer(thinking=True, ephemeral=destino is not None and destino.id != inter.channel_id)
    try:
        arquivo = await gerar_arquivo_cartao(inter.guild, alvo, escolhida)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, ErroRiot, OSError) as exc:
        log.warning("Perfil: falhou: %s", exc)
        await inter.followup.send("⚠️ Não consegui montar o cartão agora.")
        return
    if destino is not None and destino.id != inter.channel_id:
        enviado = await enviar_aviso(inter.guild, "perfil", arquivo=arquivo, membro=alvo.mention)
        await inter.followup.send(f"📊 Cartão enviado em {destino.mention}!" if enviado else "⚠️ Não consegui enviar no canal configurado.")
        return
    await inter.followup.send(content=mensagem_aviso(inter.guild.id, "perfil", membro=alvo.mention),
                              file=arquivo, allowed_mentions=MENCOES_AVISO)


# ------------------------- /perfil_estilo -------------------------
ARQUIVO_ESTILOS = Path("/data/perfil_estilos.json")
PASTA_FUNDOS = Path("/data/perfil_fundos")
estilos_perfil: dict[str, dict[str, Any]] = _carregar_json(ARQUIVO_ESTILOS, {})
ESCOLHAS_TEMA = [app_commands.Choice(name=t["nome"], value=k) for k, t in TEMAS_CARTAO.items()]
ESCOLHAS_FUNDO = [
    app_commands.Choice(name="🏆 Arte do meu campeão principal", value="principal"),
    app_commands.Choice(name="🎨 Uma skin que eu escolher", value="skin"),
    app_commands.Choice(name="🖼️ Uma imagem que eu enviar", value="imagem"),
    app_commands.Choice(name="🌸 Só as cores do tema", value="cores"),
]


async def autocompletar_campeao(inter: discord.Interaction, atual: str) -> list[app_commands.Choice[str]]:
    try:
        dados = (await campeoes_completos((await versoes_ddragon())[0])).get("data", {})
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    texto = (atual or "").casefold()
    return [app_commands.Choice(name=c["name"], value=c["id"])
            for c in sorted(dados.values(), key=lambda c: c["name"]) if texto in c["name"].casefold()][:25]


async def autocompletar_skin(inter: discord.Interaction, atual: str) -> list[app_commands.Choice[str]]:
    campeao = getattr(inter.namespace, "campeao", None)
    if not campeao:
        return [app_commands.Choice(name="Escolha primeiro o campeão", value="nenhuma")]
    try:
        dados = (await campeoes_completos((await versoes_ddragon())[0])).get("data", {})
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    c = dados.get(campeao)
    if not c:
        return []
    texto = (atual or "").casefold()
    saida = []
    for skin in c.get("skins", []):
        nome = "Visual clássico" if skin.get("name") == "default" else skin.get("name", "")
        if texto in nome.casefold():
            saida.append(app_commands.Choice(name=nome[:100], value=f"{c['id']}_{skin.get('num', 0)}"))
    return saida[:25]


def _salvar_fundo(uid: int, dados: bytes) -> bool:
    """Salva o fundo. Devolve True se for um GIF animado."""
    PASTA_FUNDOS.mkdir(parents=True, exist_ok=True)
    imagem = _encaixar(dados, 1100, 500)
    imagem.save(PASTA_FUNDOS / f"{uid}.jpg", "JPEG", quality=88)
    animado = getattr(Image.open(io.BytesIO(dados)), "n_frames", 1) > 1
    gif = PASTA_FUNDOS / f"{uid}.gif"
    if animado:
        gif.write_bytes(dados)
    else:
        gif.unlink(missing_ok=True)
    return animado


def _normalizar(texto: str) -> str:
    import unicodedata
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", texto.lower())


async def achar_campeao(texto: str) -> dict[str, Any] | None:
    dados = (await campeoes_completos((await versoes_ddragon())[0])).get("data", {})
    alvo = _normalizar(texto)
    if not alvo:
        return None
    exatos = [c for c in dados.values() if _normalizar(c["name"]) == alvo or _normalizar(c["id"]) == alvo]
    if exatos:
        return exatos[0]
    comecam = [c for c in dados.values() if _normalizar(c["name"]).startswith(alvo)]
    return comecam[0] if len(comecam) == 1 else None


async def previa_do_estilo(guild: discord.Guild, membro: discord.Member, estatico: bool = False) -> discord.File | None:
    conta = conta_do_cartao(membro.id)
    if conta is None or Image is None:
        return None
    try:
        return await gerar_arquivo_cartao(guild, membro, conta, nome="previa", estatico=estatico, rapido=True)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, ErroRiot, OSError) as exc:
        log.warning("Perfil: prévia falhou: %s", exc)
        return None


def _texto_painel(estilo: dict[str, Any], aviso: str = "", membro: discord.abc.User | None = None) -> str:
    tema = TEMAS_CARTAO.get(estilo.get("tema") or TEMA_PADRAO, TEMAS_CARTAO[TEMA_PADRAO])["nome"]
    fundo = {c.value: c.name for c in ESCOLHAS_FUNDO}.get(estilo.get("fundo", "principal"), "")
    extra = ""
    if estilo.get("fundo") == "skin" and estilo.get("skin"):
        extra = f" ({estilo.get('skin_nome') or estilo['skin']})"
    efeito = EFEITOS_PREMIUM.get(estilo.get("efeito") or "", "Sem efeito")
    premium = ""
    if membro is not None:
        premium = (f"\n💎 Premium ativo ({motivo_premium(membro)})" if eh_premium(membro)
                   else "\n-# 💎 Efeitos animados e fundo em GIF são premium (staff, boosters ou liberado pela staff).")
    return (f"🎀 **Estilo do seu cartão**\nTema: **{tema}**\nFundo: **{fundo}{extra}**\nEfeito: **{efeito}**{premium}"
            + (f"\n{aviso}" if aviso else "")
            + "\n-# Escolha nos menus abaixo; a prévia muda na hora. As mudanças já ficam salvas.")


_vez_do_painel: dict[int, int] = {}


class PainelEstiloView(discord.ui.View):
    def __init__(self, dono: int, campeao: dict[str, Any] | None = None) -> None:
        super().__init__(timeout=900)
        self.dono = dono
        self.campeao = campeao
        estilo = estilos_perfil.get(str(dono), {})

        temas = discord.ui.Select(placeholder="🎨 Escolha o tema de cores", row=0, options=[
            discord.SelectOption(label=t["nome"].split(" ", 1)[1], value=k, emoji=t["nome"].split()[0],
                                 default=(estilo.get("tema") or TEMA_PADRAO) == k)
            for k, t in TEMAS_CARTAO.items()])
        temas.callback = self.escolher_tema
        self.add_item(temas)

        fundos = discord.ui.Select(placeholder="🖼️ Escolha o plano de fundo", row=1, options=[
            discord.SelectOption(label=c.name.split(" ", 1)[1], value=c.value, emoji=c.name.split()[0],
                                 default=estilo.get("fundo", "principal") == c.value)
            for c in ESCOLHAS_FUNDO])
        fundos.callback = self.escolher_fundo
        self.add_item(fundos)

        efeito_atual = estilo.get("efeito") or "nenhum"
        efeitos = discord.ui.Select(placeholder="💎 Efeito brilhante animado (premium)", row=2, options=[
            discord.SelectOption(label="Sem efeito", value="nenhum", emoji="⭕", default=efeito_atual == "nenhum")] + [
            discord.SelectOption(label=nome.split(" ", 1)[1], value=chave, emoji=nome.split()[0],
                                 description="Premium • o cartão vira GIF animado",
                                 default=efeito_atual == chave)
            for chave, nome in EFEITOS_PREMIUM.items()])
        efeitos.callback = self.escolher_efeito
        self.add_item(efeitos)

        contas = contas_do_usuario(dono)
        if not campeao and len(contas) > 1:
            principal = (conta_do_cartao(dono) or {}).get("puuid")
            seletor_conta = discord.ui.Select(placeholder="👤 Conta principal do cartão", row=3, options=[
                discord.SelectOption(label=c.get("riot_id", "?")[:100], value=c["puuid"][:100], emoji="👤",
                                     default=c.get("puuid") == principal) for c in contas[:25]])
            seletor_conta.callback = self.escolher_conta
            self.add_item(seletor_conta)

        if campeao:
            skins = discord.ui.Select(placeholder=f"✨ Skins de {campeao['name']}", row=3, options=[
                discord.SelectOption(
                    label=("Visual clássico" if sk.get("name") == "default" else sk.get("name", "?"))[:100],
                    value=f"{campeao['id']}_{sk.get('num', 0)}",
                    default=estilo.get("skin") == f"{campeao['id']}_{sk.get('num', 0)}")
                for sk in campeao.get("skins", [])[:25]])
            skins.callback = self.escolher_skin
            self.add_item(skins)

        b_campeao = discord.ui.Button(label="Escolher skin de outro campeão", emoji="🔎", row=4,
                                      style=discord.ButtonStyle.secondary)
        b_campeao.callback = self.abrir_campeao
        b_imagem = discord.ui.Button(label="Enviar imagem ou GIF", emoji="📤", row=4, style=discord.ButtonStyle.secondary)
        b_imagem.callback = self.abrir_imagem
        b_pronto = discord.ui.Button(label="Pronto", emoji="✅", row=4, style=discord.ButtonStyle.success)
        b_pronto.callback = self.fechar
        for b in (b_campeao, b_imagem, b_pronto):
            self.add_item(b)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.dono:
            await inter.response.send_message("🚫 Este painel é de outra pessoa. Use `/perfil_estilo`.", ephemeral=True)
            return False
        return True

    def _salvar(self, **mudancas: Any) -> dict[str, Any]:
        estilo = estilos_perfil.setdefault(str(self.dono), {})
        estilo.update(mudancas)
        _salvar_json(ARQUIVO_ESTILOS, estilos_perfil)
        return estilo

    async def atualizar(self, inter: discord.Interaction, aviso: str = "", campeao: dict[str, Any] | None = None) -> None:
        """Salva já feito; aqui só redesenha a prévia e o painel."""
        if not inter.response.is_done():
            await inter.response.defer()
        vez = _vez_do_painel[self.dono] = _vez_do_painel.get(self.dono, 0) + 1
        nova = PainelEstiloView(self.dono, campeao if campeao is not None else self.campeao)
        estilo = estilos_perfil.get(str(self.dono), {})
        tem_gif = estilo.get("fundo") == "imagem" and (PASTA_FUNDOS / f"{self.dono}.gif").exists()
        animado = eh_premium(inter.user) and bool(estilo.get("efeito") or tem_gif)
        # 1º passo: prévia parada (rápida), para a cor/tema mudar na hora.
        arquivo = await previa_do_estilo(inter.guild, inter.user, estatico=True)
        if _vez_do_painel.get(self.dono) != vez:
            return  # A pessoa já escolheu outra coisa; a prévia mais nova cuida da tela.
        espera = "✨ Gerando a animação..." if animado else ""
        await inter.edit_original_response(
            content=_texto_painel(estilo, "\n".join(t for t in (aviso, espera) if t), inter.user), view=nova,
            attachments=[arquivo] if arquivo else [],
        )
        self.stop()
        if not animado:
            return
        # 2º passo: a versão animada substitui a parada quando fica pronta.
        await asyncio.sleep(0.6)  # Se a pessoa já trocou de novo, nem começa esta animação.
        if _vez_do_painel.get(self.dono) != vez:
            return
        gif = await previa_do_estilo(inter.guild, inter.user)
        if gif is None or _vez_do_painel.get(self.dono) != vez:
            return
        try:
            await inter.edit_original_response(content=_texto_painel(estilo, aviso, inter.user), attachments=[gif])
        except discord.HTTPException as exc:
            log.warning("Perfil: não consegui trocar pela prévia animada: %s", exc)

    async def escolher_tema(self, inter: discord.Interaction) -> None:
        self._salvar(tema=inter.data["values"][0])
        await self.atualizar(inter)

    async def escolher_fundo(self, inter: discord.Interaction) -> None:
        valor = inter.data["values"][0]
        estilo = estilos_perfil.get(str(self.dono), {})
        if valor == "imagem" and not (PASTA_FUNDOS / f"{self.dono}.jpg").exists():
            await inter.response.send_modal(ModalImagemFundo(self))
            return
        if valor == "skin" and not estilo.get("skin") and not self.campeao:
            await inter.response.send_modal(ModalCampeaoFundo(self))
            return
        self._salvar(fundo=valor)
        await self.atualizar(inter)

    async def escolher_efeito(self, inter: discord.Interaction) -> None:
        valor = inter.data["values"][0]
        if valor != "nenhum" and not eh_premium(inter.user):
            await inter.response.send_message(
                "💎 Os efeitos animados são **premium**: para a staff, para quem dá **boost** no servidor "
                "ou para quem a staff liberar.", ephemeral=True)
            return
        self._salvar(efeito=None if valor == "nenhum" else valor)
        await self.atualizar(inter)

    async def escolher_conta(self, inter: discord.Interaction) -> None:
        self._salvar(conta=inter.data["values"][0])
        await self.atualizar(inter)

    async def escolher_skin(self, inter: discord.Interaction) -> None:
        valor = inter.data["values"][0]
        nome = next((o.label for item in self.children if isinstance(item, discord.ui.Select)
                     for o in item.options if o.value == valor), valor)
        self._salvar(fundo="skin", skin=valor, skin_nome=nome)
        await self.atualizar(inter)

    async def abrir_campeao(self, inter: discord.Interaction) -> None:
        await inter.response.send_modal(ModalCampeaoFundo(self))

    async def abrir_imagem(self, inter: discord.Interaction) -> None:
        await inter.response.send_modal(ModalImagemFundo(self))

    async def fechar(self, inter: discord.Interaction) -> None:
        estilo = estilos_perfil.get(str(self.dono), {})
        await inter.response.edit_message(
            content=_texto_painel(estilo, "✅ Pronto! Use `/perfil` para mostrar seu cartão.", inter.user), view=None)
        self.stop()


class ModalCampeaoFundo(discord.ui.Modal):
    def __init__(self, painel: PainelEstiloView) -> None:
        super().__init__(title="Fundo com skin", timeout=300)
        self.painel = painel
        self.entrada = discord.ui.Label(
            text="Nome do campeão", description="Ex.: Ahri, Neeko, Miss Fortune",
            component=discord.ui.TextInput(placeholder="Ahri", max_length=30, required=True))
        self.add_item(self.entrada)

    async def on_submit(self, inter: discord.Interaction) -> None:
        texto = self.entrada.component.value  # type: ignore[union-attr]
        await inter.response.defer()
        try:
            campeao = await achar_campeao(texto)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            campeao = None
        if campeao is None:
            await self.painel.atualizar(inter, f"⚠️ Não achei o campeão **{discord.utils.escape_markdown(texto)}**. Tente de novo.")
            return
        self.painel._salvar(fundo="skin", skin=f"{campeao['id']}_0", skin_nome=f"{campeao['name']} clássico")
        await self.painel.atualizar(inter, f"✨ Escolha a skin de **{campeao['name']}** no menu.", campeao=campeao)


class ModalImagemFundo(discord.ui.Modal):
    def __init__(self, painel: PainelEstiloView) -> None:
        super().__init__(title="Imagem de fundo do cartão", timeout=300)
        self.painel = painel
        self.entrada = discord.ui.Label(
            text="Selecionar imagem", description="PNG, JPG, GIF ou WEBP (até 8 MB)",
            component=discord.ui.FileUpload(required=True, min_values=1, max_values=1))
        self.add_item(self.entrada)

    async def on_submit(self, inter: discord.Interaction) -> None:
        upload = self.entrada.component
        arquivos = upload.values if isinstance(upload, discord.ui.FileUpload) else []
        await inter.response.defer()
        if not arquivos:
            await self.painel.atualizar(inter, "⚠️ Selecione uma imagem.")
            return
        anexo = arquivos[0]
        if not (anexo.content_type or "").startswith("image/") or anexo.size > 8 * 1024 * 1024:
            await self.painel.atualizar(inter, "⚠️ Envie uma imagem (PNG, JPG, GIF ou WEBP) de até 8 MB.")
            return
        try:
            animado = await asyncio.to_thread(_salvar_fundo, inter.user.id, await anexo.read())
        except (OSError, ValueError, discord.HTTPException) as exc:
            log.warning("Perfil: imagem de fundo inválida: %s", exc)
            await self.painel.atualizar(inter, "⚠️ Não consegui abrir essa imagem. Tente outra.")
            return
        self.painel._salvar(fundo="imagem")
        if animado and not eh_premium(inter.user):
            aviso = "🖼️ Imagem aplicada! 💎 GIF animado é premium, então ele aparece parado no seu cartão."
        else:
            aviso = "🖼️ GIF aplicado, se mexendo! ✨" if animado else "🖼️ Imagem aplicada!"
        await self.painel.atualizar(inter, aviso)


@bot.tree.command(name="perfil_estilo", description="Abre o painel para personalizar seu cartão do /perfil")
@app_commands.guild_only()
async def perfil_estilo(inter: discord.Interaction) -> None:
    if not contas_do_usuario(inter.user.id):
        await inter.response.send_message("🔗 Vincule uma conta com `/link Nick#TAG` para ter seu cartão.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    arquivo = await previa_do_estilo(inter.guild, inter.user)
    estilo = estilos_perfil.get(str(inter.user.id), {})
    kwargs: dict[str, Any] = {"content": _texto_painel(estilo, "", inter.user), "view": PainelEstiloView(inter.user.id),
                              "ephemeral": True}
    if arquivo:
        kwargs["file"] = arquivo
    await inter.followup.send(**kwargs)


@bot.tree.command(name="perfil_fundo_remover", description="Staff: remove a imagem de fundo do cartão de um membro")
@app_commands.describe(membro="De quem remover a imagem de fundo")
@app_commands.guild_only()
async def perfil_fundo_remover(inter: discord.Interaction, membro: discord.Member) -> None:
    if not (autorizado(inter) or await pode_gerenciar_contas(inter)):
        await inter.response.send_message("🔒 Só a staff pode remover fundos.", ephemeral=True)
        return
    for ext in ("jpg", "gif"):
        try:
            (PASTA_FUNDOS / f"{membro.id}.{ext}").unlink(missing_ok=True)
        except OSError:
            pass
    estilo = estilos_perfil.get(str(membro.id))
    if estilo and estilo.get("fundo") == "imagem":
        estilo["fundo"] = "principal"
        _salvar_json(ARQUIVO_ESTILOS, estilos_perfil)
    await inter.response.send_message(f"🧹 Fundo de {membro.mention} removido.", ephemeral=True,
                                      allowed_mentions=discord.AllowedMentions.none())


# =====================================================================
# ======================== Tarefas em segundo plano =====================
# =====================================================================
@tasks.loop(minutes=5)
async def tarefas_extras() -> None:
    for nome, func, a_cada in (("patch", checar_patch_novo, 1800), ("rotacao", checar_rotacao_nova, 3600),
                               ("status", checar_status_novo, 300), ("quiz", checar_quiz_automatico, 300)):
        ultima = _ddragon_cache.get(f"ultima:{nome}", 0)
        if time.time() - ultima < a_cada - 5:
            continue
        _ddragon_cache[f"ultima:{nome}"] = time.time()
        try:
            await func()
        except (ErroRiot, aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, OSError) as exc:
            log.warning("Tarefa %s: %s", nome, exc)
        except Exception:
            log.exception("Tarefa %s: erro inesperado", nome)


@tarefas_extras.before_loop
async def _antes_tarefas_extras() -> None:
    await bot.wait_until_ready()


_extras_registrados = False


def iniciar_recursos_extras() -> None:
    global _extras_registrados
    if not _extras_registrados:
        _extras_registrados = True
        bot.add_dynamic_items(BotaoRevanche)
        bot.add_view(PatchSelecaoView())
    if not tarefas_extras.is_running():
        tarefas_extras.start()


# =====================================================================
# ===== Desenho dos cartões num processo separado (o bot não trava) =====
# =====================================================================
import concurrent.futures as _futuros
import multiprocessing as _multiproc

_desenhistas: dict[str, "_futuros.ProcessPoolExecutor | None"] = {"parado": None, "animado": None}


def _preparar_desenhista() -> None:
    """2 processos ajudantes (criados aqui, antes do bot ligar): um para a prévia parada e
    outro para os GIFs. Assim a troca de cor/tema não espera uma animação terminar."""
    for tipo in _desenhistas:
        try:
            ajudante = _futuros.ProcessPoolExecutor(max_workers=1, mp_context=_multiproc.get_context("fork"))
            ajudante.submit(int).result(timeout=30)
            _desenhistas[tipo] = ajudante
        except Exception as exc:  # Sem fork (ex.: Windows): desenha no próprio bot, como antes.
            log.warning("Cartão: sem processo ajudante (%s); desenhando no próprio bot.", exc)
            _desenhistas[tipo] = None


async def _desenhar(funcao: Any, *args: Any, animado: bool = False) -> Any:
    tipo = "animado" if animado else "parado"
    ajudante = _desenhistas.get(tipo)
    if ajudante is not None:
        try:
            return await asyncio.get_running_loop().run_in_executor(ajudante, funcao, *args)
        except _futuros.process.BrokenProcessPool as exc:
            log.warning("Cartão: processo ajudante caiu (%s); desenhando no próprio bot.", exc)
            _desenhistas[tipo] = None
    return await asyncio.to_thread(funcao, *args)


_preparar_desenhista()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina DISCORD_TOKEN no arquivo .env antes de executar o bot.")
    bot.run(TOKEN)
