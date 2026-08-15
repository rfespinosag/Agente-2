# Guía paso a paso: agente diario con Composio MCP, Exa, Notion, Gmail y GitHub Actions

## 1. Objetivo

Construir un agente que:

1. Se ejecute automáticamente todos los días desde GitHub Actions.
2. Busque exclusivamente con Exa noticias internacionales relevantes de inteligencia artificial de las últimas 72 horas.
3. Cree una página nueva en Notion con:
   - las noticias resumidas;
   - una tabla con los precios actuales de diez empresas líderes relacionadas con IA;
   - el enlace original de cada noticia.
4. Envíe por Gmail un resumen y el enlace de la página de Notion.
5. Funcione sin usar la API de OpenAI ni una clave `OPENAI_API_KEY`.

La arquitectura final es:

```text
GitHub Actions
      ↓
Python + MCP
      ↓
Composio MCP
   ↙     ↓      ↘
Exa   Notion   Gmail
```

La computadora personal no necesita estar encendida: GitHub ejecuta el flujo en sus propios servidores.

## 2. Componentes necesarios

Antes de comenzar, tener preparados:

- una cuenta de Composio Platform;
- un proyecto nuevo de Composio;
- una API key del proyecto de Composio;
- un `user_id` de Composio;
- conexiones activas de Exa, Notion y Gmail dentro del mismo proyecto/usuario;
- una página padre de Notion compartida con la conexión de Notion;
- un repositorio nuevo de GitHub;
- GitHub Actions habilitado;
- permiso para crear y modificar archivos del repositorio y sus Secrets.

No se deben subir claves, tokens, contraseñas ni archivos locales con secretos al repositorio.

## 3. Crear y preparar el proyecto en Composio

### 3.1 Elegir el área correcta

En Composio se debe trabajar en **Platform**, no en **For You**.

- **For You** conecta aplicaciones para clientes de IA.
- **Platform** permite administrar proyectos, usuarios, conexiones, toolkits y agentes mediante el SDK/MCP.

### 3.2 Crear el proyecto

1. Abrir Composio Platform.
2. Crear un proyecto dedicado para este agente.
3. Copiar la API key del proyecto.
4. Identificar el `user_id` que tiene las conexiones del agente.

El `user_id` es especialmente importante. GitHub debe usar exactamente el mismo usuario que aparece en Composio con las conexiones activas.

### 3.3 Conectar las aplicaciones

Dentro del proyecto, abrir **Toolkits** y conectar:

- Exa: mediante API key.
- Notion: mediante OAuth o la opción disponible.
- Gmail: mediante OAuth.

Después revisar **Users → Connected Accounts** y confirmar que las tres aplicaciones aparecen como **Active**.

Puede aparecer una cuenta con estado **Initiated** además de otra **Active**. La cuenta Initiated es una autorización incompleta y no debe usarse. Se puede eliminar después de confirmar que existe una cuenta Active.

Una conexión Active puede mostrar `accounts: []` en el diagnóstico MCP. Eso no significa necesariamente que esté rota: Composio puede seleccionar automáticamente la cuenta activa predeterminada. En ese caso, no se debe inventar un ID ni enviar `account: ""`; se omite el parámetro `account`.

## 4. Preparar Notion

1. Crear una página padre, por ejemplo `Reportes financieros diarios — Agente 2`.
2. Compartir esa página con la integración de Notion conectada en Composio.
3. Copiar el ID de la página desde la URL.

En una URL como:

```text
https://www.notion.so/Reportes-financieros-diarios-3bce452e012f80998465ebb0e2a5df51
```

el ID normalmente es la cadena de 32 caracteres del final:

```text
3bce452e012f80998465ebb0e2a5df51
```

La página padre debe ser accesible por la conexión de Notion; de lo contrario el agente puede investigar correctamente, pero fallará al crear la página.

## 5. Preparar Gmail

Confirmar:

- cuenta remitente: la cuenta Gmail conectada en Composio;
- destinatario: la cuenta que recibirá el informe;
- permiso de envío concedido durante OAuth.

El agente debe devolver un ID del mensaje o del hilo después de enviar el correo. Que la llamada no muestre un error no basta.

Para este caso:

```text
Remitente: rfeg1980@gmail.com
Destino:   rfespinosagarcia@gmail.com
```

## 6. Crear el repositorio de GitHub

Se recomienda un repositorio dedicado por agente. Por ejemplo:

```text
usuario/Agente-finanzas-diarias
```

Estructura recomendada:

```text
Agente-finanzas-diarias/
├── .github/
│   └── workflows/
│       └── daily-finance-report.yml
├── scripts/
│   └── daily_report.py
├── requirements.txt
└── .gitignore
```

El `.gitignore` debe incluir, como mínimo:

