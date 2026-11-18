"""Inventario de bases de datos: todos los perfiles de ~/.aws/config × regiones, en paralelo.

RDS no habla SSM: se llega por una instancia EC2 con el agente en línea que vea el endpoint (un bastión). Por región
se piden las bases (rds:DescribeDBClusters y DescribeDBInstances) y las instancias con SSM en línea, con su VPC, para
proponer el bastión. El resultado se guarda en ~/.cache para enseñarlo al abrir sin esperar.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .config import Config

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "infranettone-rds-connect" / "databases.json"
BASTION_HINT = re.compile(r"bastion|bastión|jump|proxy|ssm", re.I)


@dataclass
class Bastion:
    id: str
    name: str
    vpc: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})" if self.name else self.id


@dataclass
class Database:
    profile: str
    account: str
    region: str
    id: str
    cluster: bool  # Aurora / clúster multi-AZ: endpoint de escritura y de lectura
    engine: str
    version: str
    status: str
    host: str
    port: int
    vpc: str
    user: str = ""
    db_name: str = ""
    secret_arn: str = ""  # contraseña maestra gestionada en Secrets Manager
    # Secretos creados aparte que mencionan la base (nombre, descripción o etiquetas de RDS): solo para la URL.
    related_secrets: list[str] = field(default_factory=list)
    iam_auth: bool = False
    reader_host: str = ""
    instance_class: str = ""
    # Instancias con SSM en línea de la región: primero las de su VPC y las que se llaman «bastion», «jump»…
    bastions: list[Bastion] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.profile}/{self.region}/{self.id}"

    @property
    def family(self) -> str:
        """postgres, mysql (también MariaDB y Aurora MySQL), sqlserver, oracle…"""
        e = self.engine.removeprefix("aurora-")
        if e.startswith("postgres"):  # postgres y aurora-postgresql
            return "postgres"
        return "mysql" if e in ("aurora", "mariadb") else e.split("-")[0]

    @property
    def secrets(self) -> list[tuple[str, str]]:
        """(nombre, URL en la consola) del secreto enlazado o, si no hay, de los encontrados por nombre. La consola
        busca por nombre: el ARN termina en «secret:<nombre>-XXXXXX» (sufijo aleatorio que no es del nombre)."""
        out = []
        for arn in [self.secret_arn] if self.secret_arn else self.related_secrets:
            name = arn.split(":secret:", 1)[-1].rsplit("-", 1)[0]
            out.append((name, f"https://{self.region}.console.aws.amazon.com/secretsmanager/secret"
                              f"?name={quote(name, safe='')}&region={self.region}"))
        return out

    @property
    def secret_url(self) -> str:
        return self.secrets[0][1] if self.secrets else ""

    @property
    def available(self) -> bool:
        return self.status == "available" and bool(self.host)

    def matches(self, text: str) -> bool:
        haystack = " ".join((self.id, self.profile, self.account, self.region, self.engine, self.host, self.vpc,
                             self.instance_class, self.db_name)).lower()
        return all(w in haystack for w in text.lower().split())


@dataclass
class ProfileError:
    profile: str
    message: str
    sso: bool  # se arregla con aws sso login


def profiles() -> list[str]:
    import botocore.session

    return sorted(botocore.session.Session().available_profiles)


def _session(profile: str):
    import boto3

    return boto3.Session(profile_name=profile)


def _is_sso_error(e: Exception) -> bool:
    name = type(e).__name__
    return name in ("SSOTokenLoadError", "UnauthorizedSSOTokenError", "TokenRetrievalError") or "sso" in str(e).lower()


def _regions(sess, cfg: Config) -> list[str]:
    if cfg.regions:
        return cfg.regions
    ec2 = sess.client("ec2", region_name=sess.region_name or cfg.default_region)
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


def _bastions(sess, region: str) -> list[Bastion]:
    ssm = sess.client("ssm", region_name=region)
    online = {i["InstanceId"] for page in ssm.get_paginator("describe_instance_information").paginate()
              for i in page["InstanceInformationList"] if i.get("PingStatus") == "Online"}
    if not online:
        return []
    found = []
    ec2 = sess.client("ec2", region_name=region)
    for page in ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for r in page["Reservations"]:
            for i in r["Instances"]:
                if i["InstanceId"] in online:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    found.append(Bastion(i["InstanceId"], tags.get("Name", ""), i.get("VpcId", "")))
    return found


def _rank(bastions: list[Bastion], vpc: str) -> list[Bastion]:
    # Las de otra VPC solo llegan con peering o Transit Gateway: se dejan, pero al final.
    return sorted(bastions, key=lambda b: (b.vpc != vpc, not BASTION_HINT.search(b.name), b.name.lower(), b.id))


def _scan_region(sess, profile: str, account: str, region: str) -> list[Database]:
    rds = sess.client("rds", region_name=region)
    instances = [i for page in rds.get_paginator("describe_db_instances").paginate() for i in page["DBInstances"]]
    vpc_of = {i["DBInstanceIdentifier"]: i.get("DBSubnetGroup", {}).get("VpcId", "") for i in instances}
    # Los clústeres de DocumentDB y Neptune también salen en la API de RDS: no se filtran, el túnel vale igual.
    clusters = [c for page in rds.get_paginator("describe_db_clusters").paginate() for c in page["DBClusters"]]
    if not instances and not clusters:
        return []

    found: list[Database] = []
    in_cluster = set()
    for c in clusters:
        members = [m["DBInstanceIdentifier"] for m in c.get("DBClusterMembers", [])]
        in_cluster.update(members)
        found.append(Database(
            profile, account, region, c["DBClusterIdentifier"], True, c["Engine"], c.get("EngineVersion", ""),
            c.get("Status", ""), c.get("Endpoint", ""), c.get("Port", 0),
            next((vpc_of[m] for m in members if vpc_of.get(m)), ""), c.get("MasterUsername", ""),
            c.get("DatabaseName", ""), c.get("MasterUserSecret", {}).get("SecretArn", ""),
            c.get("IAMDatabaseAuthenticationEnabled", False), c.get("ReaderEndpoint", ""),
            c.get("DBClusterInstanceClass", "")))
    for i in instances:
        if i["DBInstanceIdentifier"] in in_cluster:
            continue  # se entra por el endpoint del clúster
        ep = i.get("Endpoint", {})
        found.append(Database(
            profile, account, region, i["DBInstanceIdentifier"], False, i["Engine"], i.get("EngineVersion", ""),
            i.get("DBInstanceStatus", ""), ep.get("Address", ""), ep.get("Port", 0),
            vpc_of[i["DBInstanceIdentifier"]], i.get("MasterUsername", ""), i.get("DBName", ""),
            i.get("MasterUserSecret", {}).get("SecretArn", ""), i.get("IAMDatabaseAuthenticationEnabled", False),
            instance_class=i.get("DBInstanceClass", "")))

    bastions = _bastions(sess, region)
    secrets = _secrets(sess, region)
    for d in found:
        d.bastions = _rank(bastions, d.vpc)
        if not d.secret_arn:
            d.related_secrets = _related(secrets, d)
    return found


def _secrets(sess, region: str) -> list[dict]:
    """Lista de secretos de la región (sin sus valores). Sin permiso, ninguno: es solo una ayuda."""
    try:
        sm = sess.client("secretsmanager", region_name=region)
        return [s for page in sm.get_paginator("list_secrets").paginate() for s in page["SecretList"]]
    except Exception:  # noqa: BLE001
        return []


def _tokens(text: str) -> set[str]:
    # El guion no separa: forma parte de los ids de RDS («mi-base» no es «mi-base-ro»).
    return set(re.split(r"[/_.:\s,;|]+", text.lower())) - {""}


def _related(secrets: list[dict], d: Database) -> list[str]:
    """Secretos que nombran la base como palabra entera, nunca como trozo («base» no casa con «base2» ni
    «baseta»). Primero los etiquetados por RDS con su ARN, luego por nombre y por último por descripción."""
    db = d.id.lower()
    tagged, named, described = [], [], []
    for s in secrets:
        tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
        description = s.get("Description", "")
        if any(v.endswith((f":db:{d.id}", f":cluster:{d.id}")) for v in tags.values()):
            tagged.append(s["ARN"])
        elif db in _tokens(s.get("Name", "")):
            named.append(s["ARN"])
        elif db in _tokens(description) or d.host and d.host.lower() in _tokens(description):
            described.append(s["ARN"])
    return tagged + named + described


def scan(cfg: Config, log: Callable[[str], None]) -> tuple[list[Database], list[ProfileError]]:
    """Recorre todos los perfiles; los que fallan (sin credenciales, SSO caducado) van a la lista de errores."""
    databases: list[Database] = []
    errors: list[ProfileError] = []
    # Varios perfiles pueden ser la misma cuenta con otro rol: se recorre solo el primero de cada cuenta.
    seen_accounts: dict[str, str] = {}
    jobs = []

    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        def prepare(profile: str):
            sess = _session(profile)
            account = sess.client("sts", region_name=sess.region_name or cfg.default_region) \
                .get_caller_identity()["Account"]
            return sess, account, _regions(sess, cfg)

        prepared = {p: pool.submit(prepare, p) for p in profiles() if p not in cfg.skip_profiles}
        for profile, fut in prepared.items():
            try:
                sess, account, regions = fut.result()
            except Exception as e:  # noqa: BLE001 - cada perfil falla por su cuenta
                errors.append(ProfileError(profile, str(e), _is_sso_error(e)))
                log(f"✖ {profile}: {e}")
                continue
            if account in seen_accounts:
                log(f"{profile}: cuenta {account} ya recorrida con {seen_accounts[account]}")
                continue
            seen_accounts[account] = profile
            log(f"{profile} ({account}): {len(regions)} regiones")
            jobs += [(profile, region, pool.submit(_scan_region, sess, profile, account, region))
                     for region in regions]

        for profile, region, fut in jobs:
            try:
                databases += fut.result()
            except Exception as e:  # noqa: BLE001 - región sin permisos o deshabilitada
                log(f"⚠ {profile} {region}: {e}")

    databases.sort(key=lambda d: (d.profile, d.region, d.id))
    save_cache(databases)
    log(f"{len(databases)} bases de datos, {sum(bool(d.bastions) for d in databases)} con algún bastión")
    return databases, errors


def save_cache(databases: list[Database]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps([asdict(d) for d in databases], indent=1) + "\n")


def cached() -> list[Database]:
    try:
        out = []
        for d in json.loads(CACHE.read_text()):
            d["bastions"] = [Bastion(**b) for b in d.get("bastions", [])]
            out.append(Database(**d))
        return out
    except (OSError, ValueError, TypeError):
        return []
