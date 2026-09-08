import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import string
import time
from contextlib import contextmanager
from pathlib import Path

import aiohttp
import discord
from aiohttp import web
from Crypto.Cipher import AES
from discord import app_commands
from discord.ext import commands, tasks


LOG_CHANNEL_NAME = os.getenv("LOG_CHANNEL_NAME", "registro-verificaciones")
VERIFIED_ROLE_NAME = os.getenv("VERIFIED_ROLE_NAME", "Verificado")
CODE_LIFETIME_SECONDS = 10 * 60
CODE_ALPHABET = string.ascii_uppercase + string.digits
BASE_URL = "https://www.habbo.es/api/public"
IMAGING_URL = "https://www.habbo.es/habbo-imaging/avatarimage"


def new_code(prefix: str) -> str:
    return prefix + "-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(7))


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class VerificationStore:
    def __init__(self) -> None:
        default_path = Path(__file__).resolve().parents[1] / "data" / "verification.sqlite3"
        self.path = Path(os.getenv("VERIFICATION_DB", str(default_path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def database(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.database() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS verified_links (
                    discord_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    habbo_unique_id TEXT NOT NULL UNIQUE,
                    habbo_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    verified_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    discord_id INTEGER NOT NULL,
                    habbo_unique_id TEXT NOT NULL,
                    habbo_name TEXT NOT NULL COLLATE NOCASE,
                    code_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER,
                    FOREIGN KEY(discord_id) REFERENCES verified_links(discord_id)
                );
                CREATE INDEX IF NOT EXISTS idx_web_challenges_habbo
                    ON web_challenges(habbo_name, expires_at, used_at);
                """
            )

    def save_link(self, discord_id: int, guild_id: int, data: dict) -> None:
        unique_id = str(data.get("uniqueId", "")).strip()
        name = str(data.get("name", "")).strip()
        if not unique_id or not name:
            raise ValueError("Habbo no entregó una identidad válida.")
        with self.database() as db:
            db.execute(
                "DELETE FROM verified_links WHERE habbo_unique_id=? OR habbo_name=? COLLATE NOCASE",
                (unique_id, name),
            )
            db.execute(
                """INSERT INTO verified_links
                   (discord_id,guild_id,habbo_unique_id,habbo_name,verified_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(discord_id) DO UPDATE SET
                     guild_id=excluded.guild_id,
                     habbo_unique_id=excluded.habbo_unique_id,
                     habbo_name=excluded.habbo_name,
                     verified_at=excluded.verified_at""",
                (discord_id, guild_id, unique_id, name, int(time.time())),
            )

    def linked_habbo(self, habbo_name: str):
        with self.database() as db:
            return db.execute(
                "SELECT * FROM verified_links WHERE habbo_name=? COLLATE NOCASE LIMIT 1",
                (habbo_name.strip(),),
            ).fetchone()

    def create_web_challenge(self, link, code: str):
        now = int(time.time())
        challenge_id = secrets.token_urlsafe(24)
        with self.database() as db:
            recent = db.execute(
                """SELECT created_at FROM web_challenges
                   WHERE discord_id=? AND created_at>? ORDER BY created_at DESC LIMIT 1""",
                (link["discord_id"], now - 60),
            ).fetchone()
            if recent:
                raise RuntimeError("Espera un minuto antes de solicitar otro código.")
            db.execute(
                "UPDATE web_challenges SET used_at=? WHERE discord_id=? AND used_at IS NULL",
                (now, link["discord_id"]),
            )
            db.execute(
                """INSERT INTO web_challenges
                   (challenge_id,discord_id,habbo_unique_id,habbo_name,code_hash,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    challenge_id,
                    link["discord_id"],
                    link["habbo_unique_id"],
                    link["habbo_name"],
                    code_hash(code),
                    now,
                    now + CODE_LIFETIME_SECONDS,
                ),
            )
        return challenge_id

    def discard_challenge(self, challenge_id: str) -> None:
        with self.database() as db:
            db.execute("DELETE FROM web_challenges WHERE challenge_id=?", (challenge_id,))

    def consume_web_challenge(self, challenge_id: str, habbo_name: str, motto: str):
        now = int(time.time())
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM web_challenges WHERE challenge_id=? LIMIT 1",
                (challenge_id,),
            ).fetchone()
            if not row or row["used_at"] is not None or row["expires_at"] < now:
                return None
            if row["habbo_name"].casefold() != habbo_name.strip().casefold():
                return None
            if not hmac.compare_digest(row["code_hash"], code_hash(motto.strip())):
                return None
            db.execute("UPDATE web_challenges SET used_at=? WHERE challenge_id=?", (now, challenge_id))
            return dict(row)


class VerificationModal(discord.ui.Modal, title="Verificación de Habbo"):
    usuario = discord.ui.TextInput(
        label="Nombre de tu Habbo",
        placeholder="Ejemplo: Andre_.",
        required=True,
        max_length=32,
    )

    def __init__(self, cog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.verify_habbo(interaction, str(self.usuario.value))


class VerificationView(discord.ui.View):
    def __init__(self, cog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Verificar mi Habbo",
        style=discord.ButtonStyle.green,
        custom_id="btn_verificar_habbo",
        emoji="🔐",
    )
    async def verify_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(VerificationModal(self.cog))


class HabboCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = VerificationStore()
        self.discord_challenges: dict[int, dict] = {}
        self.web_base_url = os.getenv("GFS_WEB_URL", "https://ejercitogfs.gt.tc").rstrip("/")
        self.api_secret = os.getenv("BOT_API_SECRET", "").strip()
        self.web_session = None

    async def cog_load(self) -> None:
        self.bot.add_view(VerificationView(self))
        if self.api_secret:
            timeout = aiohttp.ClientTimeout(total=15)
            self.web_session = aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar(unsafe=True))
            self.registration_queue.start()
            print("Conexión segura con el registro web activada.")
        else:
            print("BOT_API_SECRET no configurado: el envío de códigos web permanece desactivado.")

    async def cog_unload(self) -> None:
        if self.registration_queue.is_running():
            self.registration_queue.cancel()
        if self.web_session and not self.web_session.closed:
            await self.web_session.close()

    async def web_queue_request(self, payload: dict):
        headers = {
            "Authorization": f"Bearer {self.api_secret}",
            "X-GFS-Bot-Secret": self.api_secret,
            "Accept": "application/json",
        }
        if not self.web_session:
            raise RuntimeError("La sesión web del bot no está iniciada.")
        url = f"{self.web_base_url}/api/bot-registration-queue.php"
        last_status = 0
        last_content_type = "desconocido"
        for attempt in range(4):
            request_url = url if attempt == 0 else url + "?i=1"
            try:
                async with self.web_session.post(request_url, json=payload, headers=headers) as response:
                    body = await response.text()
                    last_status = response.status
                    last_content_type = response.headers.get("Content-Type", "desconocido")
                    try:
                        data = json.loads(body)
                    except (TypeError, ValueError):
                        values = re.findall(r'toNumbers\("([0-9a-f]+)"\)', body)
                        if len(values) >= 3:
                            key, iv, encrypted = (bytes.fromhex(value) for value in values[:3])
                            cookie = AES.new(key, AES.MODE_CBC, iv).decrypt(encrypted).hex()
                            self.web_session.cookie_jar.update_cookies({"__test": cookie}, response.url)
                            await asyncio.sleep(0.35)
                            continue
                        if attempt < 3:
                            await asyncio.sleep(0.75 * (attempt + 1))
                            continue
                        raise RuntimeError(
                            f"La web no entregó JSON (HTTP {last_status}, {last_content_type})."
                        )
                    if response.status >= 400:
                        raise RuntimeError(f"La web respondió HTTP {response.status}.")
                    if not isinstance(data, dict):
                        raise RuntimeError("La web entregó una respuesta con formato inesperado.")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= 3:
                    raise RuntimeError(f"No fue posible conectar con la web: {exc}") from exc
                await asyncio.sleep(0.75 * (attempt + 1))
        raise RuntimeError("No fue posible superar la validación del alojamiento web.")

    @tasks.loop(seconds=5)
    async def registration_queue(self) -> None:
        try:
            response = await self.web_queue_request({"action": "pull"})
            request = response.get("request") if isinstance(response, dict) else None
            if not request:
                return
            request_id = int(request["id"])
            request_token = str(request["solicitud_token"])
            habbo_name = str(request["habbo_nombre"])
            link = self.store.linked_habbo(habbo_name)
            if not link:
                await self.web_queue_request({
                    "action": "failed", "id": request_id, "request_token": request_token,
                    "message": "Este Habbo todavía no está vinculado con un Discord verificado.",
                })
                return
            code = new_code("GFS-WEB")
            try:
                discord_user = await self.bot.fetch_user(int(link["discord_id"]))
                await discord_user.send(
                    "🔐 **Registro web GFS**\n\n"
                    f"Coloca este código exactamente en tu misión de Habbo:\n`{code}`\n\n"
                    "Vuelve a la web y presiona **Verificar misión**. Caduca en 10 minutos."
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                await self.web_queue_request({
                    "action": "failed", "id": request_id, "request_token": request_token,
                    "message": "No pude enviarte el mensaje privado. Habilita los mensajes directos del servidor.",
                })
                return
            await self.web_queue_request({
                "action": "delivered",
                "id": request_id,
                "request_token": request_token,
                "discord_id": str(link["discord_id"]),
                "habbo_unique_id": link["habbo_unique_id"],
                "habbo_name": link["habbo_name"],
                "code_hash": code_hash(code),
            })
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as exc:
            print(f"Error consultando solicitudes web: {exc}")

    @registration_queue.before_loop
    async def before_registration_queue(self) -> None:
        await self.bot.wait_until_ready()

    async def fetch_habbo_user(self, username: str):
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{BASE_URL}/users", params={"name": username.strip()}) as response:
                    if response.status == 200:
                        return await response.json()
        except (aiohttp.ClientError, TimeoutError):
            return None
        return None

    async def verify_habbo(self, interaction: discord.Interaction, username: str) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        data = await self.fetch_habbo_user(username)
        if not data:
            await interaction.followup.send("❌ No fue posible encontrar ese usuario en Habbo.es.", ephemeral=True)
            return

        discord_id = interaction.user.id
        now = int(time.time())
        challenge = self.discord_challenges.get(discord_id)
        same_habbo = challenge and challenge["habbo_unique_id"] == str(data.get("uniqueId", ""))
        if not challenge or challenge["expires_at"] < now or not same_habbo:
            code = new_code("HABBO")
            self.discord_challenges[discord_id] = {
                "code": code,
                "habbo_unique_id": str(data.get("uniqueId", "")),
                "habbo_name": str(data.get("name", username)),
                "expires_at": now + CODE_LIFETIME_SECONDS,
            }
            embed = discord.Embed(
                title="🔐 Verifica tu misión de Habbo",
                description=(
                    f"Para vincular **{data['name']}**, coloca exactamente este código en tu misión:\n\n"
                    f"`{code}`\n\nGuarda el cambio y vuelve a verificar antes de 10 minutos."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if not hmac.compare_digest(str(data.get("motto", "")).strip(), challenge["code"]):
            await interaction.followup.send(
                "❌ La misión todavía no coincide exactamente con el código entregado.", ephemeral=True
            )
            return

        guild_id = interaction.guild.id if interaction.guild else 0
        self.store.save_link(discord_id, guild_id, data)
        del self.discord_challenges[discord_id]

        if interaction.guild:
            try:
                await interaction.user.edit(nick=data["name"])
            except (discord.Forbidden, discord.HTTPException):
                pass
            role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
            if role:
                try:
                    await interaction.user.add_roles(role)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        figure = data.get("figureString", "")
        embed = discord.Embed(
            title="✅ Verificación exitosa",
            description=f"Discord quedó vinculado con **{data['name']}**.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=f"{IMAGING_URL}?figure={figure}&direction=2&head_direction=3&gesture=sml&size=l")
        await interaction.followup.send(embed=embed, ephemeral=True)
        await self.send_verification_log(interaction, data)

    async def send_verification_log(self, interaction: discord.Interaction, data: dict) -> None:
        if not interaction.guild:
            return
        channel = discord.utils.get(interaction.guild.text_channels, name=LOG_CHANNEL_NAME)
        if not channel:
            return
        embed = discord.Embed(title="📋 Nueva verificación registrada", color=discord.Color.blue())
        embed.add_field(name="Usuario Discord", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="Habbo", value=f"**{data['name']}**", inline=True)
        embed.add_field(name="ID Habbo", value=f"`{data.get('uniqueId', 'N/A')}`", inline=True)
        embed.set_footer(text="Ejército GFS · Sistema de verificación")
        await channel.send(embed=embed)

    def api_authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, request.app["secret"])

    async def api_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "gfs-discord-verification"})

    async def api_create_challenge(self, request: web.Request) -> web.Response:
        if not self.api_authorized(request):
            raise web.HTTPUnauthorized()
        payload = await request.json()
        habbo_name = str(payload.get("habbo_name", "")).strip()
        if not habbo_name:
            return web.json_response({"ok": False, "message": "Falta el usuario Habbo."}, status=422)
        link = self.store.linked_habbo(habbo_name)
        if not link:
            return web.json_response({"ok": False, "message": "Este Habbo no está verificado en Discord."}, status=404)

        code = new_code("GFS-WEB")
        try:
            challenge_id = self.store.create_web_challenge(link, code)
        except RuntimeError as exc:
            return web.json_response({"ok": False, "message": str(exc)}, status=429)
        try:
            discord_user = await self.bot.fetch_user(int(link["discord_id"]))
            await discord_user.send(
                f"🔐 **Registro web GFS**\n\nColoca este código exactamente en tu misión de Habbo:\n"
                f"`{code}`\n\nCaduca en 10 minutos y solo puede utilizarse una vez."
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            self.store.discard_challenge(challenge_id)
            return web.json_response(
                {"ok": False, "message": "No pude enviar el mensaje privado. Habilita los mensajes del servidor."},
                status=409,
            )
        return web.json_response(
            {"ok": True, "challenge_id": challenge_id, "expires_in": CODE_LIFETIME_SECONDS}
        )

    async def api_verify_challenge(self, request: web.Request) -> web.Response:
        if not self.api_authorized(request):
            raise web.HTTPUnauthorized()
        payload = await request.json()
        challenge_id = str(payload.get("challenge_id", "")).strip()
        habbo_name = str(payload.get("habbo_name", "")).strip()
        motto = str(payload.get("motto", "")).strip()
        verified = self.store.consume_web_challenge(challenge_id, habbo_name, motto)
        if not verified:
            return web.json_response(
                {"ok": False, "message": "Código incorrecto, vencido o ya utilizado."}, status=422
            )
        return web.json_response(
            {
                "ok": True,
                "discord_id": str(verified["discord_id"]),
                "habbo_name": verified["habbo_name"],
                "habbo_unique_id": verified["habbo_unique_id"],
            }
        )

    @app_commands.command(name="perfil", description="Muestra el perfil público de un usuario de Habbo.")
    async def perfil(self, interaction: discord.Interaction, usuario: str) -> None:
        await interaction.response.defer()
        data = await self.fetch_habbo_user(usuario)
        if not data:
            await interaction.followup.send("❌ Usuario no encontrado.", ephemeral=True)
            return
        online = "🟢 En línea" if data.get("online") else "🔴 Desconectado"
        embed = discord.Embed(title=f"Perfil de Habbo: {data['name']}", color=discord.Color.gold())
        embed.add_field(name="Misión", value=data.get("motto") or "Sin misión", inline=False)
        embed.add_field(name="Estado", value=online)
        embed.add_field(name="Miembro desde", value=str(data.get("memberSince", "Desconocido")).split("T")[0])
        embed.set_thumbnail(url=f"{IMAGING_URL}?figure={data.get('figureString','')}&size=l")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="avatar", description="Obtiene la imagen del avatar de Habbo.")
    async def avatar(self, interaction: discord.Interaction, usuario: str) -> None:
        await interaction.response.defer()
        data = await self.fetch_habbo_user(usuario)
        if not data:
            await interaction.followup.send("❌ Usuario no encontrado.", ephemeral=True)
            return
        embed = discord.Embed(title=f"Avatar de {data['name']}", color=discord.Color.blue())
        embed.set_image(url=f"{IMAGING_URL}?figure={data.get('figureString','')}&direction=2&head_direction=2&gesture=sml&size=l")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="verificar", description="Vincula Discord con tu usuario de Habbo.")
    async def verificar(self, interaction: discord.Interaction, usuario: str) -> None:
        await self.verify_habbo(interaction, usuario)

    @app_commands.command(name="panel_verificacion", description="Publica el panel de verificación.")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel_verificacion(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🛡️ Sistema de verificación GFS",
            description="Verifica tu cuenta de Habbo para vincularla con Discord y habilitar el registro web.",
            color=discord.Color.blue(),
        )
        await interaction.channel.send(embed=embed, view=VerificationView(self))
        await interaction.response.send_message("✅ Panel publicado.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HabboCommands(bot))