```text
.env
.env.*
!.env.example
*.txt
```

Si se necesita conservar un archivo de instrucciones local que contenga secretos, debe añadirse con su nombre específico al `.gitignore`.

## 7. Configurar los Secrets de GitHub

En el repositorio abrir:

**Settings → Secrets and variables → Actions → New repository secret**

Crear:

```text
COMPOSIO_API_KEY   = clave del proyecto Composio
COMPOSIO_USER_ID   = user_id de Composio con Exa, Notion y Gmail activos
NOTION_PARENT_ID   = ID de la página padre de Notion
```

No crear ni copiar `OPENAI_API_KEY`: este agente no utiliza la API de OpenAI.

Los correos pueden configurarse como variables no secretas en el workflow si no existe una política que exija ocultarlos. Una opción más segura es crear también:

```text
GMAIL_FROM
GMAIL_TO
```

Nunca imprimir los valores de los Secrets en los logs.

## 8. Configurar el horario

GitHub Actions usa cron en UTC. Para las 8:00 AM de Ciudad de México, el cron utilizado en este proceso fue:

```yaml
schedule:
  - cron: "0 14 * * *"
```

El horario debe revisarse si cambia la zona horaria o si GitHub modifica el comportamiento del horario de verano. Mantener también `workflow_dispatch` para poder ejecutar una prueba manual:

```yaml
on:
  schedule:
    - cron: "0 14 * * *"
  workflow_dispatch:
```

## 9. Dependencias de Python

El proyecto debe instalar las dependencias del cliente MCP y del transporte HTTP. Una base funcional es:

```text
composio
mcp
httpx2
```

En el cliente MCP se debe usar un cliente HTTP compatible, por ejemplo:

```python
async with httpx.AsyncClient(
    headers=session.mcp.headers,
    follow_redirects=True,
) as http_client:
    async with streamable_http_client(
        session.mcp.url,
        http_client=http_client,
    ) as (read_stream, write_stream):
        # Crear ClientSession y ejecutar llamadas MCP aquí.
        pass
```

Errores que se deben evitar:

- No pasar `headers=` directamente a `streamable_http_client`; algunas versiones producen `unexpected keyword argument 'headers'`.
- No asumir que el transporte devuelve tres valores. En la versión utilizada devuelve dos: `(read_stream, write_stream)`.

## 10. Cómo usar las herramientas de Composio MCP

El endpoint MCP de Composio no necesariamente expone directamente herramientas llamadas `EXA_ANSWER`, `NOTION_CREATE_PAGE` o `GMAIL_SEND_EMAIL`.

En el proceso validado, el endpoint expuso principalmente estas meta-herramientas:

```text
COMPOSIO_SEARCH_TOOLS
COMPOSIO_GET_TOOL_SCHEMAS
COMPOSIO_MULTI_EXECUTE_TOOL
```

El orden correcto es:

1. Abrir la sesión MCP.
2. Ejecutar `COMPOSIO_SEARCH_TOOLS` con búsquedas separadas para:
   - investigación de noticias con Exa;
   - creación de una página en Notion;
   - envío de correo con Gmail.
3. Guardar el `session.id` devuelto por la búsqueda.
4. Reutilizar ese mismo `session.id` en las llamadas posteriores.
5. Consultar esquemas si los argumentos no están claros.
6. Ejecutar las herramientas descubiertas mediante `COMPOSIO_MULTI_EXECUTE_TOOL`.

No se debe llamar directamente a una herramienta Exa que no aparezca en la lista del endpoint. El error `Tool EXA_ANSWER not found` indica que se intentó usar una herramienta upstream directamente en lugar de descubrirla y ejecutarla mediante las meta-herramientas de Composio.

## 11. Orden de ejecución del agente

El script debe seguir este orden:

### Paso A: investigación

Con Exa:

- buscar noticias publicadas en las últimas 72 horas;
- filtrar por relevancia internacional y actualidad;
- seleccionar tres noticias para `Novedades IA` y tres para `Finanzas IA`;
- conservar título, resumen, fecha y URL original.

También consultar una tabla actualizada con precio y cambio del día para estas diez empresas relacionadas con IA: NVIDIA (NVDA), Microsoft (MSFT), Alphabet (GOOGL), Amazon (AMZN), Meta Platforms (META), Broadcom (AVGO), AMD (AMD), Oracle (ORCL), Palantir (PLTR) y TSMC (TSM).

### Paso B: creación de Notion

Crear una página hija dentro de `NOTION_PARENT_ID` con un título fechado, por ejemplo:

```text
Most relevant IA NEWS — 2026-08-14
```

Contenido recomendado:

