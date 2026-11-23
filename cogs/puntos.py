import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

ACTIVITIES_CHANNEL_NAME = os.getenv("ACTIVITIES_CHANNEL_NAME", "registro-actividades")
ACTIVITIES_CHANNEL_ID = os.getenv("ACTIVITIES_CHANNEL_ID", "").strip()
VERIFIED_ROLE_NAME = os.getenv("VERIFIED_ROLE_NAME", "Verificado")
HIGH_RANK_ROLE_NAME = os.getenv("HIGH_RANK_ROLE_NAME", "🏅 Gobierno 🏅")

POINTS_PARTICIPANT = 1
POINTS_WINNER = 3
POINTS_GUIDE = 5

REWARDS_CATALOG: dict[str, dict] = {
    "digito_1": {
        "name": "1 Dígito Habbo",
        "cost": 30,
        "period": "Semanal",
        "cooldown_days": 7,
        "emoji": "1️⃣",
        "desc": "Dígito básico. Mínimo de puntos para canjear.",
    },
    "digitos_3": {
        "name": "3 Dígitos Habbo",
        "cost": 80,
        "period": "Semanal",
        "cooldown_days": 7,
        "emoji": "3️⃣",
        "desc": "Paquete de 3 dígitos.",
    },
    "creditos_5": {
        "name": "5 Créditos Habbo",
        "cost": 60,
        "period": "Quincenal",
        "cooldown_days": 15,
        "emoji": "💰",
        "desc": "Créditos para decorar o comprar en Habbo.",
    },
    "creditos_15": {
        "name": "15 Créditos Habbo",
        "cost": 150,
        "period": "Quincenal",
        "cooldown_days": 15,
        "emoji": "💎",
        "desc": "Paquete mayor de créditos Habbo.",
    },
    "membresia_vip": {
        "name": "Membresía VIP",
        "cost": 200,
        "period": "Mensual",
        "cooldown_days": 30,
        "emoji": "👑",
        "desc": "Premio exclusivo de membresía.",
    },
    "caja_hc": {
        "name": "Caja HC",
        "cost": 300,
        "period": "Mensual",
        "cooldown_days": 30,
        "emoji": "📦",
        "desc": "Caja sorpresa HC de Habbo.",
    },
    "membresia_titanium": {
        "name": "Membresía Titanium",
        "cost": 350,
        "period": "Mensual",
        "cooldown_days": 30,
        "emoji": "🛡️",
        "desc": "Premio exclusivo de membresía.",
    },
}

HABBO_ALIASES: dict[str, str] = {
    "rubenandres09": "rubenandres",
    "rubenandre09": "rubenandres",
    "rubenandres": "rubenandres",
    "danopsmash": "dan",
    "danospsmash": "dan",
    "danopsmas": "dan",
    "danopsma": "dan",
    "dan": "dan",
    "100mar100": "100mar",
    "100mar": "100mar",
    "felipefrancis": "felipefrancisc",
    "felipefrancisc": "felipefrancisc",
    "cruzazulista": "cruz",
    "cruz": "cruz",
    "luisito32": "luisito",
    "luisito": "luisito",
    "rudamn": "ruda",
    "ruda": "ruda",
    "walesk": "walezk",
    "walezk": "walezk",
    "xavierfeijoo": "xavierfeijo",
    "xavierjeijo": "xavierfeijo",
    "xavierfeijo": "xavierfeijo",
    "vittoriojoss": "vittorio.joss",
    "vitorrio.joss": "vittorio.joss",
    "vittori.joss": "vittorio.joss",
    "vittorio.joss": "vittorio.joss",
    "nicol1312": "nicole1312",
    "nicole1312": "nicole1312",
    "kali1": "kali_1",
    "kali_1": "kali_1",
    "freddy!": "freddy",
    "freddy": "freddy",
    "juand@": "juanda",
    "juanda": "juanda",
    "leisverjose": "leisver",
    "leiverjose": "leisver",
    "leisver": "leisver",
    "multyreplay": "multyrecplay",
    "multyrecplay": "multyrecplay",
    "dominusursus": "dominusursus",
    "andre": "andre",
    "andré": "andre",
    "habbodavid": "habbo_david",
    "habbo_david": "habbo_david",
    "nicol": ":=nicol=:",
    "=nicol=": ":=nicol=:",
    "milkyway": "milkyway",
    "lyra": "-lyra.-",
    "adrianav": "adrianav",
    "adrianette2": "adrinette2",
    "adrinette2": "adrinette2",
    "ypjn19": "yjpn19",
    "yjpn19": "yjpn19",
    "3n3ria": "3n3ri4",
    "3n3ri4": "3n3ri4",
    "solofelicidad": ".solofelicidad.",
    "juan__2000": "juan2000",
    "juan_2000": "juan2000",
    "juan 2000": "juan2000",
    "juan2000": "juan2000",
    "karladayana12": "karladayana12",
}


