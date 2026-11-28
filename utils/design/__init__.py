"""r!z3 2.0 Dark UI — дизайн-система Discord Components V2.

Быстрый старт:

    from utils.design import DesignView, colors, em, btn, action_row

    view = DesignView(title="Баланс", accent=colors.ECONOMY)
    view.text(f"Монеты: **{em('coin')} 1 000**")
    view.sep()
    view.row(
        btn("Пополнить", style="success", custom_id="topup"),
        btn("В меню", style="secondary", custom_id="back"),
    )
    await interaction.response.send_message(view=view)

Шаблоны:
    from utils.design import success_view, error_view, info_view, ...
    await channel.send(view=success_view("Готово", "Операция выполнена"))
"""

from utils.design import colors, constants, emojis, formatters  # noqa: F401
from utils.design.adapter import (  # noqa: F401
    compose,
    embed_to_container,
    embed_to_section_items,
    embed_to_sections,
    smart_view,
    swap_view,
    text_card,
)
from utils.design.components import (  # noqa: F401
    STYLE_DANGER,
    STYLE_LINK,
    STYLE_PRIMARY,
    STYLE_SECONDARY,
    STYLE_SUCCESS,
    action_row,
    btn,
    option,
    select,
)
from utils.design.containers import (  # noqa: F401
    CardBuilder,
    card,
    chunk_text,
    section,
    separator,
    text_display,
)
from utils.design.emojis import EMOJI_IDS, FALLBACKS, em  # noqa: F401
from utils.design.formatters import (  # noqa: F401
    bold,
    channel_mention,
    code,
    dt,
    inline_field,
    kda,
    mention,
    money,
    muted,
    num,
    pct,
    progress_bar,
    rating,
    role_mention,
    truncate,
)
from utils.design.layouts import (  # noqa: F401
    DesignView,
    economy_view,
    edit_card,
    error_view,
    game_view,
    info_view,
    menu_view,
    moderation_view,
    profile_view,
    ranking_view,
    send_card,
    success_view,
    warning_view,
)
