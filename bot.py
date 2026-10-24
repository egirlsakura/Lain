from __future__ import annotations

import asyncio
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
ARQUIVO_PARTIDAS = BASE / "partidas.json"

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
travas: dict[int, asyncio.Lock] = {}


def trava_servidor(guild_id: int) -> asyncio.Lock:
    if guild_id not in travas:
        travas[guild_id] = asyncio.Lock()
    return travas[guild_id]


def salvar_partidas() -> None:
    temporario = ARQUIVO_PARTIDAS.with_suffix(".json.tmp")
    temporario.write_text(json.dumps(partidas, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(ARQUIVO_PARTIDAS)


def buscar_voz(guild: discord.Guild, canal_id: int | None) -> discord.VoiceChannel | None:
    canal = guild.get_channel(canal_id) if canal_id is not None else None
    return canal if isinstance(canal, discord.VoiceChannel) else None


def canal_times(guild: discord.Guild, partida: dict[str, Any]) -> list[discord.VoiceChannel]:
    encontrados = []
    for chave in ("vermelho_id", "azul_id"):
        canal = buscar_voz(guild, partida.get(chave))
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
    canal_padrao = buscar_voz(guild, partida.get("origem_id"))

    for canal in canal_times(guild, partida):
        for membro in list(canal.members):
            cadastro = participantes.get(str(membro.id), {})
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
        if vencedor and not anterior:
            partida["vencedor"] = vencedor
            salvar_partidas()

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
            nome = "🔴 VERMELHO" if resultado == "vermelho" else "🔵 AZUL"
            mensagem = f"🏆 **O TIME {nome} VENCEU!**\n"
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
    ):
        await interaction.response.send_message(
            "⚠️ O bot precisa das permissões **Gerenciar Canais** e **Mover Membros**.",
            ephemeral=True,
        )
        return
    if not eu.permissions_in(canal).move_members:
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
                "🔴 Time Vermelho", category=categoria, reason="Início da partida"
            )
            partida["vermelho_id"] = canal_vermelho.id
            salvar_partidas()
            canal_azul = await guild.create_voice_channel(
                "🔵 Time Azul", category=categoria, reason="Início da partida"
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
