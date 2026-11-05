import os
import uuid
import asyncio
import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

REGISTERED_FINGERPRINTS = {}
REGISTERED_IPS = {}
REGISTERED_HABBO_ACCOUNTS = {}
PENDING_TOKENS = {}

# 1. Leer el puerto que asigna Render automáticamente (por defecto 8080 si pruebas local)
WEB_PORT = int(os.getenv("PORT", 8080))

# 2. Leer la URL pública de tu app en Render (la configuraremos en las variables de entorno)
DOMAIN_OR_IP = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8080")

class WebVerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.web_runner = None
        self.bot.loop.create_task(self.start_web_server())

    async def start_web_server(self):
        app = web.Application()
        app.router.add_get("/verify", self.handle_verify_page)
        app.router.add_post("/api/confirm", self.handle_api_confirm)

        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        
        # En Render es fundamental escuchar en 0.0.0.0
        site = web.TCPSite(self.web_runner, "0.0.0.0", WEB_PORT)
        await site.start()
        print(f"🌐 Servidor Web activo en puerto {WEB_PORT}", flush=True)

    def cog_unload(self):
        if self.web_runner:
            asyncio.create_task(self.web_runner.cleanup())

    @app_commands.command(name="verificar_web", description="Verifica tu usuario de Habbo para el ejército.")
    async def verificar_web(self, interaction: discord.Interaction, habbo_name: str):
        await interaction.response.defer(ephemeral=True)
        discord_id = interaction.user.id

        if any(d_id == discord_id for d_id in REGISTERED_HABBO_ACCOUNTS.values()):
            return await interaction.followup.send("❌ Tu cuenta de Discord ya tiene un usuario registrado.", ephemeral=True)

        token = str(uuid.uuid4())
        PENDING_TOKENS[token] = {
            "discord_id": discord_id,
            "habbo_name": habbo_name,
            "guild_id": interaction.guild_id
        }

        verify_url = f"{DOMAIN_OR_IP}/verify?token={token}"

        embed = discord.Embed(
            title="🛡️ Verificación Militar",
            description=f"Hola **{interaction.user.display_name}**, para vincular a **{habbo_name}** haz clic abajo:\n\n🔗 [Verificar mi Dispositivo]({verify_url})",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def handle_verify_page(self, request: web.Request):
        token = request.query.get("token")
        if not token or token not in PENDING_TOKENS:
            return web.Response(text="<h1>❌ Token inválido o expirado.</h1>", content_type="text/html", status=400)

        data = PENDING_TOKENS[token]
        habbo_name = data["habbo_name"]

        html_content = f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <title>Verificación Militar</title>
            <style>
                body {{ font-family: sans-serif; background-color: #1e1e2e; color: #fff; text-align: center; padding: 50px; }}
                .card {{ background: #2b2b3d; padding: 30px; border-radius: 10px; display: inline-block; max-width: 400px; }}
                button {{ background: #7289da; color: white; border: none; padding: 12px 20px; font-size: 16px; border-radius: 5px; cursor: pointer; }}
                #status {{ margin-top: 15px; font-weight: bold; }}
            </style>
            <script src="https://openfpcdn.io/fingerprintjs/v3/umd.min.js"></script>
        </head>
        <body>
            <div class="card">
                <h2>🛡️ Sistema Anti-Alt</h2>
                <p>Verificando a: <strong>{habbo_name}</strong></p>
                <button id="btn-verify" onclick="processVerification()">Validar Miembro</button>
                <div id="status"></div>
            </div>
            <script>
                async function processVerification() {{
                    const statusDiv = document.getElementById("status");
                    document.getElementById("btn-verify").disabled = true;
                    statusDiv.innerHTML = "⏳ Escaneando dispositivo...";

                    try {{
                        const fp = await FingerprintJS.load();
                        const result = await fp.get();

                        const response = await fetch("/api/confirm", {{
                            method: "POST",
                            headers: {{ "Content-Type": "application/json" }},
                            body: JSON.stringify({{ token: "{token}", fingerprint: result.visitorId }})
                        }});

                        const resData = await response.json();
                        statusDiv.style.color = response.ok ? "#43b581" : "#f04747";
                        statusDiv.innerHTML = response.ok ? "✅ " + resData.message : "❌ " + resData.error;
                    }} catch (err) {{
                        statusDiv.style.color = "#f04747";
                        statusDiv.innerHTML = "❌ Error: " + err.message;
                    }}
                }}
            </script>
        </body>
        </html>
        """
        return web.Response(text=html_content, content_type="text/html")

    async def handle_api_confirm(self, request: web.Request):
        body = await request.json()
        token, fingerprint = body.get("token"), body.get("fingerprint")

        peername = request.transport.get_extra_info('peername')
        # En Render, la IP real del usuario viene en el header 'x-forwarded-for'
        client_ip = request.headers.get("x-forwarded-for", peername[0] if peername else "0.0.0.0").split(",")[0].strip()

        if not token or token not in PENDING_TOKENS:
            return web.json_response({"error": "Token expirado."}, status=400)

        token_data = PENDING_TOKENS[token]
        discord_id, habbo_name = token_data["discord_id"], token_data["habbo_name"]

        # Control Anti-Alt por Fingerprint e IP
        if fingerprint in REGISTERED_FINGERPRINTS and REGISTERED_FINGERPRINTS[fingerprint] != discord_id:
            return web.json_response({"error": "Este dispositivo ya fue registrado por otra cuenta."}, status=403)

        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://www.habbo.es/api/public/users?name={habbo_name}") as resp:
                if resp.status != 200:
                    return web.json_response({"error": "Usuario de Habbo no encontrado."}, status=404)
                habbo_data = await resp.json()

        REGISTERED_FINGERPRINTS[fingerprint] = discord_id
        REGISTERED_IPS[client_ip] = discord_id
        REGISTERED_HABBO_ACCOUNTS[habbo_data.get("uniqueId")] = discord_id
        del PENDING_TOKENS[token]

        return web.json_response({"message": "¡Verificado con éxito! Puedes regresar a Discord."})

async def setup(bot: commands.Bot):
    await bot.add_cog(WebVerificationCog(bot))
