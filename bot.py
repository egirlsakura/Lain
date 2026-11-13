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


def formatar_elo(entradas: Any) -> str:
    """Mostra apenas a ranqueada Solo/Duo oficial."""
    solo = None
    if isinstance(entradas, list):
        solo = next(
            (e for e in entradas if isinstance(e, dict)
             and e.get("queueType") == "RANKED_SOLO_5x5"),
            None,
        )
    if not solo:
        return "Sem ranque"
    tier = str(solo.get("tier", ""))
    nome = NOMES_TIER.get(tier, tier.title())
    pdl = solo.get("leaguePoints", 0)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return f"{nome} ({pdl} PDL)"
    return f"{nome} {solo.get('rank', '')} ({pdl} PDL)"


async def buscar_elo(puuid: str) -> str:
    dados = await chamar_riot(
        f"https://{RIOT_PLATAFORMA}.api.riotgames.com/lol/league/v4/entries/by-puuid/"
        f"{quote(puuid, safe='')}"
    )
    return formatar_elo(dados)


async def atualizar_elo_se_preciso(usuario_id: int, limite: float = 2.0) -> None:
    """Atualiza o elo salvo se estiver antigo. Em caso de falha, mantém o anterior."""
    conta = contas_riot.get(str(usuario_id))
    if not conta or not RIOT_API_KEY:
        return
    if time.time() - float(conta.get("elo_em", 0)) < ELO_VALIDADE_SEGUNDOS:
        return
    try:
        elo = await asyncio.wait_for(buscar_elo(conta["puuid"]), timeout=limite)
    except (ErroRiot, aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
        log.warning("Não consegui atualizar o elo de %s: %s", usuario_id, exc)
        return
    async with trava_contas_riot:
        conta["elo"] = elo
        conta["elo_em"] = time.time()
        try:
            salvar_contas_riot()
        except OSError as exc:
            log.warning("Não consegui salvar contas_riot.json: %s", exc)


def texto_elo(usuario_id: int) -> str:
    """Trecho exibido ao lado do jogador no painel da fila."""
    conta = contas_riot.get(str(usuario_id))
    if not conta or not conta.get("elo"):
        return ""
    return f" • {conta['elo']}"


@bot.tree.command(
    name="link", description="Vincula sua conta do League of Legends (Riot ID) ao Discord"
)
@app_commands.describe(riot_id="Seu Riot ID no formato Nick#TAG (exemplo: Nick#BR1)")
async def link(inter: discord.Interaction, riot_id: str) -> None:
    if not RIOT_API_KEY:
        await inter.response.send_message(
            "⚠️ A API da Riot ainda não foi configurada no bot (RIOT_API_KEY).",
            ephemeral=True,
        )
        return
    nome, separador, tag = riot_id.strip().rpartition("#")
    nome, tag = nome.strip(), tag.strip()
    if not separador or not 3 <= len(nome) <= 16 or not 2 <= len(tag) <= 5:
        await inter.response.send_message(
            "⚠️ Use o formato **Nick#TAG**. Exemplo: `/link Nick#BR1`", ephemeral=True
        )
        return

    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        conta = await buscar_conta_riot(nome, tag)
        elo = await buscar_elo(conta["puuid"])
    except ErroRiot as exc:
        await inter.followup.send(f"⚠️ {exc}", ephemeral=True)
        return
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
        log.warning("Falha ao consultar a Riot no /link: %s", exc)
        await inter.followup.send(
            "⚠️ Não consegui falar com a Riot agora. Tente de novo daqui a pouco.",
            ephemeral=True,
        )
        return

    riot_nome = f"{conta.get('gameName') or nome}#{conta.get('tagLine') or tag}"
    chave = str(inter.user.id)
    async with trava_contas_riot:
        anterior = contas_riot.get(chave)
        contas_riot[chave] = {
            "puuid": conta["puuid"],
            "riot_id": riot_nome,
            "elo": elo,
            "elo_em": time.time(),
        }
        try:
            salvar_contas_riot()
        except OSError as exc:
            if anterior is None:
                contas_riot.pop(chave, None)
            else:
                contas_riot[chave] = anterior
            log.exception("Não consegui salvar a conta Riot: %s", exc)
            await inter.followup.send(
                "⚠️ Não consegui salvar o vínculo em /data/contas_riot.json.", ephemeral=True
            )
            return

    # Atualiza os painéis de fila abertos em que o jogador já está inscrito.
    if inter.guild is not None:
        async with trava_servidor(inter.guild.id):
            for _chave, fila in filas_abertas(inter.guild.id):
                if inter.user.id in fila.get("inscritos", []):
                    await atualizar_mensagem_fila(inter.guild, fila)

    await inter.followup.send(
        f"✅ Conta vinculada: **{discord.utils.escape_markdown(riot_nome)}**\n"
        f"🏅 Elo (Solo/Duo): **{elo}**\n"
        "Seu elo vai aparecer ao seu lado no painel da fila. "
        "Para trocar de conta, use `/link` de novo.\n\n"
        f"-# {AVISO_RIOT}",
        ephemeral=True,
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
# Partidas do LoL não mudam depois de terminar; guardamos um resumo para não reconsultar.
_cache_partidas_lol: dict[str, dict[str, Any] | None] = {}
_cache_estatisticas: dict[str, tuple[float, dict[str, Any]]] = {}


async def nomes_campeoes() -> dict[int, str]:
    """Nomes dos campeões em português, pelo Data Dragon público da Riot."""
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
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError) as exc:
        log.warning("Não consegui carregar os nomes dos campeões: %s", exc)
    return _nomes_campeoes


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


def texto_estatisticas(
    conta: dict[str, Any], dados: dict[str, Any], nomes: dict[int, str]
) -> str:
    linhas = [
        f"🔗 {discord.utils.escape_markdown(conta.get('riot_id', ''))} • "
        f"{conta.get('elo') or 'Sem ranque'}"
    ]
    recente = dados.get("recente")
    if recente:
        nome_recente = (
            nomes.get(recente["campeao_id"]) or recente["campeao"]
            or f"Campeão {recente['campeao_id']}"
        )
        detalhes = [NOMES_FILAS_LOL.get(recente["fila"], "Outro modo")]
        if recente["rota"] in NOMES_ROTA and recente["fila"] in FILAS_SUMMONERS_RIFT:
            detalhes.append(NOMES_ROTA[recente["rota"]])
        detalhes.append("Vitória" if recente["vitoria"] else "Derrota")
        quando = f" <t:{recente['fim']}:R>" if recente.get("fim") else ""
        linhas.append(
            f"🕹️ Último campeão jogado: **{nome_recente}** ({' • '.join(detalhes)}){quando}"
        )
    if dados["maestria"]:
        linhas.append(
            "⭐ Maior maestria: "
            + ", ".join(nomes.get(cid, f"Campeão {cid}") for cid in dados["maestria"])
        )
    jogos = dados["jogos"]
    if not jogos:
        linhas.append("ℹ️ Nenhuma partida recente de Summoner's Rift encontrada.")
        return "\n".join(linhas)

    por_campeao: dict[int, dict[str, Any]] = {}
    por_rota: dict[str, dict[str, Any]] = {}
    for jogo in jogos:
        nome = nomes.get(jogo["campeao_id"]) or jogo["campeao"] or f"Campeão {jogo['campeao_id']}"
        campeao = por_campeao.setdefault(jogo["campeao_id"], {"nome": nome, "j": 0, "v": 0})
        campeao["j"] += 1
        campeao["v"] += int(jogo["vitoria"])
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
            f"🏆 Melhor campeão: **{melhor['nome']}** — {melhor['v']}V {melhor['j'] - melhor['v']}D "
            f"({_porcentagem(melhor['v'], melhor['j'])}%)"
        )
    else:
        linhas.append("🏆 Melhor campeão: ainda sem vitórias nas partidas analisadas")
    mais_derrotas = max(campeoes, key=lambda c: (c["j"] - c["v"], c["j"]))
    derrotas = mais_derrotas["j"] - mais_derrotas["v"]
    if derrotas:
        linhas.append(
            f"📉 Mais derrotas com: {mais_derrotas['nome']} — {derrotas} derrota(s) "
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
            f"🎯 Melhor rota: **{melhor_rota['nome']}** — "
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
)
@app_commands.choices(
    equipe=[
        app_commands.Choice(name="👥 Todos os times", value="todos"),
        *[
            app_commands.Choice(name=f"{emoji} {nome}", value=chave)
            for chave, emoji, nome in EQUIPES
        ],
    ]
)
@app_commands.guild_only()
async def estatisticas(
    inter: discord.Interaction,
    equipe: app_commands.Choice[str] | None = None,
    jogador: discord.Member | None = None,
    partida: Optional[app_commands.Range[int, 1, 99]] = None,
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

    await inter.response.defer(ephemeral=True, thinking=True)
    nomes = await nomes_campeoes()
    # Com muitos jogadores, analisa menos partidas de cada um para não demorar demais.
    total_jogadores = sum(len(ids) for _t, ids in grupos)
    quantidade = PARTIDAS_ANALISADAS if total_jogadores <= 10 else 8
    chave_invalida = False
    # Cada time é enviado assim que fica pronto, sem esperar os outros.
    for titulo, ids in grupos:
        embed = discord.Embed(title=titulo, color=0x5865F2)
        for uid in ids[:6]:
            membro = guild.get_member(uid)
            nome_membro = membro.display_name if membro else f"Jogador {uid}"
            conta = contas_riot.get(str(uid))
            if not conta:
                embed.add_field(
                    name=nome_membro, value="🔗 Ainda não vinculou a conta (`/link Nick#TAG`).",
                    inline=False,
                )
                continue
            if chave_invalida:
                embed.add_field(name=nome_membro, value="⚠️ Consulta interrompida.", inline=False)
                continue
            try:
                dados = await dados_estatisticas(conta["puuid"], quantidade)
            except ErroRiot as exc:
                embed.add_field(name=nome_membro, value=f"⚠️ {exc}", inline=False)
                chave_invalida = exc.status in (401, 403)
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
                log.warning("Falha nas estatísticas de %s: %s", uid, exc)
                embed.add_field(
                    name=nome_membro, value="⚠️ Não consegui falar com a Riot agora.",
                    inline=False,
                )
                continue
            embed.add_field(
                name=nome_membro, value=texto_estatisticas(conta, dados, nomes), inline=False
            )
        embed.set_footer(
            text=f"Últimas {quantidade} partidas de Summoner's Rift (normal e ranqueada).\n"
            + AVISO_RIOT
        )
        await inter.followup.send(embed=embed, ephemeral=True)


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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina DISCORD_TOKEN no arquivo .env antes de executar o bot.")
    bot.run(TOKEN)
