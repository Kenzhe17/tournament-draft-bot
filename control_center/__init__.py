"""r!z3 Control Center — backend пакета.

Отдельный FastAPI-сервис, работающий с тем же PostgreSQL,
что и Discord-бот. Ничего не пишет во внутренние JSON-хранилища
бота: настройки идут через admin_config_* таблицы (бот подхватывает
через LISTEN/NOTIFY), операции над игровым состоянием — через
admin_action_requests, исполняемые ботом.
"""