def clean_raw_name(text: str) -> str:
    cleaned = re.sub(r"[\u200b-\u200f\ufeff`*]", "", text)
    cleaned = cleaned.replace(r"\_", "_").replace(r"\.", ".")
    cleaned = re.sub(r"\[[^\]]+\]", "", cleaned)
    return cleaned.strip()


def normalize_habbo_key(name: str) -> str:
    base = clean_raw_name(name).casefold()
    stripped = re.sub(r"^[._\-:!@]+|[._\-:!@]+\$", "", base).strip()
    if stripped in HABBO_ALIASES:
        return HABBO_ALIASES[stripped]
    if base in HABBO_ALIASES:
        return HABBO_ALIASES[base]
    return stripped


def is_high_rank(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    target = HIGH_RANK_ROLE_NAME.casefold()
    for role in member.roles:
        role_cf = role.name.casefold()
        if role_cf == target or "gobierno" in role_cf:
            return True
    return False


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
                CREATE TABLE IF NOT EXISTS verified_links (
                    discord_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    habbo_unique_id TEXT NOT NULL UNIQUE,
                    habbo_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    verified_at INTEGER NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS transfers_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_discord_id INTEGER NOT NULL,
                    sender_habbo TEXT NOT NULL,
                    receiver_discord_id INTEGER NOT NULL,
                    receiver_habbo TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
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
        cleaned = clean_raw_name(habbo_name)
        target_norm = normalize_habbo_key(cleaned)
        with self.database() as db:
            exact = db.execute(
                "SELECT * FROM verified_links WHERE habbo_name=? COLLATE NOCASE LIMIT 1",
                (cleaned,),
            ).fetchone()
            if exact and int(exact["discord_id"]) > 0:
                return exact

            all_rows = db.execute("SELECT * FROM verified_links").fetchall()
            if target_norm:
                for row in all_rows:
                    if int(row["discord_id"]) > 0 and normalize_habbo_key(row["habbo_name"]) == target_norm:
                        return row
                for row in all_rows:
                    if normalize_habbo_key(row["habbo_name"]) == target_norm:
                        return row
            if exact:
                return exact
        return None

    def restore_verified_from_guild(self, discord_id: int, guild_id: int, habbo_name: str):
        cleaned = clean_raw_name(habbo_name)
        with self.database() as db:
            db.execute(
                """INSERT INTO verified_links (discord_id, guild_id, habbo_unique_id, habbo_name, verified_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(discord_id) DO UPDATE SET
                     habbo_name=excluded.habbo_name""",
                (discord_id, guild_id, f"discord-sync-{discord_id}", cleaned, int(time.time())),
            )
            return db.execute(
                "SELECT * FROM verified_links WHERE discord_id=? LIMIT 1",
                (discord_id,),
            ).fetchone()

    def create_provisional_user(self, habbo_name: str):
        cleaned = clean_raw_name(habbo_name)
        prov_id = -(abs(hash(cleaned.casefold())) % 900_000_000 + 100_000_000)
        with self.database() as db:
            db.execute(
                """INSERT INTO verified_links (discord_id, guild_id, habbo_unique_id, habbo_name, verified_at)
                   VALUES (?, 0, ?, ?, ?)
                   ON CONFLICT(habbo_name) DO NOTHING""",
                (prov_id, f"provisional-{cleaned.casefold()}", cleaned, int(time.time())),
            )
            return db.execute(
                "SELECT * FROM verified_links WHERE habbo_name=? COLLATE NOCASE LIMIT 1",
                (cleaned,),
            ).fetchone()

    def merge_provisional_to_discord(self, real_discord_id: int, guild_id: int, habbo_name: str) -> int:
        cleaned = clean_raw_name(habbo_name)
        target_norm = normalize_habbo_key(cleaned)
        merged_count = 0
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            provs = db.execute("SELECT * FROM verified_links WHERE discord_id < 0").fetchall()
            matching_provs = [
                p for p in provs
                if p["habbo_name"].casefold() == cleaned.casefold()
                or (target_norm and normalize_habbo_key(p["habbo_name"]) == target_norm)
            ]

            for prov in matching_provs:
                prov_id = int(prov["discord_id"])
                prov_pts = db.execute(
                    "SELECT * FROM military_points WHERE discord_id=? LIMIT 1",
                    (prov_id,),
                ).fetchone()

                if prov_pts:
                    self._upsert_points(
                        db,
                        real_discord_id,
                        cleaned,
                        part_pts=prov_pts["points_participation"],
                        win_pts=prov_pts["points_wins"],
                        guide_pts=prov_pts["points_guide"],
                        adj_pts=prov_pts["points_adjusted"],
                        spent_pts=prov_pts["points_spent"],
                        now=int(time.time()),
                    )
                    db.execute("DELETE FROM military_points WHERE discord_id=?", (prov_id,))

                db.execute("DELETE FROM verified_links WHERE discord_id=?", (prov_id,))
                merged_count += 1

            db.execute(
                """INSERT INTO verified_links (discord_id, guild_id, habbo_unique_id, habbo_name, verified_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(discord_id) DO UPDATE SET habbo_name=excluded.habbo_name""",
                (real_discord_id, guild_id, f"discord-sync-{real_discord_id}", cleaned, int(time.time())),
            )
        return merged_count

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

    def check_reward_limit(self, discord_id: int, reward_key: str) -> tuple[bool, int]:
        reward = REWARDS_CATALOG[reward_key]
        cooldown_secs = reward["cooldown_days"] * 86400
        now = int(time.time())
        with self.database() as db:
            row = db.execute(
                """SELECT created_at FROM redemptions_log
                   WHERE discord_id=? AND membership_name=? COLLATE NOCASE
                   ORDER BY created_at DESC LIMIT 1""",
                (discord_id, reward["name"]),
            ).fetchone()
            if not row:
                return True, 0
            next_available = int(row["created_at"]) + cooldown_secs
            if now < next_available:
                return False, next_available
            return True, 0

    def transfer_points(
        self,
        sender_id: int,
        sender_habbo: str,
        receiver_id: int,
        receiver_habbo: str,
        amount: int,
        reason: str,
    ) -> bool:
        now = int(time.time())
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM military_points WHERE discord_id=? LIMIT 1", (sender_id,)).fetchone()
            if not row:
                return False
            earned = row["points_participation"] + row["points_wins"] + row["points_guide"] + row["points_adjusted"]
            available = earned - row["points_spent"]
            if available < amount:
                return False

            self._upsert_points(db, sender_id, sender_habbo, adj_pts=-amount, now=now)
            self._upsert_points(db, receiver_id, receiver_habbo, adj_pts=amount, now=now)

            db.execute(
                """INSERT INTO transfers_log
                   (sender_discord_id, sender_habbo, receiver_discord_id, receiver_habbo, amount, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sender_id, sender_habbo, receiver_id, receiver_habbo, amount, reason, now),
            )
            return True

    def redeem_reward_atomic(self, discord_id: int, habbo_name: str, reward_key: str, admin_id: int) -> tuple[bool, str]:
        reward = REWARDS_CATALOG[reward_key]
        cost = reward["cost"]
        cooldown_secs = reward["cooldown_days"] * 86400
        now = int(time.time())

        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            last = db.execute(
                """SELECT created_at FROM redemptions_log
                   WHERE discord_id=? AND membership_name=? COLLATE NOCASE
                   ORDER BY created_at DESC LIMIT 1""",
                (discord_id, reward["name"]),
            ).fetchone()
            if last and now < int(last["created_at"]) + cooldown_secs:
                next_ts = int(last["created_at"]) + cooldown_secs
                return False, f"Límite {reward['period'].lower()} alcanzado. Disponible nuevamente <t:{next_ts}:R>."

            row = db.execute("SELECT * FROM military_points WHERE discord_id=? LIMIT 1", (discord_id,)).fetchone()
            if not row:
                return False, "El usuario no tiene puntos acumulados."
            earned = row["points_participation"] + row["points_wins"] + row["points_guide"] + row["points_adjusted"]
            available = earned - row["points_spent"]
            if available < cost:
                return False, f"Saldo insuficiente (tiene **{available} pts**, requiere **{cost} pts**)."

            self._upsert_points(db, discord_id, habbo_name, spent_pts=cost, now=now)
            db.execute(
                """INSERT INTO redemptions_log (discord_id, habbo_name, membership_name, cost, admin_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (discord_id, habbo_name, reward["name"], cost, admin_id, now),
            )
            return True, "OK"

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
class RewardSelect(discord.ui.Select):
    def __init__(self, cog, requester_id: int, habbo_name: str, available_pts: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.habbo_name = habbo_name
        self.available_pts = available_pts

        options = []
        for key, item in REWARDS_CATALOG.items():
            options.append(
                discord.SelectOption(
                    label=f"{item['name']} — {item['cost']} pts ({item['period']})",
                    value=key,
                    description=item["desc"][:100],
                    emoji=item["emoji"],
                )
            )
        super().__init__(
            placeholder="Selecciona el premio que deseas canjear...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            return await interaction.response.send_message("❌ Este menú pertenece a otro usuario.", ephemeral=True)

        reward_key = self.values[0]
        reward = REWARDS_CATALOG[reward_key]

        allowed, next_ts = self.cog.store.check_reward_limit(self.requester_id, reward_key)
        if not allowed:
            return await interaction.response.edit_message(
                content=(
                    f"⛔ **Límite de Canje Alcanzado ({reward['period']})**\n"
                    f"Ya reclamaste **{reward['name']}** dentro de este periodo.\n"
                    f"• **Próximo canje habilitado:** <t:{next_ts}:F> (<t:{next_ts}:R>)."
                ),
                embed=None,
                view=None,
            )

        bal = self.cog.store.get_user_balance(self.requester_id)
        disp = bal["available"] if bal else 0
        if disp < reward["cost"]:
            return await interaction.response.edit_message(
                content=(
                    f"❌ **Puntos Insuficientes**\n"
                    f"Para canjear **{reward['name']}** necesitas **{reward['cost']} pts**, pero tu saldo disponible actual es de **{disp} pts**."
                ),
                embed=None,
                view=None,
            )

        req_embed = discord.Embed(
            title="📋 Solicitud de Canje de Puntos — Pendiente de Autorización",
            description=(
                f"El oficial <@{self.requester_id}> (**{self.habbo_name}**) solicita canjear puntos de su cuenta.\n"
                f"*Requiere aprobación de **{HIGH_RANK_ROLE_NAME}** o Administración.*"
            ),
            color=discord.Color.orange(),
        )
        req_embed.add_field(name="Premio Solicitado", value=f"{reward['emoji']} **{reward['name']}**", inline=True)
        req_embed.add_field(name="Costo", value=f"**{reward['cost']} pts**", inline=True)
        req_embed.add_field(name="Límite / Reclamación", value=f"**{reward['period']}**", inline=True)
        req_embed.add_field(name="Saldo Actual del Oficial", value=f"{disp} pts *(Quedarían {disp - reward['cost']} pts)*", inline=False)
        req_embed.set_footer(text="Ejército GFS · Control de Recompensas y Ascensos")

        approval_view = RedemptionApprovalView(
            cog=self.cog,
            requester_id=self.requester_id,
            habbo_name=self.habbo_name,
            reward_key=reward_key,
        )

        await interaction.channel.send(embed=req_embed, view=approval_view)
        await interaction.response.edit_message(
            content=f"✅ **Solicitud enviada.** Un miembro de **{HIGH_RANK_ROLE_NAME}** revisará y autorizará tu canje de **{reward['name']}** en este canal.",
            embed=None,
            view=None,
        )


class RewardCatalogView(discord.ui.View):
    def __init__(self, cog, requester_id: int, habbo_name: str, available_pts: int) -> None:
        super().__init__(timeout=180)
        self.add_item(RewardSelect(cog, requester_id, habbo_name, available_pts))


class RedemptionApprovalView(discord.ui.View):
    def __init__(self, cog, requester_id: int, habbo_name: str, reward_key: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.requester_id = requester_id
        self.habbo_name = habbo_name
        self.reward_key = reward_key

    @discord.ui.button(label="Autorizar Canje", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not is_high_rank(interaction.user):
            return await interaction.response.send_message(
                f"❌ Solo miembros con el rango **{HIGH_RANK_ROLE_NAME}** o Administradores pueden autorizar canjes.",
                ephemeral=True,
            )

        success, reason = self.cog.store.redeem_reward_atomic(
            discord_id=self.requester_id,
            habbo_name=self.habbo_name,
            reward_key=self.reward_key,
            admin_id=interaction.user.id,
        )

        if not success:
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            return await interaction.response.send_message(f"⚠️ No se pudo completar el canje: {reason}", ephemeral=True)

        reward = REWARDS_CATALOG[self.reward_key]
        bal = self.cog.store.get_user_balance(self.requester_id)
        remaining = bal["available"] if bal else 0

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        announce_embed = discord.Embed(
            title="🎉 CANJE OFICIAL AUTORIZADO — EJÉRCITO GFS",
            description=(
                f"El oficial <@{self.requester_id}> (**{self.habbo_name}**) ha canjeado sus puntos exitosamente "
                f"y fue autorizado por {interaction.user.mention}."
            ),
            color=discord.Color.green(),
        )
        announce_embed.add_field(name="Premio Obtenido", value=f"{reward['emoji']} **{reward['name']}**", inline=True)
        announce_embed.add_field(name="Puntos Canjeados", value=f"`-{reward['cost']} pts`", inline=True)
        announce_embed.add_field(name="Reclamación", value=reward["period"], inline=True)
        announce_embed.add_field(name="Saldo Restante", value=f"**{remaining} pts**", inline=True)
        announce_embed.add_field(name="Autorizado por", value=interaction.user.mention, inline=True)
        announce_embed.set_footer(text="Ejército GFS · Tesorería y Mérito Militar")

        await self.cog.refresh_live_board(interaction.guild)
        await interaction.response.send_message(embed=announce_embed)

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not is_high_rank(interaction.user):
            return await interaction.response.send_message(
                f"❌ Solo miembros con el rango **{HIGH_RANK_ROLE_NAME}** o Administradores pueden rechazar solicitudes.",
                ephemeral=True,
            )

        reward = REWARDS_CATALOG[self.reward_key]
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"❌ La solicitud de canje de **{reward['name']}** para <@{self.requester_id}> (**{self.habbo_name}) fue rechazada por {interaction.user.mention}."
        )


class LeaderboardView(discord.ui.View):
    def __init__(self, cog, page: int = 0) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.page = page
        web_url = os.getenv("RENDER_EXTERNAL_URL", "https://bot-discord-yau4.onrender.com").rstrip("/") + "/tablero"
        self.add_item(discord.ui.Button(label="Tablero Web", style=discord.ButtonStyle.link, url=web_url, emoji="🌐"))

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
        link = self.cog.resolve_verified_by_member(interaction.guild, interaction.user)
        if not link:
            return await interaction.response.send_message(
                "❌ No estás verificado en la base militar. Completa tu registro primero.", ephemeral=True
            )
        bal = self.cog.store.get_user_balance(int(link["discord_id"]))
        if not bal:
            return await interaction.response.send_message(
                f"🎖️ **, aún no registras puntos en el ciclo mensual actual (**0 pts**).",
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
        embed.add_field(name="🔄 Ajustes / Cesiones", value=f"{bal['points_adjusted']:+d} pts", inline=True)
        embed.add_field(name="🛍️ Canjeados en Premios", value=f"{bal['points_spent']} pts", inline=True)
        embed.add_field(name="📊 Total Neto del Mes", value=f"{bal['earned']} pts", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Canjear Puntos", style=discord.ButtonStyle.success, emoji="🛍️", custom_id="pts_redeem_btn")
    async def redeem_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.open_redeem_menu(interaction)


class PointsCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = PointsStore()

    async def cog_load(self) -> None:
        self.bot.add_view(LeaderboardView(self, 0))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.sync_all_verified_members()

    async def sync_all_verified_members(self) -> int:
        merged_total = 0
        for guild in self.bot.guilds:
            verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
            if not verified_role:
                continue
            for member in guild.members:
                if member.bot or verified_role not in member.roles:
                    continue
                habbo_nick = clean_raw_name(member.display_name)
                if habbo_nick:
                    merged_total += self.store.merge_provisional_to_discord(member.id, guild.id, habbo_nick)
                    if not self.store.get_verified_by_discord(member.id):
                        self.store.restore_verified_from_guild(member.id, guild.id, habbo_nick)
        return merged_total

    def resolve_verified_by_member(self, guild: discord.Guild, member: discord.Member):
        verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
        if verified_role and verified_role in member.roles:
            habbo_nick = clean_raw_name(member.display_name)
            self.store.merge_provisional_to_discord(member.id, guild.id, habbo_nick)
            restored = self.store.restore_verified_from_guild(member.id, guild.id, habbo_nick)
            return dict(restored) if restored else None
        row = self.store.get_verified_by_discord(member.id)
        return dict(row) if row else None

    def resolve_verified_member(self, guild: discord.Guild, habbo_name: str):
        cleaned = clean_raw_name(habbo_name)
        target = cleaned.casefold()
        target_norm = normalize_habbo_key(cleaned)

        if guild:
            verified_role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
            for member in guild.members:
                if member.bot:
                    continue
                if verified_role and verified_role not in member.roles:
                    continue
                raw_candidates = [clean_raw_name(member.display_name), clean_raw_name(member.name)]
                exact_candidates = {c.casefold() for c in raw_candidates if c}
                norm_candidates = {normalize_habbo_key(c) for c in raw_candidates if normalize_habbo_key(c)}

                if target in exact_candidates or (target_norm and target_norm in norm_candidates):
                    official_name = clean_raw_name(member.display_name) or cleaned
                    self.store.merge_provisional_to_discord(member.id, guild.id, official_name)
                    restored = self.store.restore_verified_from_guild(member.id, guild.id, official_name)
                    return dict(restored) if restored else None

        row = self.store.get_verified_user(cleaned)
        if row:
            return dict(row)

        return None

    async def open_redeem_menu(self, interaction: discord.Interaction) -> None:
        link = self.resolve_verified_by_member(interaction.guild, interaction.user)
        if not link:
            return await interaction.response.send_message(
                "❌ No estás verificado en la base militar. Completa tu registro antes de canjear puntos.",
                ephemeral=True,
            )

        bal = self.store.get_user_balance(int(link["discord_id"]))
        available = bal["available"] if bal else 0
        if available < 30:
            return await interaction.response.send_message(
                f"❌ **Saldo insuficiente:** Tienes **{available} pts** disponibles y el premio mínimo cuesta **30 pts** (1 Dígito Habbo).",
                ephemeral=True,
            )

        catalog_lines = []
        for key, item in REWARDS_CATALOG.items():
            allowed, next_ts = self.store.check_reward_limit(int(link["discord_id"]), key)
            status = "✅ Disponible" if allowed else f"⏳ Límite alcanzado (<t:{next_ts}:R>)"
            catalog_lines.append(
                f"{item['emoji']} **{item['name']}** — `{item['cost']} pts` · *{item['period']}* ({status})"
            )

        embed = discord.Embed(
            title=f"🛍️ Catálogo Oficial de Canjes — {link['habbo_name']}",
            description=(
                f"💰 **Tu saldo disponible:** **{available} pts**\n"
                f"Selecciona un premio en el menú desplegable inferior. Un rango alto (**{HIGH_RANK_ROLE_NAME}**) autorizará tu entrega.\n\n"
                + "\n".join(catalog_lines)
            ),
            color=discord.Color.gold(),
        )
        view = RewardCatalogView(self, int(link["discord_id"]), link["habbo_name"], available)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def find_activities_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if ACTIVITIES_CHANNEL_ID and ACTIVITIES_CHANNEL_ID.isdigit():
            ch = guild.get_channel(int(ACTIVITIES_CHANNEL_ID))
            if isinstance(ch, discord.TextChannel):
                return ch
        for ch in guild.text_channels:
            if ACTIVITIES_CHANNEL_NAME.lower() in ch.name.lower():
                return ch
        return None

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
            clean = clean_raw_name(line)
            lower = clean.lower()

            if "fecha:" in lower:
                date_str = clean.split(":", 1)[1].strip()
                section = None
            elif "juego:" in lower:
                game_str = clean.split(":", 1)[1].strip()
                section = None
            elif "encargado" in lower and ":" in lower:
                guide_str = clean_raw_name(clean.split(":", 1)[1])
                section = None
            elif "participantes" in lower:
                section = "participants"
            elif "ganadores" in lower:
                section = "winners"
            elif section == "participants":
                name = re.sub(r"^[\-\•\*\d\)\s]+", "", clean).strip()
                name = re.sub(r"\s*\(.*?\)\s*\$", "", name).strip()
                name = clean_raw_name(name)
                if name:
                    raw_participants.append(name)
            elif section == "winners":
                if ":" in clean:
                    name = clean.split(":", 1)[1].strip()
                else:
                    name = re.sub(r"^[\-\•\*\d\)\s]+", "", clean).strip()
                name = re.sub(r"\s*\(.*?\)\s*\$", "", name).strip()
                name = clean_raw_name(name)
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

    def validate_and_extract_entries(self, guild: discord.Guild, parsed: dict):
        unverified = []

        guide_row = self.resolve_verified_member(guild, parsed["guide"])
        if not guide_row:
            unverified.append(f"Encargado(a): `{parsed['guide']}`")

        seen_part = set()
        valid_participants = []
        for p_name in parsed["participants"]:
            key = normalize_habbo_key(p_name) or p_name.casefold()
            if key in seen_part:
                continue
            seen_part.add(key)
            row = self.resolve_verified_member(guild, p_name)
            if not row:
                unverified.append(f"Participante: `{p_name}`")
            else:
                valid_participants.append(row)

        valid_winners = []
        for w_name in parsed["winners"]:
            row = self.resolve_verified_member(guild, w_name)
            if not row:
                unverified.append(f"Ganador(a): `{w_name}`")
            else:
                valid_winners.append(row)

        return guide_row, valid_participants, valid_winners, unverified

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

        guide_row, valid_participants, valid_winners, unverified = self.validate_and_extract_entries(
            message.guild, parsed
        )

        if unverified:
            try:
                await message.clear_reactions()
                await message.add_reaction("⚠️")
            except discord.HTTPException:
                pass
            error_list = "\n".join(f"• {item}" for item in dict.fromkeys(unverified))
            await message.reply(
                "⚠️ **Registro en pausa — Personal no verificado detectado**\n"
                "Los siguientes nombres no coinciden con ningún oficial verificado en el servidor:\n"
                f"{error_list}\n\n"
                "*Edita tu mensaje corrigiendo la ortografía o pide al recluta que se verifique. El bot lo procesará automáticamente al editarlo.*",
                delete_after=45,
            )
            return

        summary = f"Guía: {guide_row['habbo_name']} | Part: {len(valid_participants)} | Rondas: {len(valid_winners)}"
        self.store.record_activity(
            message_id=message.id,
            guild_id=message.guild.id,
            guide=guide_row,
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
                "Clasificación mensual de mérito militar y saldo disponible para canje de premios y membresías.\n"
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

    @app_commands.command(
        name="recalcular_historial",
        description="Escanea los mensajes del canal de actividades y reconstruye los puntos perdidos (Solo Admins).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def recalcular_historial(
        self,
        interaction: discord.Interaction,
        solo_mes_actual: bool = True,
        limite_mensajes: int = 500,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = self.find_activities_channel(interaction.guild)
        if not channel:
            return await interaction.followup.send(
                "❌ No se encontró el canal de registro de actividades.", ephemeral=True
            )

        limite = max(10, min(limite_mensajes, 2000))
        now = datetime.now(timezone.utc)
        after_dt = datetime(now.year, now.month, 1, tzinfo=timezone.utc) if solo_mes_actual else None

        recovered = 0
        already_counted = 0
        skipped_unverified = []

        async for msg in channel.history(limit=limite, after=after_dt, oldest_first=True):
            if msg.author.bot:
                continue

            if self.store.is_activity_processed(msg.id):
                already_counted += 1
                continue

            parsed = self.parse_activity_template(msg.content)
            if not parsed:
                continue

            guide_row, valid_participants, valid_winners, unverified = self.validate_and_extract_entries(
                interaction.guild, parsed
            )

            if unverified:
                skipped_unverified.append(f"• [Mensaje]({msg.jump_url}): {', '.join(dict.fromkeys(unverified))}")
                continue

            summary = f"Guía: {guide_row['habbo_name']} | Part: {len(valid_participants)} | Rondas: {len(valid_winners)}"
            self.store.record_activity(
                message_id=msg.id,
                guild_id=interaction.guild.id,
                guide=guide_row,
                game_name=parsed["game"],
                activity_date=parsed["date"],
                participants=valid_participants,
                winners=valid_winners,
                summary_text=summary,
            )

