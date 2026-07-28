Hermes разворачивается через Docker Compose из этого проекта; основной провайдер — openai-codex, модель — gpt-5.6-sol.
§
Знания хранятся в /opt/data/knowledge: это обычный Markdown-vault, совместимый с Obsidian; исследовательская wiki находится в /opt/data/knowledge/wiki.
§
Внешняя память Holographic локальна, использует /opt/data/memory_store.db и не должна автоматически извлекать все факты подряд.
§
Секреты хранятся только в .env, credentials/ и runtime/. Никогда не копировать токены, пароли и API-ключи в память, заметки или ответы.
