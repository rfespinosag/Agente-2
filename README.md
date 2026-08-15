# Rogelio IA news

Este agente se ejecuta diariamente a las 08:00 de Monterrey, Nuevo León, México mediante GitHub Actions.
Usa exclusivamente Exa para investigar noticias de inteligencia artificial de las últimas 72 horas, Composio como servidor MCP, Notion para publicar el reporte y Gmail para enviarlo.

The report is titled `Most relevant IA NEWS` and contains three blocks. News must be published within the exact last 72 hours:

- `AI Developments`: three stories about new AI launches, developments, or capabilities.
- `AI Finance`: three stories about AI investments, acquisitions, valuations, earnings, or financial strategy.
- `AI Leaders Stock Prices`: a table with current price, daily change, market date, and source for ten companies: NVDA, MSFT, GOOGL, AMZN, META, AVGO, AMD, ORCL, PLTR, and TSM.

Headlines and summaries are written in English, prioritizing original English-language sources. Each story must end with a link to its original source.

No usa el SDK ni la API de OpenAI.

## Secretos requeridos

Configura estos secretos en `Settings → Secrets and variables → Actions`:

- `COMPOSIO_API_KEY`: clave de proyecto de Composio con acceso al servidor MCP.
- `COMPOSIO_USER_ID`: usuario de Composio que tiene conectados Exa, Notion y Gmail.
- `NOTION_PARENT_ID`: `3bce452e-012f-8024-b193-daa7632f731e` (página `Noticias IA diarias`).

El flujo usa como remitente `rfeg1980@gmail.com` y destinatario `rfespinosagarcia@gmail.com`.

## Ejecución de prueba

El workflow incluye `workflow_dispatch`, por lo que puede probarse manualmente desde la pestaña **Actions** antes de confiar en el horario diario.
