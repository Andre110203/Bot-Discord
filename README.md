# Bot GFS

Esta versión conserva los comandos de Habbo y consulta de forma segura la cola de registro de la web GFS.

## Inicio

1. Instala Python 3.10 o superior.
2. Ejecuta `pip install -r requirements.txt`.
3. Configura las variables de `.env.example` en el servicio donde alojas el bot.
4. Inicia con `python bot.py`.

El token de Discord y `BOT_API_SECRET` deben configurarse como variables de entorno; nunca deben escribirse dentro del código ni publicarse.

La conexión con la web solamente se activa cuando existe `BOT_API_SECRET`. La base SQLite se crea en `data/verification.sqlite3`; el alojamiento debe conservar esa carpeta entre reinicios.

## Primera prueba, sin modificar la web

Configura `DISCORD_TOKEN`, `GUILD_ID`, `LOG_CHANNEL_NAME`, `VERIFIED_ROLE_NAME`, `GFS_WEB_URL` y el `BOT_API_SECRET` indicado en el archivo privado entregado. Los comandos `/perfil`, `/avatar`, `/verificar` y `/panel_verificacion` seguirán funcionando normalmente. Cada cinco segundos el bot consultará si la web tiene una solicitud pendiente y, si el Habbo está vinculado, enviará el código por DM.

Después de verificar un usuario comprueba que se haya creado `data/verification.sqlite3`. No borres ese archivo al reiniciar o actualizar el bot, porque contiene la relación segura entre Discord y Habbo que posteriormente utilizará el registro web.
