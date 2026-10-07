import os
import sqlite3
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = "accounts.db"

# ---- CONFIG ----
LOGIN_URL = ""
PREMIUM_ROLE_NAME = "premium gen"
LOG_CHANNEL_ID = 1556360608117432370
ACCOUNT_LABEL = "an account"

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

ACCOUNT_TYPES = ["Free Tier", "Premium Tier", "Admin"]
PREMIUM_TIERS = {"Premium Tier", "Admin"}

COOLDOWNS = {
    "Free Tier": 60,
    "Premium Tier": 30,
    "Admin": 30,
}

# ---------- DATABASE ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS service_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                data TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                assigned_to INTEGER,
                assigned_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS panel_state (
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                account_type TEXT,
                PRIMARY KEY (user_id, message_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS panel_locations (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                last_used TEXT NOT NULL,
                PRIMARY KEY (user_id, service)
            )
        """)
        conn.commit()

def set_panel_state(user_id, message_id, value):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO panel_state (user_id, message_id, account_type)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, message_id) DO UPDATE SET account_type = excluded.account_type
        """, (user_id, message_id, value))
        conn.commit()

def get_panel_state(user_id, message_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT account_type FROM panel_state WHERE user_id = ? AND message_id = ?",
            (user_id, message_id)
        ).fetchone()

def clear_panel_state(user_id, message_id):
    with get_db() as conn:
        conn.execute("DELETE FROM panel_state WHERE user_id = ? AND message_id = ?", (user_id, message_id))
        conn.commit()

def bulk_add_accounts(service, lines, added_by):
    count = 0
    with get_db() as conn:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            conn.execute("""
                INSERT INTO service_accounts (service, data, added_by, added_at)
                VALUES (?, ?, ?, ?)
            """, (service, line, added_by, datetime.utcnow().isoformat()))
            count += 1
        conn.commit()
    return count

def assign_account(service, target_id):
    with get_db() as conn:
        row = conn.execute("""
            SELECT id, data FROM service_accounts
            WHERE service = ? AND assigned_to IS NULL
            LIMIT 1
        """, (service,)).fetchone()

        if not row:
            return None

        cur = conn.execute("""
            UPDATE service_accounts
            SET assigned_to = ?, assigned_at = ?
            WHERE id = ? AND assigned_to IS NULL
        """, (target_id, datetime.utcnow().isoformat(), row['id']))
        conn.commit()

        if cur.rowcount == 0:
            return None

        return row['data']

def get_stock_count(service):
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM service_accounts WHERE service = ? AND assigned_to IS NULL",
            (service,)
        ).fetchone()
        return row['c'] if row else 0

def save_panel_location(channel_id, message_id):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO panel_locations (channel_id, message_id)
            VALUES (?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET message_id = excluded.message_id
        """, (channel_id, message_id))
        conn.commit()

def get_all_panel_locations():
    with get_db() as conn:
        return conn.execute("SELECT channel_id, message_id FROM panel_locations").fetchall()

# ---------- COOLDOWNS ----------
def get_cooldown_remaining(user_id, service):
    with get_db() as conn:
        row = conn.execute(
            "SELECT last_used FROM cooldowns WHERE user_id = ? AND service = ?",
            (user_id, service)
        ).fetchone()
        if not row:
            return 0
        last_used = datetime.fromisoformat(row['last_used'])
        elapsed = (datetime.utcnow() - last_used).total_seconds()
        remaining = COOLDOWNS.get(service, 0) - elapsed
        return max(0, int(remaining))

def set_cooldown(user_id, service):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO cooldowns (user_id, service, last_used)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, service) DO UPDATE SET last_used = excluded.last_used
        """, (user_id, service, datetime.utcnow().isoformat()))
        conn.commit()

def format_seconds(s):
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    return f"{m}m {sec}s"

# ---------- HELPERS ----------
def has_premium_role(member: discord.Member) -> bool:
    return any(r.name.lower() == PREMIUM_ROLE_NAME.lower() for r in member.roles)

def split_credentials(data: str):
    if ":" in data:
        u, p = data.split(":", 1)
        return u.strip(), p.strip()
    return None, None

async def post_log(client, member: discord.Member):
    try:
        channel = await client.fetch_channel(LOG_CHANNEL_ID)
        await channel.send(f"{member.display_name} has just generated {ACCOUNT_LABEL}")
    except Exception as e:
        print(f"Log post failed: {e}")

