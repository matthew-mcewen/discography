from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Union

import discord
from discord.ext import commands

import db.database as db
from utils.config import GuildConfig

if TYPE_CHECKING:
    from main import DiscographyBot

logger = logging.getLogger(__name__)

TROPHY = "🏆"

# An image source is either a discord.Attachment (file upload) or a str URL (direct image link)
ImageSource = Union[discord.Attachment, str]


def _collect_images(message: discord.Message) -> list[ImageSource]:
    """Return all image sources in a message (attachments + direct-image-link embeds)."""
    images: list[ImageSource] = []

    for attachment in message.attachments:
        ct = attachment.content_type or ""
        if ct.startswith("image/"):
            images.append(attachment)

    for embed in message.embeds:
        if embed.type == "image" and embed.url:
            images.append(embed.url)

    return images


class SubmissionsCog(commands.Cog):
    def __init__(self, bot: DiscographyBot) -> None:
        self.bot = bot

    @property
    def _pool(self):
        return self.bot.db_pool

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        config = await self.bot.get_guild_config(message.guild.id)
        if config is None or config.channels.submissions is None:
            return
        if message.channel.id != config.channels.submissions:
            return

        guild_id = message.guild.id
        images = _collect_images(message)

        if config.competition.mode == "basic":
            if len(images) != 1:
                return
            await self._accept_basic(message, guild_id, config, images[0])
            return

        if len(images) == 0:
            await self._reject(
                message,
                "Your post was removed because it contained no image. "
                "Please post exactly one screenshot per entry.",
            )
            return

        if len(images) > 1:
            await self._reject(
                message,
                "Your post was removed because it contained multiple images. "
                "Please post exactly one screenshot per entry.",
            )
            return

        await self._accept(message, guild_id, config, images[0])

    async def _reject(self, message: discord.Message, reason: str) -> None:
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning("Missing permissions to delete message %s", message.id)
            return

        try:
            await message.channel.send(
                f"{message.author.mention} {reason}",
                delete_after=30,
            )
        except discord.Forbidden:
            logger.warning("Missing permissions to send rejection message in channel %s", message.channel.id)

    async def _accept(
        self,
        message: discord.Message,
        guild_id: int,
        config: GuildConfig,
        image: ImageSource,
    ) -> None:
        period = await db.ensure_active_period(self._pool, guild_id, config.tzinfo())
        await db.upsert_user(
            self._pool,
            guild_id,
            message.author.id,
            str(message.author),
            message.author.display_name,
        )

        # Download the image BEFORE deleting the original message.
        # Deleting the message invalidates the CDN URL immediately.
        file: discord.File | None = None
        image_url: str

        if isinstance(image, discord.Attachment):
            try:
                data = await image.read()
                file = discord.File(io.BytesIO(data), filename=image.filename)
                image_url = image.url
            except Exception:
                logger.exception("Failed to download attachment from message %s", message.id)
                image_url = image.url
        else:
            image_url = image  # direct image link — CDN URL won't be invalidated

        embed = discord.Embed(
            description=f"Submitted by {message.author.mention}",
            color=discord.Color.blurple(),
        )
        if file is not None:
            embed.set_image(url=f"attachment://{file.filename}")
        else:
            embed.set_image(url=image_url)
        embed.set_footer(text="Submitted", icon_url=message.author.display_avatar.url)
        embed.timestamp = message.created_at

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

        try:
            if file is not None:
                embed_msg = await message.channel.send(embed=embed, file=file)
            else:
                embed_msg = await message.channel.send(embed=embed)
        except discord.Forbidden:
            logger.error("Missing permissions to post submission embed in channel %s", message.channel.id)
            return

        if embed_msg.attachments:
            image_url = embed_msg.attachments[0].url

        try:
            await embed_msg.add_reaction(TROPHY)
        except discord.Forbidden:
            logger.warning("Missing permissions to add reaction to embed %s", embed_msg.id)

        try:
            await db.create_submission(
                self._pool,
                guild_id,
                period["id"],
                message.author.id,
                message.id,
                embed_msg.id,
                image_url,
            )
        except Exception:
            logger.exception("Failed to save submission for embed message %s", embed_msg.id)

    async def _accept_basic(
        self,
        message: discord.Message,
        guild_id: int,
        config: GuildConfig,
        image: ImageSource,
    ) -> None:
        """
        Basic mode: react directly to the original post instead of deleting and
        reposting it. The original message doubles as the vote target.
        """
        period = await db.ensure_active_period(self._pool, guild_id, config.tzinfo())
        await db.upsert_user(
            self._pool,
            guild_id,
            message.author.id,
            str(message.author),
            message.author.display_name,
        )

        image_url = image.url if isinstance(image, discord.Attachment) else image

        try:
            await message.add_reaction(TROPHY)
        except discord.Forbidden:
            logger.warning("Missing permissions to add reaction to message %s", message.id)
            return

        try:
            await db.create_submission(
                self._pool,
                guild_id,
                period["id"],
                message.author.id,
                message.id,
                message.id,
                image_url,
            )
        except Exception:
            logger.exception("Failed to save submission for message %s", message.id)

    # ------------------------------------------------------------------
    # Reaction cleanup — remove any non-trophy reactions in real time
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        if str(payload.emoji) == TROPHY:
            return
        config = await self.bot.get_guild_config(payload.guild_id)
        if config is None or config.channels.submissions is None:
            return
        if payload.channel_id != config.channels.submissions:
            return
        if config.competition.mode == "basic":
            # Basic mode doesn't enforce trophy-only reactions.
            return

        known = await db.is_known_embed_message(self._pool, payload.guild_id, payload.message_id)
        if not known:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(payload.message_id)
            await msg.clear_reaction(payload.emoji)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Soft-delete when a submission embed is removed
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        config = await self.bot.get_guild_config(payload.guild_id)
        if config is None or config.channels.submissions is None:
            return
        if payload.channel_id != config.channels.submissions:
            return
        await db.soft_delete_submission(self._pool, payload.guild_id, payload.message_id)


async def setup(bot: DiscographyBot) -> None:
    await bot.add_cog(SubmissionsCog(bot))
