# Reporte financiero diario desde GitHub Actions

Este agente se ejecuta diariamente a las 08:00 de Ciudad de México mediante GitHub Actions.
Usa Composio como servidor MCP, Exa para investigar el tipo de cambio y las noticias, Notion para publicar el reporte y Gmail para enviarlo.

No usa el SDK ni la API de OpenAI.

## Secretos requeridos

Configura estos secretos en `Settings → Secrets and variables → Actions`:

- `COMPOSIO_API_KEY`: clave de proyecto de Composio con acceso al servidor MCP.
- `COMPOSIO_USER_ID`: usuario de Composio que tiene conectados Exa, Notion y Gmail.
- `NOTION_PARENT_ID`: `3bce452e-012f-8024-b193-daa7632f731e` (página `Noticias IA diarias`).

El flujo usa como remitente `rfeg1980@gmail.com` y destinatario `rfespinosagarcia@gmail.com`.

## Ejecución de prueba

El workflow incluye `workflow_dispatch`, por lo que puede probarse manualmente desde la pestaña **Actions** antes de confiar en el horario diario.