# ---------- PANEL EMBED ----------
def build_panel_embed():
    stock_lines = []
    for t in ACCOUNT_TYPES:
        stock_lines.append(f"• **{t}** — {get_stock_count(t)} in stock")

    embed = discord.Embed(
        title="🎁  Account Generator",
        description=(
            "Generate a fresh account quickly and safely.\n"
            "Your account will be sent straight to your **DMs**."
        ),
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.add_field(
        name="📦  Available Tiers",
        value="\n".join(stock_lines),
        inline=False
    )
    embed.add_field(
        name="⏱  Cooldowns",
        value=(
            "• **Free Tier** — 1 minute\n"
            "• **Premium Tier** — 30 seconds\n"
            "• **Admin** — 30 seconds\n"
            "*Administrators skip all cooldowns.*"
        ),
        inline=False
    )
    embed.add_field(
        name="🔒  Locked Tiers",
        value=(
            f"**Premium Tier** and **Admin** require the "
            f"`{PREMIUM_ROLE_NAME}` role.\n"
            "Click **Unlock Premium** below to learn how to get it."
        ),
        inline=False
    )
    embed.set_footer(text="Select a tier below, then click Generate.")
    return embed

# ---------- MODAL ----------
class AddStockModal(discord.ui.Modal):
    def __init__(self, service: str):
        super().__init__(title=f"Add stock - {service}")
        self.service = service
        self.lines = discord.ui.TextInput(
            label="Paste one account per line",
            style=discord.TextStyle.paragraph,
            placeholder="user1:pass1\nuser2:pass2\nuser3:pass3",
            required=True,
            max_length=4000
        )
        self.add_item(self.lines)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can add stock.", ephemeral=True)
            return
        entries = [ln for ln in self.lines.value.split("\n") if ln.strip()]
        added = bulk_add_accounts(self.service, entries, interaction.user.id)
        new_count = get_stock_count(self.service)
        await interaction.response.send_message(
            f"Added **{added}** account(s) to **{self.service}**.\nNew stock: **{new_count}**",
            ephemeral=True
        )
        await refresh_all_panels(interaction.client)

# ---------- PANEL VIEW ----------
_active_generates = set()

def build_panel_view():
    view = AccountPanel()
    options = []
    for t in ACCOUNT_TYPES:
        count = get_stock_count(t)
        options.append(discord.SelectOption(label=f"{t} (stock - {count})", value=t))
    for child in view.children:
        if isinstance(child, discord.ui.Select) and child.custom_id == "account_type_select":
            child.options = options
    return view

class AccountPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Choose an account tier...",
        options=[discord.SelectOption(label=t, value=t) for t in ACCOUNT_TYPES],
        custom_id="account_type_select"
    )
    async def account_type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        set_panel_state(interaction.user.id, interaction.message.id, select.values[0])
        await interaction.response.defer()

    @discord.ui.button(
        label="Generate",
        style=discord.ButtonStyle.green,
        emoji="🎁",
        custom_id="generate_button"
    )
    async def generate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lock_key = (interaction.user.id, interaction.message.id)
        if lock_key in _active_generates:
            await interaction.response.send_message("Hold on, already processing...", ephemeral=True)
            return
        _active_generates.add(lock_key)

        try:
            if not interaction.guild:
                await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
                return

            state = get_panel_state(interaction.user.id, interaction.message.id)
            if not state or not state['account_type']:
                await interaction.response.send_message("Please select an account tier first.", ephemeral=True)
                return

            service = state['account_type']
            is_admin = interaction.user.guild_permissions.administrator

            if service in PREMIUM_TIERS and not is_admin:
                if not has_premium_role(interaction.user):
                    await interaction.response.send_message(
                        f"**{service}** is restricted to the `{PREMIUM_ROLE_NAME}` role.\n"
                        f"Click the **Unlock Premium** button for info.",
                        ephemeral=True
                    )
                    return

            if not is_admin:
                remaining = get_cooldown_remaining(interaction.user.id, service)
                if remaining > 0:
                    await interaction.response.send_message(
                        f"You're on cooldown for **{service}**. Try again in **{format_seconds(remaining)}**.",
                        ephemeral=True
                    )
                    return

            data = assign_account(service, interaction.user.id)
            if not data:
                await interaction.response.send_message(
                    f"No available accounts for **{service}**. Ask an admin to add more.",
                    ephemeral=True
                )
                return

            try:
                embed = discord.Embed(
                    title="🎁  Your Account",
                    description="Save these credentials somewhere safe.",
                    color=discord.Color.green()
                )
                embed.add_field(name="Type", value=f"`{service}`", inline=False)

                u, p = split_credentials(data)
                if u is not None and p is not None:
                    embed.add_field(name="user-", value=f"`{u}`", inline=True)
                    embed.add_field(name="password-", value=f"`{p}`", inline=True)
                else:
                    embed.add_field(name="Account", value=f"```{data}```", inline=False)

                if LOGIN_URL:
                    embed.add_field(name="Login Page", value=LOGIN_URL, inline=False)

                embed.set_footer(text="Please change your password after logging in.")

                await interaction.user.send(embed=embed)
                await interaction.response.send_message("Account sent to your DMs!", ephemeral=True)

                if not is_admin:
                    set_cooldown(interaction.user.id, service)

                await post_log(interaction.client, interaction.user)

            except discord.Forbidden:
                await interaction.response.send_message(
                    "I couldn't DM you. Please enable DMs from server members and try again.",
                    ephemeral=True
                )
            except Exception as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            finally:
                clear_panel_state(interaction.user.id, interaction.message.id)
                await refresh_all_panels(interaction.client)
        finally:
            _active_generates.discard(lock_key)

    @discord.ui.button(
        label="Unlock Premium",
        style=discord.ButtonStyle.blurple,
        emoji="🔓",
        custom_id="unlock_premium_button"
    )
    async def unlock_premium_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = interaction.user.guild_permissions.administrator
        has_role = has_premium_role(interaction.user)

        if is_admin:
            status = "You're an admin — you already have full access."
        elif has_role:
            status = f"You already have the `{PREMIUM_ROLE_NAME}` role. Enjoy!"
        else:
            status = (
                f"You don't have the `{PREMIUM_ROLE_NAME}` role yet.\n"
                f"Ask an admin to give it to you, or open a ticket in the support channel."
            )

        embed = discord.Embed(
            title="🔓  Unlock Premium",
            description=(
                "Premium unlocks **Premium Tier** and **Admin** accounts, "
                "plus **faster cooldowns**.\n\n"
                f"**How to get it:** you need the `{PREMIUM_ROLE_NAME}` role.\n\n"
                f"**Your status:** {status}"
            ),
            color=discord.Color.from_rgb(88, 101, 242)
        )
        embed.set_footer(text="Contact staff if you'd like to upgrade.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- PANEL REFRESH ----------
async def refresh_all_panels(client):
    embed = build_panel_embed()
    for loc in get_all_panel_locations():
        try:
            channel = await client.fetch_channel(loc['channel_id'])
            message = await channel.fetch_message(loc['message_id'])
            await message.edit(embed=embed, view=build_panel_view())
        except Exception:
            pass

# ---------- COMMANDS ----------
@bot.tree.command(name="panel", description="Post the account generation panel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can post the panel.", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_panel_embed(), view=build_panel_view())
    msg = await interaction.original_response()
    save_panel_location(interaction.channel_id, msg.id)

@bot.tree.command(name="addstock", description="Add stock - paste one account per line (admin only)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.choices(service=[
    app_commands.Choice(name="Free Tier", value="Free Tier"),
    app_commands.Choice(name="Premium Tier", value="Premium Tier"),
    app_commands.Choice(name="Admin", value="Admin"),
])
async def addstock(interaction: discord.Interaction, service: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can add stock.", ephemeral=True)
        return
    await interaction.response.send_modal(AddStockModal(service.value))

@bot.tree.command(name="stock", description="View current stock counts")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def stock(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can view stock.", ephemeral=True)
        return
    lines = [f"**{t}**: {get_stock_count(t)}" for t in ACCOUNT_TYPES]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="sync", description="Force sync slash commands (admin only)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can sync.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        bot.tree.copy_global_to(guild=interaction.guild)
        synced = await bot.tree.sync(guild=interaction.guild)
        await interaction.followup.send(f"Synced {len(synced)} commands to this server.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Sync error: {e}", ephemeral=True)

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Global sync failed: {e}")

    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to {guild.name}")
        except Exception as e:
            print(f"Guild sync failed for {guild.id}: {e}")

    await refresh_all_panels(bot)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

init_db()
bot.add_view(AccountPanel())
bot.run(TOKEN)
