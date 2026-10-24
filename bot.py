from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
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
PASTA_IMAGENS_FILA = Path("/data/queue_imagens")
TAMANHO_TIME = 5
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


def carregar_filas() -> dict[str, dict[str, Any]]:
    if not ARQUIVO_FILAS.exists():
        return {}
    try:
        dados = json.loads(ARQUIVO_FILAS.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("A raiz deve ser um objeto JSON.")
        return dados
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


def salvar_config_imagens() -> None:
    ARQUIVO_IMAGENS_FILA.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_IMAGENS_FILA.with_suffix(".json.tmp")
    temporario.write_text(
        json.dumps(imagens_fila, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporario.replace(ARQUIVO_IMAGENS_FILA)


def guardar_imagem_fila(guild_id: int, dados: bytes) -> str:
    """Guarda em /data, que persiste entre redeploys no FadeHost."""
    formato = tipo_imagem(dados)
    if formato is None or len(dados) > LIMITE_BYTES:
        raise ValueError("Use PNG, JPG, GIF ou WEBP de até 10 MB.")
    extensao = {"PNG": "png", "JPG": "jpg", "GIF": "gif", "WEBP": "webp"}[formato]
    nome = f"painel_lol_{guild_id}.{extensao}"
    PASTA_IMAGENS_FILA.mkdir(parents=True, exist_ok=True)
    caminho = PASTA_IMAGENS_FILA / nome
    temporario = PASTA_IMAGENS_FILA / f"{guild_id}.upload.tmp"
    try:
        temporario.write_bytes(dados)
        temporario.replace(caminho)
        anterior = imagens_fila.get(str(guild_id), {}).get("nome")
        imagens_fila[str(guild_id)] = {"nome": nome}
        salvar_config_imagens()
        if anterior and anterior != nome:
            (PASTA_IMAGENS_FILA / anterior).unlink(missing_ok=True)
        return nome
    finally:
        temporario.unlink(missing_ok=True)


def imagem_fila_salva(guild_id: int) -> tuple[bytes, str] | None:
    registro = imagens_fila.get(str(guild_id))
    if not registro:
        return None
    nome = registro.get("nome", "")
    if not nome or Path(nome).name != nome:
        return None
    caminho = PASTA_IMAGENS_FILA / nome
    try:
        dados = caminho.read_bytes()
    except OSError:
        log.warning("Imagem padrão da fila não encontrada: %s", caminho)
        return None
    if tipo_imagem(dados) is None or len(dados) > LIMITE_BYTES:
        log.warning("Imagem padrão da fila é inválida: %s", caminho)
        return None
    return dados, nome


# Se reiniciou enquanto uma fila estava iniciando, a partida já está salva.
for _gid, _fila in list(filas.items()):
    if _fila.get("status") == "iniciando":
        if _gid in partidas:
            filas.pop(_gid, None)
        else:
            _fila["status"] = "aberta"
# As alterações de recuperação são persistidas depois de definir salvar_filas().
_filas_recuperadas = any(
    fila.get("status") == "iniciando" for fila in carregar_filas().values()
)
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


intents = discord.Intents.default()
intents.members = True
intents.voice_states = True


class ClientePartidas(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
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
        for guild_id, fila in list(filas.items()):
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
        chave = str(guild.id)
        partida = partidas.get(chave)
        if partida is None:
            await interaction.followup.send("ℹ️ Não há partida ativa neste servidor.")
            return
        anterior = partida.get("vencedor")
        if anterior and vencedor and anterior != vencedor:
            await interaction.followup.send(
                f"⚠️ O vencedor já foi registrado como **{anterior.title()}**. "
                "Não é possível alterar o resultado enquanto a partida está sendo finalizada."
            )
            return
        # Não aceita declarar um time que não participa desta partida.
        chaves_ativas = set((partida.get("canais") or {}).keys()) or {"vermelho", "azul"}
        if vencedor is not None and vencedor not in chaves_ativas:
            await interaction.followup.send(
                "⚠️ Esse time não participa desta partida. Selecione um vencedor válido."
            )
            return
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
            partidas.pop(chave, None)
            salvar_partidas()

        resultado = partida.get("vencedor")
        if resultado:
            detalhes = next((f"{emoji} {nome.upper()}" for chave, emoji, nome in EQUIPES
                              if chave == resultado), resultado.upper())
            mensagem = f"🏆 **O TIME {detalhes} VENCEU!**\n"
        else:
            mensagem = "🛑 **Partida cancelada.**\n"
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
        await interaction.followup.send(mensagem, allowed_mentions=discord.AllowedMentions.none())



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
        chave = str(guild.id)
        if chave in partidas:
            await interaction.followup.send(
                "⚠️ Já existe uma partida neste servidor. Use `/partida_status` ou finalize a atual."
            )
            return
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
            categoria = await guild.create_category("🎮 PARTIDA EM ANDAMENTO", reason="Início da partida")
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
            title="🎮 Partida iniciada!",
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
        partida = partidas.get(str(guild.id))
        if partida is None:
            await interaction.response.send_message("ℹ️ Não existe partida ativa.", ephemeral=True)
            return
        canais = canal_times(guild, partida)
        linhas = [
            "🎮 **Partida registrada**",
            f"🎙️ Origem: <#{partida['origem_id']}>",
            f"👥 Jogadores sorteados: {len(partida.get('participantes', {}))}",
            f"🏆 Vencedor: {partida.get('vencedor') or 'a definir'}",
        ]
        for canal in canais:
            linhas.append(f"• {canal.mention}: {len(canal.members)} pessoa(s) conectada(s)")
        await interaction.response.send_message("\n".join(linhas), ephemeral=True)




# ======================= FILA DE JOGADORES =======================
# /queue cria uma mensagem pública. Os inscritos ficam na mesma sala de espera.
# Inscrições (colunas) não são os times definitivos: o sorteio ocorre em Iniciar.


def fila_cheia(fila: dict[str, Any]) -> bool:
    return len(fila.get("inscritos", [])) == TAMANHO_TIME * int(fila["equipes"])


def fila_total_paginas(fila: dict[str, Any]) -> int:
    # Duas colunas de cinco vagas por página, mesmo para até 25 inscritos.
    capacidade = TAMANHO_TIME * int(fila["equipes"])
    return max(1, (capacidade + 9) // 10)


def fila_pagina(fila: dict[str, Any]) -> int:
    return min(max(0, int(fila.get("pagina", 0))), fila_total_paginas(fila) - 1)


def montar_embed_fila(fila: dict[str, Any]) -> discord.Embed:
    numero = int(fila["equipes"])
    inscritos = fila.get("inscritos", [])
    capacidade = TAMANHO_TIME * numero
    cheio = len(inscritos) == capacidade
    pagina = fila_pagina(fila)
    embed = discord.Embed(
        title="🎮 League of Legends | FILA PARA PARTIDA",
        description=(
            f"🎙️ **Sala de espera:** <#{fila['origem_id']}>\n"
            f"👥 **Vagas preenchidas:** {len(inscritos)}/{capacidade}\n"
            f"🏅 **Equipes:** {numero} times de {TAMANHO_TIME} jogadores\n\n"
            + (
                "✅ **Fila completa! O botão Iniciar está disponível para o dono.**"
                if cheio else "Clique em **Entrar** para participar ou **Sair** para cancelar."
            )
        ),
        color=0x22C55E if cheio else 0x5865F2,
    )
    # Sempre exatamente duas colunas. Páginas extras não criam novas colunas.
    for coluna, (emoji, nome) in enumerate((("🔴", "Vermelho"), ("🔵", "Azul"))):
        inicio = pagina * 10 + coluna * TAMANHO_TIME
        linhas = []
        for indice in range(inicio, inicio + TAMANHO_TIME):
            if indice >= capacidade:
                linhas.append("▫️ Sem vaga")
            elif indice < len(inscritos):
                linhas.append(f"<@{inscritos[indice]}>")
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
            embed.color = 0xF59E0B
            await mensagem.edit(embed=embed, view=None, attachments=mensagem.attachments)
        else:
            await mensagem.edit(
                embed=montar_embed_fila(fila), view=FilaView(
                    cheia=fila_cheia(fila), pagina=fila_pagina(fila), paginas=fila_total_paginas(fila)
                ),
                attachments=mensagem.attachments,
            )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        log.warning("Não foi possível atualizar a mensagem da fila: %s", exc)


def valida_fila_interacao(inter: discord.Interaction) -> dict[str, Any] | None:
    if inter.guild_id is None or not inter.message:
        return None
    fila = filas.get(str(inter.guild_id))
    if (not fila or fila.get("status") != "aberta"
            or fila.get("message_id") != inter.message.id):
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
                attachments=inter.message.attachments,
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
                attachments=inter.message.attachments,
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
                attachments=inter.message.attachments,
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
            if str(guild.id) in partidas:
                await inter.followup.send(
                    "⚠️ Uma partida já está ativa. Finalize-a antes de iniciar outra."
                )
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
            times = {
                chave: jogadores[i * TAMANHO_TIME:(i + 1) * TAMANHO_TIME]
                for i, (chave, _emoji, _nome) in enumerate(EQUIPES[:fila["equipes"]])
            }

            fila["status"] = "iniciando"
            salvar_filas()
            partida = {
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
            partidas[str(guild.id)] = partida
            salvar_partidas()
            try:
                categoria = await guild.create_category(
                    "🔒 PARTIDA EM ANDAMENTO", reason="Queue: início da partida"
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
                    partidas.pop(str(guild.id), None)
                    salvar_partidas()
                    fila["status"] = "aberta"
                    salvar_filas()
                    await atualizar_mensagem_fila(guild, fila)
                else:
                    # Preserva o registro parcial para /partida_cancelar.
                    filas.pop(str(guild.id), None)
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
            filas.pop(str(guild.id), None)
            salvar_filas()
            await atualizar_mensagem_fila(guild, fila, fechada=True)
            embed = discord.Embed(
                title="🎮 League of Legends | EQUIPES SORTEADAS",
                description=(
                    f"🔒 Cada time tem um canal privado: apenas membros sorteados "
                    f"podem conectar.\n🎙️ Canal original: {origem.mention}\n"
                    f"↪️ {movidos}/{len(jogadores)} jogadores movidos."
                ),
                color=0x5865F2,
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
    def __init__(self, usuario_id: int, guild_id: int) -> None:
        super().__init__(timeout=180)
        self.usuario_id = usuario_id
        self.guild_id = guild_id
        partida = partidas.get(str(guild_id)) or {}
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
            partida = partidas.get(str(inter.guild.id))
            if not partida or partida.get("vencedor") is not None:
                await inter.followup.send("⚠️ A partida já terminou ou está sendo finalizada.")
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
        partida = partidas.get(str(inter.guild.id))
        if (not partida or not inter.message or
                partida.get("assistir_message_id") != inter.message.id):
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
            view=EscolhaEquipeAssistir(inter.user.id, inter.guild.id),
            ephemeral=True,
        )


@bot.tree.command(name="queue", description="Abre uma fila com 5 jogadores por equipe e sorteio ao iniciar")
@app_commands.describe(
    equipes="Número de equipes (de 2 a 6, com 5 jogadores em cada)",
    canal="Sala de voz de espera; se vazio, usa a sala em que você está",
    imagem="Foto/GIF para salvar como imagem padrão da fila (opcional)",
    link_imagem="Link HTTPS direto para salvar como imagem padrão (opcional)",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def queue(
    inter: discord.Interaction,
    equipes: app_commands.Range[int, 2, 6] = 2,
    canal: discord.VoiceChannel | None = None,
    imagem: discord.Attachment | None = None,
    link_imagem: str | None = None,
) -> None:
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
        if str(guild.id) in filas:
            await inter.followup.send("⚠️ Já existe uma fila aberta neste servidor.", ephemeral=True)
            return
        if str(guild.id) in partidas:
            await inter.followup.send("⚠️ Finalize a partida atual antes de abrir uma fila.", ephemeral=True)
            return
        if imagem_nova:
            try:
                imagem_nome = guardar_imagem_fila(guild.id, imagem_dados)
            except (OSError, ValueError) as exc:
                log.warning("Falha ao salvar imagem da fila: %s", exc)
                await inter.followup.send("⚠️ Não consegui salvar a imagem em /data.", ephemeral=True)
                return
        else:
            padrao = imagem_fila_salva(guild.id)
            if padrao is not None:
                imagem_dados, imagem_nome = padrao

        fila: dict[str, Any] = {
            "equipes": equipes,
            "pagina": 0,
            "origem_id": canal.id,
            "channel_id": inter.channel.id,
            "message_id": None,
            "inscritos": [],
            "status": "aberta",
            "imagem_nome": imagem_nome,
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
        fila["message_id"] = mensagem.id
        filas[str(guild.id)] = fila
        salvar_filas()


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
        except (OSError, ValueError) as exc:
            log.warning("Erro ao salvar imagem padrão: %s", exc)
            await inter.followup.send("⚠️ Não consegui salvar em /data.", ephemeral=True)
            return

        alterada = False
        fila = filas.get(str(guild.id))
        if fila and fila.get("status") == "aberta":
            canal = guild.get_channel(fila.get("channel_id"))
            if isinstance(canal, discord.TextChannel):
                try:
                    mensagem = await canal.fetch_message(fila["message_id"])
                    anterior = fila.get("imagem_nome")
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
                    log.warning("Não consegui trocar imagem do painel ativo: %s", exc)

    await inter.followup.send(
        "✅ **Imagem/GIF padrão salvo permanentemente!** "
        "As próximas filas já usarão essa imagem automaticamente."
        + (" O painel aberto também foi atualizado." if alterada else ""),
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
        fila = filas.get(str(membro.guild.id))
        if not fila or fila.get("status") != "aberta":
            return
        if membro.id not in fila.get("inscritos", []):
            return
        if depois.channel and depois.channel.id == fila["origem_id"]:
            return
        fila["inscritos"].remove(membro.id)
        salvar_filas()
        await atualizar_mensagem_fila(membro.guild, fila)


@bot.tree.command(name="queue_cancelar", description="Cancela a fila antes de iniciar a partida")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def queue_cancelar(inter: discord.Interaction) -> None:
    if inter.guild is None or not autorizado(inter):
        await inter.response.send_message("⛔ Sem permissão.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True)
    async with trava_servidor(inter.guild.id):
        fila = filas.get(str(inter.guild.id))
        if not fila or fila.get("status") != "aberta":
            await inter.followup.send("ℹ️ Não há fila aberta.")
            return
        fila["status"] = "cancelada"
        await atualizar_mensagem_fila(inter.guild, fila, fechada=True)
        filas.pop(str(inter.guild.id), None)
        salvar_filas()
        await inter.followup.send("✅ Fila cancelada.")


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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina DISCORD_TOKEN no arquivo .env antes de executar o bot.")
    bot.run(TOKEN)
