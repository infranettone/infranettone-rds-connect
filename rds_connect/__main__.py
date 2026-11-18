import argparse
import sys

from . import inventory, session
from .config import Config
from .version import version


def _find(target: str, refresh: bool) -> inventory.Database:
    """Por clave perfil/región/id, por id exacto o por texto si solo hay una que case."""
    pool = inventory.cached()
    if refresh or not pool:
        pool, _ = inventory.scan(Config.load(), lambda m: print(m, file=sys.stderr))
    hits = [d for d in pool if d.key == target] or [d for d in pool if d.id == target] \
        or [d for d in pool if d.matches(target)]
    if len(hits) != 1:
        found = "\n".join(f"  {d.key}  {d.engine}" for d in hits[:20])
        raise ValueError(f"{len(hits)} bases de datos casan con «{target}»" + (f":\n{found}" if found else ""))
    return hits[0]


def _bastion(d: inventory.Database, via: str | None) -> str:
    chosen = via or Config.load().bastions.get(d.key) or (d.bastions[0].id if d.bastions else "")
    if not chosen:
        raise RuntimeError(f"No hay ninguna instancia con SSM en línea en {d.profile} {d.region} (usa --via)")
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(prog="infranettone-rds-connect",
                                     description="Conecta por SSM a las bases RDS de todas las cuentas de AWS")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--list", nargs="?", const="", metavar="FILTRO", help="lista las bases de datos (de la caché)")
    g.add_argument("--scan", action="store_true", help="recorre todas las cuentas y regiones")
    g.add_argument("--tunnel", metavar="BASE", help="abre el túnel (id, perfil/región/id o texto)")
    g.add_argument("--client", metavar="BASE", help="túnel y psql/mysql con la contraseña de Secrets Manager")
    parser.add_argument("--via", help="instancia (bastión) por la que pasa el túnel")
    parser.add_argument("--port", type=int, default=0, help="con --tunnel: puerto local")
    parser.add_argument("--reader", action="store_true", help="endpoint de lectura del clúster")
    parser.add_argument("--refresh", action="store_true", help="vuelve a recorrer AWS antes de buscar")
    args = parser.parse_args()

    try:
        if args.list is not None or args.scan:
            pool = inventory.scan(Config.load(), lambda m: print(m, file=sys.stderr))[0] if args.scan \
                else inventory.cached()
            for d in pool:
                if not args.list or d.matches(args.list):
                    print(f"{'●' if d.available and d.bastions else '○'} {d.id[:36]:36} {d.engine[:18]:18} "
                          f"{d.status[:10]:10} {d.profile} {d.region}")
        elif args.tunnel or args.client:
            d = _find(args.tunnel or args.client, args.refresh)
            bastion = _bastion(d, args.via)
            if args.client:
                return session.client(d, bastion, args.reader)
            return session.tunnel(d, bastion, args.port or session.free_port(), args.reader)
        else:
            from .ui import App
            return App().run([sys.argv[0]])
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.client or args.tunnel:
            input("Pulsa Intro para cerrar…")  # en la terminal que abre la app, para que se lea el error
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
