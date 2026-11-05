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
import uuid
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
RECRUIT_ROLE_NAME = os.getenv("RECRUIT_ROLE_NAME", "Recluta Inicial")
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
                CREATE TABLE IF NOT EXISTS device_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id INTEGER NOT NULL,
                    habbo_unique_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    verified_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_web_challenges_habbo
                    ON web_challenges(habbo_name, expires_at, used_at);
                CREATE INDEX IF NOT EXISTS idx_device_fingerprint ON device_logs(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_device_ip ON device_logs(ip_address);
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

    def save_device_log(self, discord_id: int, habbo_unique_id: str, fingerprint: str, ip_address: str) -> None:
        with self.database() as db:
            db.execute(
                """INSERT INTO device_logs (discord_id, habbo_unique_id, fingerprint, ip_address, verified_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (discord_id, habbo_unique_id, fingerprint, ip_address, int(time.time())),
            )

    def is_device_registered(self, fingerprint: str, ip_address: str, discord_id: int):
        with self.database() as db:
            row = db.execute(
                "SELECT * FROM device_logs WHERE (fingerprint=? OR ip_address=?) AND discord_id != ? LIMIT 1",
                (fingerprint, ip_address, discord_id),
            ).fetchone()
            return row

    def linked_habbo(self, habbo_name: str):
        with self.database() as db:
            return db.execute(
                "SELECT * FROM verified_links WHERE habbo_name=? COLLATE NOCASE LIMIT 1",
                (habbo_name.strip(),),
            ).fetchone()

    def linked_discord(self, discord_id: int):
        with self.database() as db:
            return db.execute(
                "SELECT * FROM verified_links WHERE discord_id=? LIMIT 1",
                (discord_id,),
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


class VerificationModal(discord.ui.Modal, title="Verificación de Habbo Anti-Alt"):
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
        await self.cog.start_web_verification_flow(interaction, str(self.usuario.value))


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
        self.pending_web_tokens: dict[str, dict] = {}
        self.web_base_url = os.getenv("GFS_WEB_URL", "https://ejercitogfs.gt.tc").rstrip("/")
        self.external_render_url = os.getenv("RENDER_EXTERNAL_URL", "https://bot-discord-yau4.onrender.com").rstrip("/")
        self.api_secret = os.getenv("BOT_API_SECRET", "").strip()
        self.web_session = None
        self.web_runner = None

    async def cog_load(self) -> None:
        self.bot.add_view(VerificationView(self))
        
        await self.start_web_server()

        if self.api_secret:
            timeout = aiohttp.ClientTimeout(total=15)
            self.web_session = aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar(unsafe=True))
            self.registration_queue.start()
            print("Conexión segura con el registro web activada.", flush=True)
        else:
            print("BOT_API_SECRET no configurado: el envío de códigos web permanece desactivado.", flush=True)

    async def cog_unload(self) -> None:
        if self.registration_queue.is_running():
            self.registration_queue.cancel()
        if self.web_session and not self.web_session.closed:
            await self.web_session.close()
        if self.web_runner:
            await self.web_runner.cleanup()

    async def api_health(self, _request: web.Request) -> web.Response:
        return web.Response(text="Servidor de Verificación Habbo Activo", status=200)

    def api_authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not supplied:
            supplied = request.headers.get("X-GFS-Bot-Secret", "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, self.api_secret)

    async def start_web_server(self) -> None:
        app = web.Application()
        app["secret"] = self.api_secret
        app.router.add_get("/", self.api_health)
        app.router.add_get("/health", self.api_health)
        app.router.add_post("/api/challenge/create", self.api_create_challenge)
        app.router.add_post("/api/challenge/verify", self.api_verify_challenge)
        
        app.router.add_get("/verify", self.handle_verify_page)
        app.router.add_post("/api/confirm", self.handle_api_confirm)

        port = int(os.getenv("PORT", "10000"))
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "0.0.0.0", port)
        await site.start()
        print(f"🌐 Servidor Web HTTP activo escuchando en el puerto {port}", flush=True)

    async def handle_verify_page(self, request: web.Request) -> web.Response:
        token = request.query.get("token")
        if not token or token not in self.pending_web_tokens:
            return web.Response(text="<h1>❌ Token de verificación inválido o expirado.</h1>", content_type="text/html", status=400)

        data = self.pending_web_tokens[token]
        habbo_name = data["habbo_name"]

        html_content = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Verificación Anti-Alt GFS</title>
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #1e1e2e; color: #fff; text-align: center; padding: 40px 10px; }}
                .card {{ background: #2b2b3d; padding: 30px; border-radius: 12px; display: inline-block; max-width: 420px; box-shadow: 0 4px 15px rgba(0,0,0,0.3); }}
                h2 {{ color: #5865F2; margin-bottom: 10px; }}
                button {{ background: #43b581; color: white; border: none; padding: 14px 24px; font-size: 16px; font-weight: bold; border-radius: 6px; cursor: pointer; transition: background 0.2s; }}
                button:hover {{ background: #3ca374; }}
                #status {{ margin-top: 20px; font-size: 15px; font-weight: bold; word-break: break-word; }}
            </style>
            <script src="https://openfpcdn.io/fingerprintjs/v3/umd.min.js"></script>
        </head>
        <body>
            <div class="card">
                <h2>🛡️ Verificación Militar GFS</h2>
                <p>Usuario a validar: <strong>{habbo_name}</strong></p>
                <p style="font-size: 13px; color: #b9bbbe;">Se escaneará tu dispositivo e IP para prevenir multicuentas en el ejército.</p>
                <br>
                <button id="btn-verify" onclick="processVerification()">Validar Mi Dispositivo</button>
                <div id="status"></div>
            </div>

            <script>
                async function processVerification() {{
                    const statusDiv = document.getElementById("status");
                    const btn = document.getElementById("btn-verify");
                    btn.disabled = true;
                    statusDiv.style.color = "#ffffff";
                    statusDiv.innerHTML = "⏳ Generando huella digital...";

                    try {{
                        const fp = await FingerprintJS.load();
                        const result = await fp.get();
                        const visitorId = result.visitorId;

                        statusDiv.innerHTML = "⏳ Comprobando con el servidor...";

                        const response = await fetch("/api/confirm", {{
                            method: "POST",
                            headers: {{ "Content-Type": "application/json" }},
                            body: JSON.stringify({{
                                token: "{token}",
                                fingerprint: visitorId
                            }})
                        }});

                        const resData = await response.json();
                        if (response.ok) {{
                            statusDiv.style.color = "#43b581";
                            statusDiv.innerHTML = "✅ " + resData.message;
                        }} else {{
                            statusDiv.style.color = "#f04747";
                            statusDiv.innerHTML = "❌ " + resData.error;
                            btn.disabled = false;
                        }}
                    }} catch (err) {{
                        statusDiv.style.color = "#f04747";
                        statusDiv.innerHTML = "❌ Error en el proceso: " + err.message;
                        btn.disabled = false;
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=html_content, content_type="text/html")

    async def handle_api_confirm(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "JSON no válido."}, status=400)

        token = body.get("token")
        fingerprint = str(body.get("fingerprint", "")).strip()

        peername = request.transport.get_extra_info('peername')
        client_ip = request.headers.get("x-forwarded-for", peername[0] if peername else "0.0.0.0").split(",")[0].strip()

        if not token or token not in self.pending_web_tokens:
            return web.json_response({"error": "El token de verificación ha expirado o no existe."}, status=400)

        if not fingerprint:
            return web.json_response({"error": "No se pudo obtener la huella digital del dispositivo."}, status=400)

        token_data = self.pending_web_tokens[token]
        discord_id = token_data["discord_id"]
        habbo_name = token_data["habbo_name"]
        guild_id = token_data["guild_id"]

        alt_found = self.store.is_device_registered(fingerprint, client_ip, discord_id)
        if alt_found:
            return web.json_response({
                "error": "Se detectó que este dispositivo o red IP ya fue usado por otra cuenta militar."
            }, status=403)

        habbo_data = await self.fetch_habbo_user(habbo_name)
        if not habbo_data:
            return web.json_response({"error": f"La cuenta de Habbo '{habbo_name}' no existe."}, status=404)

        self.store.save_link(discord_id, guild_id, habbo_data)
        self.store.save_device_log(discord_id, habbo_data.get("uniqueId", ""), fingerprint, client_ip)
        del self.pending_web_tokens[token]

        guild = self.bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(discord_id)
            if member:
                try:
                    await member.edit(nick=habbo_data["name"])
                except (discord.Forbidden, discord.HTTPException):
                    pass
                
                role_verified = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
                if role_verified:
                    try:
                        await member.add_roles(role_verified)
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                
                role_recruit = discord.utils.get(guild.roles, name=RECRUIT_ROLE_NAME)
                if role_recruit:
                    try:
                        await member.add_roles(role_recruit)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        self.bot.loop.create_task(self.send_web_verification_log(guild_id, discord_id, habbo_data, client_ip, fingerprint))

        return web.json_response({"message": "¡Verificación de dispositivo completada con éxito! Ya puedes volver a Discord."})

    async def send_web_verification_log(self, guild_id: int, discord_id: int, data: dict, ip_address: str, fingerprint: str) -> None:
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if not channel:
            return

        figure = data.get("figureString", "")
        embed = discord.Embed(
            title="🛡️ Nueva Verificación Anti-Alt Completada",
            description=f"El recluta <@{discord_id}> ha validado su dispositivo correctamente.",
            color=discord.Color.green()
        )
        embed.add_field(name="Usuario Discord", value=f"<@{discord_id}> (`{discord_id}`)", inline=False)
        embed.add_field(name="Habbo Registrado", value=f"**{data['name']}**", inline=True)
        embed.add_field(name="ID Habbo", value=f"`{data.get('uniqueId', 'N/A')}`", inline=True)
        embed.add_field(name="IP capturada", value=f"`{ip_address}`", inline=True)
        embed.add_field(name="Fingerprint ID", value=f"`{fingerprint[:12]}...`", inline=True)
        embed.set_thumbnail(url=f"{IMAGING_URL}?figure={figure}&direction=2&head_direction=3&
