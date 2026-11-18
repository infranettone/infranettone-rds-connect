# RDS por SSM

App de escritorio para Ubuntu (GTK4 + libadwaita) que recorre **todos los perfiles de `~/.aws/config`** y todas sus
regiones, lista las bases de datos **RDS y Aurora** (PostgreSQL, MySQL, MariaDB…) y conecta a ellas con un túnel de
**SSM Session Manager** a través de una instancia EC2 de la misma VPC (un bastión). Sin VPN, sin bases públicas y
sin puertos abiertos.

Documentación para desarrollar: [AGENTS.md](AGENTS.md). Para conectar a las instancias EC2:
[infranettone-ec2-connect](https://github.com/infranettone/infranettone-ec2-connect).

## Requisitos

- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) y
  [session-manager-plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
- Perfiles en `~/.aws/config` (claves, roles o SSO).
- En cada cuenta, una instancia con el agente de SSM que llegue a la base (su grupo de seguridad permitido en el de
  la base).
- Para abrir el cliente: `sudo apt install postgresql-client mariadb-client`.

## Instalar

1. Baja `infranettone-rds-connect_<versión>_all.deb` de la última
   [release](https://github.com/infranettone/infranettone-rds-connect/releases).
2. `sudo apt install ./infranettone-rds-connect_*_all.deb`
3. Abre **RDS por SSM** desde el menú.

## Uso

- Al abrir se ve la última lista guardada y se recorre AWS en segundo plano (botón ⟳ para repetir).
- Los clústeres (Aurora) salen una vez, con su endpoint de escritura y el de lectura; sus instancias no se repiten.
- El **bastión** se elige solo: una instancia con SSM en línea de la misma VPC, mejor si se llama «bastion», «jump»,
  «proxy»… En el menú ⋯ se puede elegir otro y se recuerda.
- Por fila:
  - **Cliente**: túnel y `psql` o `mariadb`/`mysql` en una terminal. Si RDS gestiona la contraseña maestra en
    **Secrets Manager**, entra sin pedirla (va por el entorno del cliente, nunca en la línea de órdenes ni en disco);
    con PostgreSQL y autenticación por IAM, con un token. Si no, la pide el cliente.
  - **Túnel**: solo el reenvío a `127.0.0.1` (el puerto de la base si está libre) para DBeaver, pgAdmin o tu aplicación.
  - Menú ⋯: elegir el bastión, copiar endpoint, endpoint de lectura, usuario, contraseña o la URL del secreto en la consola
    de Secrets Manager, abrir la base en la consola.
- **Comandos de psql** (botón 📖 de la barra): chuleta por apartados (navegar, permisos, exportar, tamaños,
  bloqueos, rendimiento…) con buscador; pulsar uno lo copia para pegarlo en el cliente.
- Los perfiles sin credenciales válidas salen arriba; los de SSO con **Iniciar sesión** (`aws sso login`).
- **Preferencias**: qué perfiles se recorren y regiones fijas (vacío = todas las habilitadas en cada cuenta).

Por terminal:

```bash
infranettone-rds-connect --scan                    # recorre AWS y lista
infranettone-rds-connect --list [FILTRO]           # de la caché
infranettone-rds-connect --client mi-base          # túnel + psql/mysql (id, perfil/región/id o texto)
infranettone-rds-connect --tunnel mi-base --port 5433 [--reader] [--via i-0123…]
```

## Permisos de IAM

`sts:GetCallerIdentity`, `ec2:DescribeRegions`, `ec2:DescribeInstances`, `rds:DescribeDBInstances`,
`rds:DescribeDBClusters`, `ssm:DescribeInstanceInformation`, `ssm:StartSession` (sobre el bastión y el documento
`AWS-StartPortForwardingSessionToRemoteHost`) y `ssm:TerminateSession`. Para encontrar el secreto de
las bases que no lo tienen enlazado, `secretsmanager:ListSecrets` (opcional: no lee valores). Para la contraseña,
`secretsmanager:GetSecretValue` (y `kms:Decrypt` si el secreto usa una clave propia); para IAM, `rds-db:connect`.

## Ficheros

- `~/.config/infranettone-rds-connect/config.json`: preferencias y bastión elegido por base.
- `~/.cache/infranettone-rds-connect/databases.json`: última lista (no sale del equipo; no guarda contraseñas).
