"""Túneles de SSM a RDS y el cliente de la base por encima.

El túnel es `aws ssm start-session` con AWS-StartPortForwardingSessionToRemoteHost: el bastión abre la conexión al
endpoint y el plugin la expone en 127.0.0.1. Todo va en una ventana de terminal; los argumentos se pasan como lista,
nunca por un shell, y se validan antes.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from .inventory import Database

ID_RE = re.compile(r"^(i|mi)-[0-9a-f]{8,17}$")
NAME_RE = re.compile(r"^[\w.@+=,-]+$")  # perfiles y regiones
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
# Cliente por familia de motor; el primero que esté instalado.
CLIENTS = {"postgres": ("psql",), "mysql": ("mariadb", "mysql")}


def _check(d: Database, bastion: str, host: str) -> None:
    if not ID_RE.match(bastion) or not NAME_RE.match(d.profile) or not NAME_RE.match(d.region) \
            or not HOST_RE.match(host) or not 0 < d.port < 65536:
        raise ValueError(f"Datos no válidos: {d.profile} {d.region} {bastion} {host}:{d.port}")


def missing_tools() -> list[str]:
    return [t for t in ("aws", "session-manager-plugin") if not shutil.which(t)]


def client_for(d: Database) -> str | None:
    return next((c for c in CLIENTS.get(d.family, ()) if shutil.which(c)), None)


def _terminal(cmd: list[str], title: str) -> None:
    if shutil.which("ptyxis"):
        argv = ["ptyxis", "--new-window", "--", *cmd]
    elif shutil.which("gnome-terminal"):
        argv = ["gnome-terminal", f"--title={title}", "--", *cmd]
    elif shutil.which("kgx"):
        argv = ["kgx", f"--title={title}", "--", *cmd]
    elif shutil.which("x-terminal-emulator"):
        argv = ["x-terminal-emulator", "-e", *cmd]
    else:
        raise RuntimeError("No encuentro ninguna terminal (ptyxis, gnome-terminal…)")
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tunnel_command(d: Database, bastion: str, local_port: int, reader: bool = False) -> list[str]:
    host = d.reader_host if reader and d.reader_host else d.host
    _check(d, bastion, host)
    return ["aws", "ssm", "start-session", "--profile", d.profile, "--region", d.region, "--target", bastion,
            "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters", f"host={host},portNumber={d.port},localPortNumber={local_port}"]


def login_command(profile: str) -> list[str]:
    if not NAME_RE.match(profile):
        raise ValueError(f"Perfil no válido: {profile}")
    return ["aws", "sso", "login", "--profile", profile]


def open_tunnel(d: Database, bastion: str, local_port: int, reader: bool = False) -> None:
    """En la terminal corre `--tunnel`: la ficha de la base y el túnel, que dura lo que la ventana."""
    tunnel_command(d, bastion, local_port, reader)  # valida antes de abrir la ventana
    _terminal([sys.executable, "-m", "rds_connect", "--tunnel", d.key, "--via", bastion, "--port", str(local_port),
               *(["--reader"] if reader else [])], f"Túnel {local_port} → {d.id}")


def _style(code: str):
    on = sys.stdout.isatty()
    return lambda s: f"\033[{code}m{s}\033[0m" if on else s


def banner(d: Database, bastion: str, local_port: int, reader: bool = False) -> str:
    """Ficha de lo que hay al otro lado del túnel, para saber de un vistazo qué ventana es cuál."""
    bold, dim, green = _style("1"), _style("2"), _style("32")
    host = d.reader_host if reader and d.reader_host else d.host
    b = next((x for x in d.bastions if x.id == bastion), None)
    via = f"{b.label}" if b else bastion
    if b and d.vpc and b.vpc != d.vpc:
        via += f" · otra VPC ({b.vpc})"
    kind = ("clúster, endpoint de lectura" if reader else "clúster, endpoint de escritura") if d.cluster \
        else "instancia"
    scheme = "postgresql" if d.family == "postgres" else "mysql" if d.family == "mysql" else ""
    rows = [
        ("Base de datos", f"{bold(d.id)} ({kind})"),
        ("Motor", " ".join(x for x in (d.engine, d.version, d.instance_class and f"· {d.instance_class}") if x)),
        ("Estado", d.status),
        ("Cuenta", f"{d.account} · perfil {d.profile} · {d.region}"),
        ("Endpoint", f"{host}:{d.port}"),
        ("VPC", d.vpc),
        ("Bastión", via),
        ("Usuario", d.user),
        ("Base inicial", d.db_name or ("postgres" if d.family == "postgres" else "")),
        ("Contraseña", "Secrets Manager (gestionada por RDS)" if d.secret_arn
         else "IAM (token)" if d.iam_auth else "la de siempre (no hay secreto enlazado)"),
        ("Secreto", d.secret_url),
    ]
    width = max(len(k) for k, _ in rows)
    lines = ["", bold(f"  Túnel a {d.id}"), dim("  " + "─" * 60)]
    lines += [f"  {dim(k.ljust(width))}  {v}" for k, v in rows if v]
    lines += [dim("  " + "─" * 60),
              f"  {dim('Local'.ljust(width))}  {green(bold(f'127.0.0.1:{local_port}'))}  →  {host}:{d.port}"]
    if scheme:
        db = d.db_name or ("postgres" if scheme == "postgresql" else "")
        lines.append(f"  {dim('URL'.ljust(width))}  {scheme}://{d.user}@127.0.0.1:{local_port}/{db}")
    if d.family == "postgres":
        lines.append(f"  {dim('psql'.ljust(width))}  psql -h 127.0.0.1 -p {local_port} -U {d.user} -d {db}")
    elif d.family == "mysql":
        lines.append(f"  {dim('mysql'.ljust(width))}  mariadb --protocol=TCP -h 127.0.0.1 -P {local_port} "
                     f"-u {d.user} -p")
    lines.append("")
    return "\n".join(lines)


def tunnel(d: Database, bastion: str, local_port: int, reader: bool = False) -> int:
    """Ficha, túnel y aviso de cuándo está listo; Ctrl+C o cerrar la ventana lo cierran."""
    print(banner(d, bastion, local_port, reader))
    print(_style("2")("  Abriendo el túnel…"), flush=True)
    started = time.monotonic()
    proc = subprocess.Popen(tunnel_command(d, bastion, local_port, reader), stdout=subprocess.DEVNULL)
    try:
        _wait(proc, local_port)
        print(_style("32")(f"  ✔ Túnel listo en 127.0.0.1:{local_port} ({time.monotonic() - started:.0f} s, "
                           f"abierto a las {time.strftime('%H:%M')}). Ctrl+C o cerrar la ventana para cerrarlo."),
              flush=True)
        return proc.wait()
    except KeyboardInterrupt:
        print("\n  Túnel cerrado")
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


def open_login(profile: str) -> None:
    _terminal(login_command(profile), f"SSO {profile}")


def open_client(d: Database, bastion: str, reader: bool = False) -> None:
    """En la terminal corre `--client`: el túnel y, cuando está listo, psql o mysql. Al salir se cierra todo."""
    tunnel_command(d, bastion, 1, reader)  # valida antes de abrir la ventana
    _terminal([sys.executable, "-m", "rds_connect", "--client", d.key, "--via", bastion,
               *(["--reader"] if reader else [])], f"{d.id}")


def password(d: Database) -> str:
    """Contraseña maestra de Secrets Manager (si RDS la gestiona), o vacío."""
    if not d.secret_arn:
        return ""
    import boto3

    sm = boto3.Session(profile_name=d.profile, region_name=d.region).client("secretsmanager")
    return json.loads(sm.get_secret_value(SecretId=d.secret_arn)["SecretString"]).get("password", "")


def iam_token(d: Database, host: str) -> str:
    import boto3

    rds = boto3.Session(profile_name=d.profile, region_name=d.region).client("rds")
    return rds.generate_db_auth_token(DBHostname=host, Port=d.port, DBUsername=d.user)


def _wait(tunnel: subprocess.Popen, port: int) -> None:
    # El plugin tarda unos segundos en abrir el puerto local.
    for _ in range(60):
        if tunnel.poll() is not None:
            raise RuntimeError("El túnel de SSM se ha cerrado (¿bastión sin agente, sin permisos o sin ruta a la base?)")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("El túnel no ha abierto el puerto local")


def client(d: Database, bastion: str, reader: bool = False) -> int:
    """Túnel y cliente contra 127.0.0.1; se ejecuta dentro de la terminal (o desde la línea de órdenes)."""
    prog = client_for(d)
    if not prog:
        raise RuntimeError(f"No hay cliente para {d.engine}: sudo apt install postgresql-client o mariadb-client "
                           f"(o usa solo el túnel)")
    host = d.reader_host if reader and d.reader_host else d.host
    port = free_port()
    env = dict(os.environ)
    # La contraseña va por el entorno del cliente: ni en argv (se ve en ps) ni en disco.
    try:
        secret = password(d)
        if not secret and d.iam_auth and d.family == "postgres":
            secret = iam_token(d, host)  # IAM exige TLS: psql usa sslmode=prefer y RDS lo ofrece
            print("Autenticación por IAM (token de 15 minutos)")
    except Exception as e:  # noqa: BLE001 - sin permiso al secreto: que el cliente la pida
        print(f"No se puede leer la contraseña ({e}): la pedirá el cliente")
        secret = ""
    if d.family == "postgres":
        cmd = [prog, "-h", "127.0.0.1", "-p", str(port), "-U", d.user, "-d", d.db_name or "postgres"]
        if secret:
            env["PGPASSWORD"] = secret
    else:
        cmd = [prog, "--protocol=TCP", "-h", "127.0.0.1", "-P", str(port), "-u", d.user,
               *([d.db_name] if d.db_name else [])]
        if secret:
            env["MYSQL_PWD"] = secret
        else:
            cmd.insert(1, "-p")  # que la pida
    print(banner(d, bastion, port, reader), flush=True)
    tunnel = subprocess.Popen(tunnel_command(d, bastion, port, reader), stdout=subprocess.DEVNULL)
    try:
        _wait(tunnel, port)
        return subprocess.run(cmd, env=env).returncode
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(5)
        except subprocess.TimeoutExpired:
            tunnel.kill()
