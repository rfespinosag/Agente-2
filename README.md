# Rogelio IA news

Este agente se ejecuta diariamente a las 08:00 de Monterrey, Nuevo León, México mediante GitHub Actions.
Usa exclusivamente Exa para investigar noticias de inteligencia artificial de las últimas 24 horas, Composio como servidor MCP, Notion para publicar el reporte y Gmail para enviarlo.

El resumen se titula `Noticias globales IA de las últimas 24 hrs` y contiene dos bloques:

- `Novedades IA`: tres noticias sobre lanzamientos, desarrollos o nuevas capacidades de inteligencia artificial.
- `Finanzas IA`: tres noticias sobre inversiones, adquisiciones, valuaciones, resultados o estrategia financiera relacionada con IA.

Cada noticia se solicita en español y debe terminar con un enlace a su fuente original.

No usa el SDK ni la API de OpenAI.

## Secretos requeridos

Configura estos secretos en `Settings → Secrets and variables → Actions`:

- `COMPOSIO_API_KEY`: clave de proyecto de Composio con acceso al servidor MCP.
- `COMPOSIO_USER_ID`: usuario de Composio que tiene conectados Exa, Notion y Gmail.
- `NOTION_PARENT_ID`: `3bce452e-012f-8024-b193-daa7632f731e` (página `Noticias IA diarias`).

El flujo usa como remitente `rfeg1980@gmail.com` y destinatario `rfespinosagarcia@gmail.com`.

## Ejecución de prueba

El workflow incluye `workflow_dispatch`, por lo que puede probarse manualmente desde la pestaña **Actions** antes de confiar en el horario diario.