```text
**Novedades IA**

1. [Título]
Resumen: [resumen]
Fuente original: [URL]

2. [Título]
Resumen: [resumen]
Fuente original: [URL]

3. [Título]
Resumen: [resumen]
Fuente original: [URL]

**Finanzas IA**

1. [Título]
Resumen: [resumen]
Fuente original: [URL]

**Precios de acciones de empresas líderes en IA**

| Empresa | Ticker | Precio actual | Cambio del día | Fecha de mercado | Fuente |
|---|---|---:|---:|---|---|
| [Empresa] | [Ticker] | [Precio] | [Cambio] | [Fecha] | [URL] |
```

### Paso C: envío de Gmail

Enviar el correo solamente después de confirmar que Notion devolvió un ID de página o URL.

El correo debe incluir:

- fecha de consulta;
- titulares de las seis noticias;
- resúmenes breves;
- tabla de acciones;
- enlaces originales;
- enlace a la página de Notion.

Después del envío, confirmar que Gmail devolvió un `message_id` o `thread_id`.

## 12. Validación correcta

Una ejecución verde de GitHub solo significa que el proceso terminó con código 0. No prueba por sí sola que se haya creado la página o enviado el correo.

La última parte del script debe validar:

```json
{
  "notion_url": "https://app.notion.com/p/...",
  "email_id": "...",
  "sent_to": "destinatario@ejemplo.com"
}
```

El job debe fallar si falta cualquiera de estos datos o si una respuesta tiene `successful: false`.

Después de una prueba manual:

1. Abrir el log de `Run daily report`.
2. Buscar la línea JSON final.
3. Abrir `notion_url` y confirmar el contenido.
4. Revisar Gmail y confirmar el mensaje enviado.
5. Revisar Composio → Sessions/Logs para confirmar las llamadas a Exa, Notion y Gmail.
6. Solo después dejar activo el horario diario.

## 13. Diagnóstico de errores conocidos

### `No active Composio account found for exa`

Comprobar que `COMPOSIO_USER_ID` sea exactamente el usuario que tiene las conexiones activas. Si Composio informa:

```json
{"toolkit":"exa","has_active_connection":true,"accounts":[]}
```

no se debe concluir automáticamente que no hay conexión. Omitir el campo opcional `account` para que Composio seleccione la cuenta activa predeterminada.

### `Tool EXA_ANSWER not found`

El script está intentando llamar una herramienta upstream directamente. Ejecutar primero búsqueda de herramientas y después `COMPOSIO_MULTI_EXECUTE_TOOL`.

### `streamable_http_client() got an unexpected keyword argument 'headers'`

Pasar los headers al cliente `httpx2.AsyncClient` y luego pasar ese objeto con `http_client=`.

### `not enough values to unpack`

Cambiar el desempaquetado a:

```python
async with streamable_http_client(...) as (read_stream, write_stream):
```

### GitHub verde, pero sin página ni correo

Revisar que el script no esté ignorando respuestas fallidas. Exigir `successful`, el ID/URL de Notion y el ID del mensaje de Gmail antes de devolver código 0.

### Warning de Node.js 20 deprecado

Si el job termina en verde, el warning de `actions/checkout` o `actions/setup-python` es informativo y no es la causa del problema principal.

## 14. Lista de comprobación final

- [ ] Proyecto correcto seleccionado en Composio Platform.
- [ ] API key de Composio guardada únicamente en GitHub Secrets.
- [ ] `COMPOSIO_USER_ID` coincide con el usuario de las conexiones activas.
- [ ] Exa aparece como Active.
- [ ] Notion aparece como Active y tiene acceso a la página padre.
- [ ] Gmail aparece como Active y tiene permiso de envío.
- [ ] El repositorio tiene GitHub Actions habilitado.
- [ ] El workflow incluye `workflow_dispatch`.
- [ ] El cron está convertido correctamente a UTC.
- [ ] El workflow no contiene claves en texto plano.
- [ ] El cliente MCP usa `http_client=` y dos streams.
- [ ] Las herramientas se descubren mediante `COMPOSIO_SEARCH_TOOLS`.
- [ ] Se reutiliza el `session.id` de la búsqueda.
- [ ] No se envía `account: ""` ni un ID inventado.
- [ ] El script valida las respuestas.
- [ ] El log imprime `notion_url`, `email_id` y `sent_to`.
- [ ] La página de Notion fue comprobada manualmente.
- [ ] El correo fue comprobado manualmente.
- [ ] Las sesiones de Composio muestran las llamadas esperadas.

## 15. Regla de seguridad

Nunca pegar en el código, en los logs, en Notion ni en esta guía:

- API keys;
- tokens OAuth;
- contraseñas;
- headers de autorización completos;
- archivos `.env`;
- credenciales exportadas desde Composio.

Si una clave aparece accidentalmente en un commit o log, revocarla y generar una nueva antes de continuar.
