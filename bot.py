import asyncio
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env")


GUILD_ID = int(os.getenv("GUILD_ID", "1429125327170441251"))

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
_commands_synced = False


@bot.event
async def on_ready() -> None:
    global _commands_synced
    print(f"Bot encendido como {bot.user} (ID: {bot.user.id})")
    if _commands_synced:
        return
    connected = list(bot.guilds)
    if not connected:
        print("El bot no pertenece a ningún servidor. Debes invitarlo antes de sincronizar comandos.")
        return
    selected = bot.get_guild(GUILD_ID)
    if selected is None and len(connected) == 1:
        selected = connected[0]
        print(f"Aviso: GUILD_ID no coincide. Se usará {selected.name} (ID: {selected.id}).")
    elif selected is None:
        print("GUILD_ID no coincide con los servidores disponibles:")
        for item in connected:
            print(f"- {item.name}: {item.id}")
        return
    guild = discord.Object(id=selected.id)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        _commands_synced = True
        print(f"Sincronizados {len(synced)} comando(s) en el servidor.")
    except Exception as exc:
        print(f"Error al sincronizar comandos: {exc}")


async def load_extensions() -> None:
    cogs_path = Path(__file__).resolve().parent / "cogs"
    for module in cogs_path.glob("*.py"):
        if module.name.startswith("_"):
            continue
        await bot.load_extension(f"cogs.{module.stem}")
        print(f"Módulo cargado: {module.name}")


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Falta la variable de entorno DISCORD_TOKEN.")
    async with bot:
        await load_extensions()
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as exc:
        print(f"Error de configuración: {exc}")
    except KeyboardInterrupt:
        print("Bot detenido correctamente.")
