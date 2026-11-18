# Guía para agentes (y humanos con prisa)

App de escritorio para Ubuntu (Python + GTK 4 + libadwaita ≥ 1.5) que lista las bases RDS/Aurora de todas las
cuentas de AWS configuradas y conecta a ellas con un túnel de SSM por un bastión. Uso: [README.md](README.md).
Es la hermana de [infranettone-ec2-connect](https://github.com/infranettone/infranettone-ec2-connect): misma
estructura; un arreglo en lo común (recorrido, SSO, terminal, empaquetado) suele valer para las dos.

| Módulo | Qué hace |
|---|---|
| `rds_connect/inventory.py` | Perfiles × regiones en paralelo: clústeres e instancias de RDS y las EC2 con SSM en línea (bastiones, ordenados por VPC y nombre); caché en `~/.cache` |
| `rds_connect/session.py` | Túnel `AWS-StartPortForwardingSessionToRemoteHost` en una terminal; cliente = túnel + psql/mysql con la contraseña de Secrets Manager o token IAM |
| `rds_connect/config.py` | `~/.config/infranettone-rds-connect/config.json`: perfiles omitidos, regiones, bastión por base |
| `rds_connect/cheatsheet.py` | Chuleta de psql (solo datos: apartado → orden y descripción) |
| `rds_connect/ui.py` | Lista agrupada por perfil, buscador, diálogos de túnel y bastión, preferencias |
| `rds_connect/__main__.py` | Línea de órdenes (`--scan`, `--list`, `--tunnel`, `--client`) |

## Reglas del proyecto

- **El repositorio es público**: nada de ids de cuenta, nombres de perfiles, bases, endpoints, IP ni correos internos
  en código, docs, commits o ejemplos. Los datos reales solo viven en `~/.aws` y en la caché del usuario.
- **Todo en español** (UI, comentarios, commits, docs), con tildes correctas. Identificadores en inglés.
- Comentarios breves que explican el *porqué*. Sin linter: la CI hace `python3 -m compileall -q rds_connect`.
- Commits: título en español, **sin `Co-authored-by:`** ni ninguna otra línea de atribución. Releases: tag `v*` (SemVer).
- Lo lento va en hilos y vuelve a la UI con `GLib.idle_add`.
- A los procesos se les pasan listas, nunca un shell; perfil, región, bastión y endpoint se validan en `session._check`.
- **Las contraseñas no se guardan ni van en argv**: de Secrets Manager al entorno del cliente (`PGPASSWORD`,
  `MYSQL_PWD`) o al portapapeles, nunca a la caché.

## Detalles

- Las instancias que son miembro de un clúster no se listan: se entra por el endpoint del clúster (o el de lectura).
  La VPC del clúster se saca de sus miembros (`DescribeDBClusters` no la trae).
- DocumentDB y Neptune salen en la API de RDS: se listan y el túnel vale, pero no hay cliente.
- El cliente se ejecuta dentro de la terminal (`python3 -m rds_connect --client …`): al salir se cierra el túnel.
- Un bastión elegido a mano que ya no está en línea se ignora y se usa el primero de la lista.

## Probar

```bash
python3 -m rds_connect            # interfaz
python3 -m rds_connect --scan     # recorrido por terminal
packaging/build-deb.sh 0.1.0      # .deb en dist/
```
