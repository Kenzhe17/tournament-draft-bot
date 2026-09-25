"""View для интерактивной справки по командам."""

import discord


class HelpCategoryButton(discord.ui.Button):
    """Кнопка категории справки."""

    def __init__(self, category: str, label: str):
        category_colors = {
            "tournament": discord.ButtonStyle.primary,
            "economy": discord.ButtonStyle.success,
            "shop": discord.ButtonStyle.secondary,
            "profile": discord.ButtonStyle.danger,
            "admin": discord.ButtonStyle.secondary,
        }

        style = category_colors.get(category, discord.ButtonStyle.primary)

        super().__init__(
            style=style,
            label=label,
            custom_id=f"help_category:{category}"
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        """Показать команды категории."""
        from utils.embeds import build_help_category_embed

        embed = build_help_category_embed(self.category)
        view = HelpView(self.category)

        await interaction.response.edit_message(embed=embed, view=view)


class HelpBackButton(discord.ui.Button):
    """Кнопка возврата в главное меню справки."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="↩️ В меню",
            custom_id="help_back"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Вернуться в главное меню."""
        embed = discord.Embed(
            title="📚 Справка по командам",
            description="Выберите категорию для просмотра команд",
            color=discord.Color.dark_blue()
        )

        view = HelpMainView()
        await interaction.response.edit_message(embed=embed, view=view)


class HelpView(discord.ui.View):
    """View для конкретной категории справки."""

    def __init__(self, category: str):
        super().__init__(timeout=None)
        self.add_item(HelpBackButton())


class HelpMainView(discord.ui.View):
    """Главное меню справки."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(HelpCategoryButton("tournament", "🏆 Турниры"))
        self.add_item(HelpCategoryButton("economy", "💰 Экономика"))
        self.add_item(HelpCategoryButton("shop", "🛒 Магазин"))
        self.add_item(HelpCategoryButton("profile", "👤 Профиль"))
        self.add_item(HelpCategoryButton("admin", "⚙️ Админ"))
