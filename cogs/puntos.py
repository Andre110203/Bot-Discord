import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

ACTIVITIES_CHANNEL_NAME = os.getenv("ACTIVITIES_CHANNEL_NAME", "registro-actividades")
ACTIVITIES_CHANNEL_ID = os.getenv("ACTIVITIES_CHANNEL_ID", "").strip()

POINTS_PARTICIPANT = 1
POINTS_WINNER = 3
POINTS_GUIDE = 5


class PointsStore:
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
                CREATE TABLE IF NOT EXISTS military_points (
                    discord_id INTEGER PRIMARY KEY,
                    habbo_name TEXT NOT NULL COLLATE NOCASE,
                    points_participation INTEGER NOT NULL DEFAULT 0,
                    points_wins INTEGER NOT NULL DEFAULT 0,
                    points_guide INTEGER NOT NULL DEFAULT 0,
                    points_adjusted INTEGER NOT NULL DEFAULT 0,
                    points_spent INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity_logs (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    guide_discord_id INTEGER NOT NULL,
                    guide_habbo TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    activity_date TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS redemptions_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id INTEGER NOT NULL,
                    habbo_name TEXT NOT NULL,
                    membership_name TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS board_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL
                );
                """
            )

    def get_verified_user(self, habbo_name: str):
        with self.database() as db:
            return db.execute(
                "SELECT * FROM verified_links WHERE habbo_name=? COLLATE NOCASE LIMIT 1",
                (habbo_name.strip(),),
            ).fetchone()

    def get_verified_by_discord(self, discord_id: int):
        with self.database() as db:
            return db.execute(
                "SELECT * FROM verified_links WHERE discord_id=? LIMIT 1",
                (discord_id,),
            ).fetchone()

    def is_activity_processed(self, message_id: int) -> bool:
        with self.database() as db:
            row = db.execute(
                "SELECT 1 FROM activity_logs WHERE message_id=? LIMIT 1",
                (message_id,),
            ).fetchone()
            return bool(row)

    def record_activity(
        self,
        message_id: int,
        guild_id: int,
        guide: dict,
        game_name: str,
        activity_date: str,
        participants: list[dict],
        winners: list[dict],
        summary_text: str,
    ) -> None:
        now = int(time.time())
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM activity_logs WHERE message_id=?", (message_id,)).fetchone()
            if exists:
                return

            self._upsert_points(db, guide["discord_id"], guide["habbo_name"], guide_pts=POINTS_GUIDE, now=now)

            for p in participants:
                self._upsert_points(db, p["discord_id"], p["habbo_name"], part_pts=POINTS_PARTICIPANT, now=now)

            for w in winners:
                self._upsert_points(db, w["discord_id"], w["habbo_name"], win_pts=POINTS_WINNER, now=now)

            db.execute(
                """INSERT INTO activity_logs
                   (message_id, guild_id, guide_discord_id, guide_habbo, game_name, activity_date, summary_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, guild_id, guide["discord_id"], guide["habbo_name"], game_name, activity_date, summary_text, now),
            )

    def _upsert_points(
        self,
        db: sqlite3.Connection,
        discord_id: int,
        habbo_name: str,
        part_pts: int = 0,
        win_pts: int = 0,
        guide_pts: int = 0,
        adj_pts: int = 0,
        spent_pts: int = 0,
        now: int = 0,
    ) -> None:
        db.execute(
            """INSERT INTO military_points
               (discord_id, habbo_name, points_participation, points_wins, points_guide, points_adjusted, points_spent, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
                 habbo_name = excluded.habbo_name,
                 points_participation = military_points.points_participation + excluded.points_participation,
                 points_wins = military_points.points_wins + excluded.points_wins,
                 points_guide = military_points.points_guide + excluded.points_guide,
                 points_adjusted = military_points.points_adjusted + excluded.points_adjusted,
                 points_spent = military_points.points_spent + excluded.points_spent,
                 updated_at = excluded.updated_at""",
            (discord_id, habbo_name, part_pts, win_pts, guide_pts, adj_pts, spent_pts, now or int(time.time())),
        )

    def get_user_balance(self, discord_id: int):
        with self.database() as db:
            row = db.execute("SELECT * FROM military_points WHERE discord_id=? LIMIT 1", (discord_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            earned = data["points_participation"] + data["points_wins"] + data["points_guide"] + data["points_adjusted"]
            data["earned"] = earned
            data["available"] = earned - data["points_spent"]
            return data

    def redeem_membership(self, discord_id: int, habbo_name: str, membership_name: str, cost: int, admin_id: int) -> bool:
        now = int(time.time())
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM military_points WHERE discord_id=? LIMIT 1", (discord_id,)).fetchone()
            if not row:
                return False
            earned = row["points_participation"] + row["points_wins"] + row["points_guide"] + row["points_adjusted"]
            available = earned - row["points_spent"]
            if available < cost:
                return False
            self._upsert_points(db, discord_id, habbo_name, spent_pts=cost, now=now)
            db.execute(
                """INSERT INTO redemptions_log (discord_id, habbo_name, membership_name, cost, admin_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (discord_id, habbo_name, membership_name, cost, admin_id, now),
            )
            return True

    def adjust_points(self, discord_id: int, habbo_name: str, amount: int) -> None:
        with self.database() as db:
            self._upsert_points(db, discord_id, habbo_name, adj_pts=amount, now=int(time.time()))

    def get_leaderboard(self) -> list[dict]:
        with self.database() as db:
            rows = db.execute("SELECT * FROM military_points").fetchall()
            result = []
            for r in rows:
                d = dict(r)
                earned = d["points_participation"] + d["points_wins"] + d["points_guide"] + d["points_adjusted"]
                available = earned - d["points_spent"]
                if earned > 0 or available > 0:
                    d["earned"] = earned
                    d["available"] = available
                    result.append(d)
            result.sort(key=lambda x: (x["available"], x["earned"]), reverse=True)
            return result

    def save_board_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.database() as db:
            db.execute(
                """INSERT INTO board_config (guild_id, channel_id, message_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                     channel_id=excluded.channel_id,
                     message_id=excluded.message_id""",
                (guild_id, channel_id, message_id),
            )

    def get_board_message(self, guild_id: int):
        with self.database() as db:
            return db.execute("SELECT * FROM board_config WHERE guild_id=? LIMIT 1", (guild_id,)).fetchone()

    def reset_monthly_cycle(self) -> None:
        with self.database() as db:
            db.execute("DELETE FROM military_points")
            db.execute("DELETE FROM activity_logs")


class LeaderboardView(discord.ui.View):
    def __init__(self, cog, page: int = 0) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.page = page

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary, emoji="⬅️", custom_id="pts_prev")
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        new_page = max(0, self.page - 1)
        embed = self.cog.build_leaderboard_embed(interaction.guild, new_page)
        await interaction.response.edit_message(embed=embed, view=LeaderboardView(self.cog, new_page))

    @discord.ui.button(label="Siguiente", style=discord.ButtonStyle.secondary, emoji="➡️", custom_id="pts_next")
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        ranking = self.cog.store.get_leaderboard()
        max_page = max(0, (len(ranking) - 1) // 15)
        new_page = min(max_page, self.page + 1)
        embed = self.cog.build_leaderboard_embed(interaction.guild, new_page)
        await interaction.response.edit_message(embed=embed, view=LeaderboardView(self.cog, new_page))

    @discord.ui.button(label="Consultar Mi Saldo", style=discord.ButtonStyle.primary, emoji="💳", custom_id="pts_balance")
    async def balance_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        link = self.cog.store.get_verified_by_discord(interaction.user.id)
        if not link:
            return await interaction.response.send_message(
                "❌ No estás verificado en la base militar. Completa tu registro primero.", ephemeral=True
            )
        bal = self.cog.store.get_user_balance(interaction.user.id)
        if not bal:
            return await interaction.response.send_message(
                f"🎖️ **{link['habbo_name']}**, aún no registras puntos en el ciclo mensual actual (**0 pts**).",
                ephemeral=True,
            )
        embed = discord.Embed(
            title=f"💳 Estado de Cuenta Militar — {link['habbo_name']}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="💰 Puntos Disponibles (Canjeables)", value=f"**{bal['available']} pts**", inline=False)
        embed.add_field(name="🎮 Participación (+1)", value=f"{bal['points_participation']} pts", inline=True)
        embed.add_field(name="🏆 Victorias (+3)", value=f"{bal['points_wins']} pts", inline=True)
        embed.add_field(name="🎖️ Guía/Encargado (+5)", value=f"{bal['points_guide']} pts", inline=True)
        embed.add_field(name="🛍️ Canjeados en Membresías", value=f"{bal['points_spent']} pts", inline=True)
        embed.add_field(name="📊 Total Acumulado del Mes", value=f"{bal['earned']} pts", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class PointsCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = PointsStore()

    async def cog_load(self) -> None:
        self.bot.add_view(LeaderboardView(self, 0))

    def is_activities_channel(self, channel: discord.abc.GuildChannel) -> bool:
        if not channel:
            return False
        if ACTIVITIES_CHANNEL_ID and ACTIVITIES_CHANNEL_ID.isdigit():
            return channel.id == int(ACTIVITIES_CHANNEL_ID)
        return ACTIVITIES_CHANNEL_NAME.lower() in getattr(channel, "name", "").lower()

    def parse_activity_template(self, text: str):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None

        date_str = None
        game_str = None
        guide_str = None
        section = None
        raw_participants = []
        raw_winners = []

        for line in lines:
            clean = line.replace("*", "").strip()
            lower = clean.lower()

            if "fecha:" in lower:
                date_str = clean.split(":", 1)[1].strip()
                section = None
            elif "juego:" in lower:
                game_str = clean.split(":", 1)[1].strip()
                section = None
            elif "encargado" in lower and ":" in lower:
                guide_str = clean.split(":", 1)[1].strip()
                section = None
            elif "participantes" in lower:
                section = "participants"
            elif "ganadores" in lower:
                section = "winners"
            elif section == "participants":
                name = re.sub(r"^[\-\•\*\d\.\)\s]+", "", clean).strip()
                if name:
                    raw_participants.append(name)
            elif section == "winners":
                if ":" in clean:
                    name = clean.split(":", 1)[1].strip()
                else:
                    name = re.sub(r"^[\-\•\*\d\.\)\s]+", "", clean).strip()
                if name and name.lower() not in ("nadie", "ninguno", "-", "n/a"):
                    raw_winners.append(name)

        if not (date_str and game_str and guide_str and (raw_participants or raw_winners)):
            return None

        return {
            "date": date_str,
            "game": game_str,
            "guide": guide_str,
            "participants": raw_participants,
            "winners": raw_winners,
        }

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not self.is_activities_channel(message.channel):
            return
        await self.process_activity_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, _before: discord.Message, after: discord.Message) -> None:
        if after.author.bot or not after.guild:
            return
        if not self.is_activities_channel(after.channel):
            return
        await self.process_activity_message(after)

    async def process_activity_message(self, message: discord.Message) -> None:
        if self.store.is_activity_processed(message.id):
            return

        parsed = self.parse_activity_template(message.content)
        if not parsed:
            return

        unverified = []

        guide_row = self.store.get_verified_user(parsed["guide"])
        if not guide_row:
            unverified.append(f"Encargado(a): `{parsed['guide']}`")

        seen_part = set()
        valid_participants = []
        for p_name in parsed["participants"]:
            key = p_name.casefold()
            if key in seen_part:
                continue
            seen_part.add(key)
            row = self.store.get_verified_user(p_name)
            if not row:
                unverified.append(f"Participante: `{p_name}`")
            else:
                valid_participants.append(dict(row))

        valid_winners = []
        for w_name in parsed["winners"]:
            row = self.store.get_verified_user(w_name)
            if not row:
                unverified.append(f"Ganador(a): `{w_name}`")
            else:
                valid_winners.append(dict(row))

        if unverified:
            try:
                await message.clear_reactions()
                await message.add_reaction("⚠️")
            except discord.HTTPException:
                pass
            error_list = "\n".join(f"• {item}" for item in dict.fromkeys(unverified))
            await message.reply(
                "⚠️ **Registro en pausa — Personal no verificado detectado**\n"
                "Los siguientes nombres no coinciden con ningún oficial verificado en la base de datos:\n"
                f"{error_list}\n\n"
                "*Edita tu mensaje corrigiendo la ortografía o pide al recluta que se verifique. El bot lo procesará automáticamente al editarlo.*",
                delete_after=45,
            )
            return

        summary = f"Guía: {guide_row['habbo_name']} | Part: {len(valid_participants)} | Rondas: {len(valid_winners)}"
        self.store.record_activity(
            message_id=message.id,
            guild_id=message.guild.id,
            guide=dict(guide_row),
            game_name=parsed["game"],
            activity_date=parsed["date"],
            participants=valid_participants,
            winners=valid_winners,
            summary_text=summary,
        )

        try:
            await message.clear_reactions()
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        await self.refresh_live_board(message.guild)

    def build_leaderboard_embed(self, guild: discord.Guild, page: int = 0) -> discord.Embed:
        ranking = self.store.get_leaderboard()
        per_page = 15
        total_pages = max(1, (len(ranking) - 1) // per_page + 1)
        page = max(0, min(page, total_pages - 1))

        embed = discord.Embed(
            title="🏆 TABLERO OFICIAL DE PUNTOS — EJÉRCITO GFS",
            description=(
                "Clasificación mensual de mérito militar y saldo disponible para canje de membresías.\n"
                f"**Escala:** Participación `+{POINTS_PARTICIPANT} pt` · Ronda Ganada `+{POINTS_WINNER} pts` · Guía `+{POINTS_GUIDE} pts`"
            ),
            color=discord.Color.gold(),
        )

        if not ranking:
            embed.add_field(
                name="Sin registros en el ciclo actual",
                value="Aún no se han contabilizado actividades este mes.",
                inline=False,
            )
        else:
            start = page * per_page
            chunk = ranking[start : start + per_page]
            lines = []
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            for idx, entry in enumerate(chunk, start=start):
                prefix = medals.get(idx, f"`#{idx + 1:02d}`")
                lines.append(
                    f"{prefix} **{entry['habbo_name']}** — **{entry['available']} pts** disp. *(Total: {entry['earned']})*"
                )
            embed.add_field(name=f"Clasificación General (Página {page + 1}/{total_pages})", value="\n".join(lines), inline=False)

        embed.set_footer(text="Ejército GFS · Actualización automática en tiempo real")
        return embed

    async def refresh_live_board(self, guild: discord.Guild) -> None:
        cfg = self.store.get_board_message(guild.id)
        if not cfg:
            return
        channel = guild.get_channel(int(cfg["channel_id"]))
        if not channel:
            return
        try:
            msg = await channel.fetch_message(int(cfg["message_id"]))
            embed = self.build_leaderboard_embed(guild, 0)
            await msg.edit(embed=embed, view=LeaderboardView(self, 0))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    @app_commands.command(name="tablero_puntos", description="Publica el tablero dinámico de puntos en este canal (Solo Admins).")
    @app_commands.checks.has_permissions(administrator=True)
    async def tablero_puntos(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = self.build_leaderboard_embed(interaction.guild, 0)
        msg = await interaction.channel.send(embed=embed, view=LeaderboardView(self, 0))
        self.store.save_board_message(interaction.guild.id, interaction.channel.id, msg.id)
        await interaction.followup.send("✅ Tablero dinámico publicado y vinculado para actualización automática.", ephemeral=True)

    @app_commands.command(name="canjear_puntos", description="Descuenta puntos a un oficial por el canje de una membresía (Solo Admins).")
    @app_commands.checks.has_permissions(administrator=True)
    async def canjear_puntos(
        self,
        interaction: discord.Interaction,
        habbo_usuario: str,
        membresia: str,
        costo_puntos: int,
    ) -> None:
        await interaction.response.defer()
        if costo_puntos <= 0:
            return await interaction.followup.send("❌ El costo en puntos debe ser mayor a 0.", ephemeral=True)

        link = self.store.get_verified_user(habbo_usuario)
        if not link:
            return await interaction.followup.send(f"❌ El usuario **{habbo_usuario}** no está verificado.", ephemeral=True)

        habbo_oficial = link["habbo_name"]
        success = self.store.redeem_membership(
            discord_id=int(link["discord_id"]),
            habbo_name=habbo_oficial,
            membership_name=membresia,
            cost=costo_puntos,
            admin_id=interaction.user.id,
        )
        if not success:
            bal = self.store.get_user_balance(int(link["discord_id"]))
            disp = bal["available"] if bal else 0
            return await interaction.followup.send(
                f"❌ **{habbo_oficial}** no tiene saldo suficiente. Disponible actual: **{disp} pts**.",
                ephemeral=True,
            )

        bal = self.store.get_user_balance(int(link["discord_id"]))
        embed = discord.Embed(
            title="🛍️ Canje de Membresía Autorizado",
            description=f"Se ha procesado el canje de puntos de <@{link['discord_id']}>.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Oficial", value=f"**{habbo_oficial}**", inline=True)
        embed.add_field(name="Membresía Adquirida", value=f"**{membresia}**", inline=True)
        embed.add_field(name="Costo Descontado", value=f"`-{costo_puntos} pts`", inline=True)
        embed.add_field(name="Saldo Restante", value=f"**{bal['available']} pts**", inline=True)
        embed.add_field(name="Autorizado por", value=interaction.user.mention, inline=True)
        embed.set_footer(text="Ejército GFS · Tesorería y Mérito Militar")

        await self.refresh_live_board(interaction.guild)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="ajustar_puntos", description="Suma o resta puntos manualmente a un oficial verificado (Solo Admins).")
    @app_commands.checks.has_permissions(administrator=True)
    async def ajustar_puntos(self, interaction: discord.Interaction, habbo_usuario: str, puntos: int, motivo: str) -> None:
        await interaction.response.defer(ephemeral=True)
        link = self.store.get_verified_user(habbo_usuario)
        if not link:
            return await interaction.followup.send(f"❌ El usuario **{habbo_usuario}** no está verificado.", ephemeral=True)

        habbo_oficial = link["habbo_name"]
        self.store.adjust_points(int(link["discord_id"]), habbo_oficial, puntos)
        bal = self.store.get_user_balance(int(link["discord_id"]))
        await self.refresh_live_board(interaction.guild)
        signo = "+" if puntos >= 0 else ""
        await interaction.followup.send(
            f"✅ Ajuste aplicado a **{habbo_oficial}** (`{signo}{puntos} pts`). Nuevo saldo disponible: **{bal['available']} pts**.\n**Motivo:** {motivo}",
            ephemeral=True,
        )

    @app_commands.command(name="cierre_mensual", description="Publica el acta final del mes y reinicia el tablero a 0 (Solo Admins).")
    @app_commands.checks.has_permissions(administrator=True)
    async def cierre_mensual(self, interaction: discord.Interaction, mes_cerrado: str) -> None:
        await interaction.response.defer()
        ranking = self.store.get_leaderboard()

        embed = discord.Embed(
            title=f"📜 ACTA OFICIAL DE CIERRE MENSUAL — {mes_cerrado.upper()}",
            description="Se da por concluido el ciclo mensual de actividades del **Ejército GFS**. Los contadores han sido reiniciados para el nuevo mes.",
            color=discord.Color.blue(),
        )

        if ranking:
            top_lines = []
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            for idx, entry in enumerate(ranking[:15]):
                prefix = medals.get(idx, f"`#{idx + 1:02d}`")
                top_lines.append(
                    f"{prefix} **{entry['habbo_name']}** — Acumulado: **{entry['earned']} pts** *(Saldo sin canjear: {entry['available']} pts)*"
                )
            embed.add_field(name="🎖️ Cuadro de Honor del Mes", value="\n".join(top_lines), inline=False)
        else:
            embed.add_field(name="Resumen", value="No hubo puntos registrados durante este periodo.", inline=False)

        embed.set_footer(text=f"Cierre ejecutado por {interaction.user.display_name} · Ejército GFS")
        self.store.reset_monthly_cycle()
        await self.refresh_live_board(interaction.guild)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PointsCommands(bot))
