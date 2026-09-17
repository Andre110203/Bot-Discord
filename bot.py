import asyncio
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Forzar a Python a emitir la salida de consola sin búfer
sys.stdout.reconfigure(line_buffering=True)

load_dotenv(Path(__file__).resolve().parent / ".env")

GUILD_ID = int(os.getenv("GUILD_ID", "1429125327170441251"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
_commands_synced = False


@bot.event
async def on_ready() -> None:
    global _commands_synced
    print(f"✅ Bot encendido como {bot.user} (ID: {bot.user.id})", flush=True)
    if _commands_synced:
        return
    connected = list(bot.guilds)
    if not connected:
        print("⚠️ El bot no pertenece a ningún servidor. Debes invitarlo antes de sincronizar comandos.", flush=True)
        return
    selected = bot.get_guild(GUILD_ID)
    if selected is None and len(connected) == 1:
        selected = connected[0]
        print(f"⚠️ Aviso: GUILD_ID no coincide. Se usará {selected.name} (ID: {selected.id}).", flush=True)
    elif selected is None:
        print("❌ GUILD_ID no coincide con los servidores disponibles:", flush=True)
        for item in connected:
            print(f"- {item.name}: {item.id}", flush=True)
        return
    guild = discord.Object(id=selected.id)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        _commands_synced = True
        print(f"✅ Sincronizados {len(synced)} comando(s) en el servidor.", flush=True)
    except Exception as exc:
        print(f"❌ Error al sincronizar comandos: {exc}", flush=True)


async def load_extensions() -> None:
    cogs_path = Path(__file__).resolve().parent / "cogs"
    if not cogs_path.exists():
        print("⚠️ No se encontró la carpeta 'cogs'.", flush=True)
        return

    for module in cogs_path.glob("*.py"):
        if module.name.startswith("_"):
            continue
        try:
            await bot.load_extension(f"cogs.{module.stem}")
            print(f"📦 Módulo cargado correctamente: {module.name}", flush=True)
        except Exception as exc:
            print(f"❌ ERROR al cargar el módulo {module.name}: {exc}", flush=True)


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        print("❌ ERROR CRÍTICO: Falta la variable de entorno DISCORD_TOKEN.", flush=True)
        raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN.")
    
    print("🚀 Iniciando carga de extensiones...", flush=True)
    async with bot:
        await load_extensions()
        print("🔑 Conectando con Discord...", flush=True)
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as exc:
        print(f"❌ Error de configuración: {exc}", flush=True)
    except KeyboardInterrupt:
        print("🛑 Bot detenido correctamente.", flush=True)
    except Exception as exc:
        print(f"💥 Error no controlado al iniciar: {exc}", flush=True)
