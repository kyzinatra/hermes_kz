---
name: hermes-self-management
description: Управляй собственными файлами, skills, plugins, конфигурацией и cron Hermes.
metadata:
  hermes:
    category: operations
    tags: [self-management, files, skills, plugins, config, cron, backup]
---

# Самоуправление Hermes

Используй этот skill, когда пользователь просит изменить файлы Hermes, создать
или улучшить skill, настроить plugin, изменить конфигурацию либо управлять cron.

## Доступные области

- `/opt/data/workspace` — обычные заметки, проекты и рабочие файлы;
- `/opt/data/skills` — управляемые Hermes skills, создаваемые через
  `skill_manage`;
- `/opt/data/custom-skills` — version-controlled skills этого проекта;
- `/opt/data/config.yaml` — активная конфигурация;
- `/opt/data/SOUL.md` — только личность, стиль общения и постоянные предпочтения;
- `/opt/data/plugins` — локальные plugins;
- `/opt/data/cron` — постоянное состояние cron.

`/opt/data/.env` и `/credentials` доступны только для чтения. Никогда не
переписывай, не копируй в ответы и не сохраняй их содержимое в memory, skills,
логи или рабочие файлы.

## Правила изменения

1. Сначала прочитай текущий файл и делай минимальный patch вместо полной
   перезаписи.
2. Операционные инструкции выноси в skills. Не добавляй их в `SOUL.md`.
3. После изменения `config.yaml` выполни `hermes config check`.
4. После изменения Python-plugin запусти относящиеся к нему тесты до рестарта.
5. Не отключай single-gateway init и не запускай `hermes gateway start/stop`
   внутри контейнера. Жизненным циклом gateway управляет Docker Compose.
6. Перед удалением или массовой перезаписью покажи точные цели пользователю и
   получи подтверждение. Предпочитай обратимые изменения.

## Создание skills

Для обычного нового skill используй `skill_manage`: он пишет в
`/opt/data/skills`, а созданный skill проходит security scan. Если пользователь
явно просит version-controlled skill проекта, создай его в
`/opt/data/custom-skills/<category>/<name>/SKILL.md` с YAML frontmatter,
узким описанием области применения и только необходимыми инструкциями.

После создания или изменения проверь `hermes skills list`. Для обновления
контекста уже открытой Telegram-сессии обычно нужен `/reset`.

## Cron без потери заданий

Перед обслуживанием выполни `hermes cron list` и создай `hermes backup` в
`/backup`. Не очищай `/opt/data/cron` и не пересоздавай весь `runtime` ради
одного задания. После create/edit/remove снова выполни `hermes cron list` и
`hermes cron status`, чтобы подтвердить сохранённое состояние и работу
планировщика.
