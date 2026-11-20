"""Interfaz GTK4/libadwaita: todas las bases RDS por perfil, buscador y botones de túnel y cliente por SSM."""

import shlex
import socket
import threading
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import cheatsheet, inventory, session  # noqa: E402
from .config import Config  # noqa: E402
from .inventory import Database, ProfileError  # noqa: E402
from .version import version as app_version  # noqa: E402

APP_ID = "com.infranettone.RdsConnect"


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="RDS por SSM", default_width=1000, default_height=860)
        self.cfg = Config.load()
        self.busy = False
        self.databases: list[Database] = inventory.cached()
        self.errors: list[ProfileError] = []
        # Fila → base, y grupo → sus filas: el buscador solo cambia la visibilidad, no reconstruye.
        self.rows: list[tuple[Adw.ActionRow, Database]] = []
        self.groups: list[tuple[Adw.PreferencesGroup, list[Adw.ActionRow]]] = []

        self.toasts = Adw.ToastOverlay()
        self.search = Gtk.SearchEntry(placeholder_text="Buscar por id, motor, endpoint, cuenta, región…",
                                      hexpand=True, width_request=420)
        self.search.connect("search-changed", lambda *_: self._filter())
        self.ready_only = Gtk.ToggleButton(icon_name="network-transmit-receive-symbolic", active=True,
                                           tooltip_text="Solo las disponibles y con algún bastión con SSM")
        self.ready_only.connect("toggled", lambda *_: self._filter())

        header = Adw.HeaderBar(title_widget=self.search)
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                      tooltip_text="Recorrer todas las cuentas y regiones")
        self.refresh_btn.connect("clicked", lambda *_: self.scan())
        prefs = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Preferencias")
        prefs.connect("clicked", lambda *_: self._preferences())
        commands = Gtk.Button(icon_name="accessories-dictionary-symbolic", tooltip_text="Comandos típicos de psql")
        commands.connect("clicked", lambda *_: self._cheatsheet())
        header.pack_end(self.refresh_btn)
        header.pack_end(prefs)
        header.pack_end(commands)
        header.pack_start(self.ready_only)
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text=f"Acerca de · versión {app_version()}")
        about.connect("clicked", lambda *_: self._about())
        header.pack_start(about)

        self.page = Adw.PreferencesPage()
        self.summary = Gtk.Label(css_classes=["dim-label"], margin_top=6, wrap=True)
        self.progress_bar = Gtk.ProgressBar(show_text=True, margin_start=16, margin_end=16)
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, left_margin=8)
        log_box = Gtk.Expander(label="Registro", margin_start=16, margin_end=16,
                               child=Gtk.ScrolledWindow(child=self.log_view, min_content_height=140,
                                                        css_classes=["card"]))
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_bottom=12)
        for w in (self.summary, self.progress_bar, log_box):
            bottom.append(w)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.page.set_vexpand(True)
        content.append(self.page)
        content.append(bottom)
        view = Adw.ToolbarView(content=content)
        view.add_top_bar(header)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # Ctrl+F y escribir directamente van al buscador.
        self.search.set_key_capture_widget(self)

        self._build()
        missing = session.missing_tools()
        if missing:
            self.log(f"✖ Falta {', '.join(missing)}: sin eso no se puede abrir ningún túnel (ver README)")
        # Con la caché se ve todo al momento; la lista buena llega cuando termina el recorrido.
        self.scan()

    # --- trabajo en segundo plano ---

    def log(self, msg: str) -> None:
        def append():
            buf = self.log_view.get_buffer()
            buf.insert(buf.get_end_iter(), msg + "\n")
            self.log_view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
        GLib.idle_add(append)

    def toast(self, msg: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=4))

    def scan(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.refresh_btn.set_sensitive(False)
        self.progress_bar.set_text("Recorriendo cuentas y regiones…")
        pulse = GLib.timeout_add(150, lambda: self.progress_bar.pulse() or True)

        def worker():
            try:
                result = inventory.scan(self.cfg, self.log)
                GLib.idle_add(self._set_databases, *result)
            except Exception as e:  # noqa: BLE001 - lo mostramos en la UI
                self.log(traceback.format_exc())
                GLib.idle_add(self.toast, f"No se ha podido recorrer AWS: {e}")
            finally:
                GLib.idle_add(self._done, pulse)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, pulse: int) -> None:
        GLib.source_remove(pulse)
        self.busy = False
        self.refresh_btn.set_sensitive(True)
        self.progress_bar.set_text("")
        self.progress_bar.set_fraction(0)

    def _set_databases(self, databases: list[Database], errors: list[ProfileError]) -> None:
        self.databases, self.errors = databases, errors
        self._build()

    # --- lista ---

    def _build(self) -> None:
        for g, _ in self.groups:
            self.page.remove(g)
        self.rows, self.groups = [], []

        if self.errors:
            g = Adw.PreferencesGroup(title="Perfiles sin acceso",
                                     description="Sin credenciales válidas: no se ven sus bases de datos")
            for e in self.errors:
                row = Adw.ActionRow(title=e.profile, subtitle=GLib.markup_escape_text(e.message[:200]),
                                    subtitle_lines=2)
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
                if e.sso:
                    row.add_suffix(self._button("Iniciar sesión", lambda p=e.profile: self._login(p),
                                                "suggested-action"))
                row.add_suffix(self._button("", lambda p=e.profile: self._skip(p), "flat",
                                            icon="list-remove-symbolic", tooltip="No recorrer este perfil"))
                g.add(row)
            self.page.add(g)
            self.groups.append((g, []))

        by_profile: dict[str, list[Database]] = {}
        for d in self.databases:
            by_profile.setdefault(d.profile, []).append(d)
        for profile, items in by_profile.items():
            g = Adw.PreferencesGroup(title=GLib.markup_escape_text(profile),
                                     description=f"Cuenta {items[0].account}")
            rows = []
            for d in items:
                row = self._database_row(d)
                g.add(row)
                rows.append(row)
                self.rows.append((row, d))
            self.page.add(g)
            self.groups.append((g, rows))
        self._filter()

    def _bastion(self, d: Database) -> str:
        """El elegido a mano si sigue con SSM en línea; si no, el primero de la lista (misma VPC, «bastion»…)."""
        chosen = self.cfg.bastions.get(d.key)
        ids = [b.id for b in d.bastions]
        return chosen if chosen in ids else (ids[0] if ids else "")

    def _ready(self, d: Database) -> bool:
        return d.available and bool(self._bastion(d))

    def _database_row(self, d: Database) -> Adw.ActionRow:
        b = next((b for b in d.bastions if b.id == self._bastion(d)), None)
        details = [f"{d.engine} {d.version}".strip(), d.region, d.host, d.instance_class,
                   "clúster" if d.cluster else "", d.status if d.status != "available" else "",
                   f"por {b.label}" if b else "sin bastión con SSM"]
        row = Adw.ActionRow(title=GLib.markup_escape_text(d.id),
                            subtitle=GLib.markup_escape_text(" · ".join(x for x in details if x)),
                            subtitle_lines=2, activatable=self._ready(d))
        ready = self._ready(d)
        row.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic" if ready else "network-offline-symbolic",
                                 css_classes=[] if ready else ["dim-label"]))
        client = session.client_for(d)
        if client:
            btn = self._button("", lambda: self._open(session.open_client, d, self._bastion(d)), "flat",
                               icon="utilities-terminal-symbolic", tooltip=f"Túnel y {client}")
            btn.set_sensitive(ready)
            row.add_suffix(btn)
        tunnel = self._button("", lambda: self._tunnel(d), "flat", icon="network-wired-symbolic",
                              tooltip="Solo el túnel (para DBeaver, pgAdmin, tu aplicación…)")
        tunnel.set_sensitive(ready)
        row.add_suffix(tunnel)
        row.add_suffix(self._menu(d))
        if ready:
            row.connect("activated", lambda *_: self._open(session.open_client, d, self._bastion(d))
                        if client else self._tunnel(d))
        return row

    def _menu(self, d: Database) -> Gtk.MenuButton:
        items = [("Elegir el bastión…", lambda: self._choose_bastion(d)),
                 ("Copiar el endpoint", lambda: self._copy(f"{d.host}:{d.port}"))]
        if d.reader_host:
            items.append(("Copiar el endpoint de lectura", lambda: self._copy(f"{d.reader_host}:{d.port}")))
        if d.user:
            items.append(("Copiar el usuario maestro", lambda: self._copy(d.user)))
        if d.secret_arn:
            items.append(("Copiar la contraseña (Secrets Manager)", lambda: self._copy_password(d)))
        if d.secret_arn:
            items.append(("Copiar la URL del secreto", lambda: self._copy(d.secret_url)))
        else:
            # Encontrados por nombre: todos, con su nombre, para elegir el bueno (p. ej. uno por entorno).
            for name, url in d.secrets[:8]:
                items.append((f"Copiar la URL del secreto «{name}»", lambda url=url: self._copy(url)))
        items.append(("Abrir en la consola de AWS", lambda: Gtk.UriLauncher.new(
            f"https://{d.region}.console.aws.amazon.com/rds/home?region={d.region}"
            f"#database:id={d.id};is-cluster={'true' if d.cluster else 'false'}").launch(self, None, None, None)))
        menu, group = Gio.Menu(), Gio.SimpleActionGroup()
        for n, (label, cb) in enumerate(items):
            action = Gio.SimpleAction(name=f"a{n}")
            action.connect("activate", lambda *_, cb=cb: cb())
            group.add_action(action)
            menu.append(label, f"db.a{n}")
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                                css_classes=["flat"], tooltip_text="Más")
        button.insert_action_group("db", group)
        return button

    def _filter(self) -> None:
        text = self.search.get_text().strip()
        ready_only = self.ready_only.get_active()
        shown = 0
        for row, d in self.rows:
            visible = (not ready_only or self._ready(d)) and (not text or d.matches(text))
            row.set_visible(visible)
            shown += visible
        for g, rows in self.groups:
            g.set_visible(not rows or any(r.get_visible() for r in rows))
        self.summary.set_label(f"{shown} de {len(self.databases)} bases de datos · "
                               f"{sum(self._ready(d) for d in self.databases)} accesibles · "
                               f"{len({d.account for d in self.databases})} cuentas"
                               + (f" · {len(self.errors)} perfiles sin acceso" if self.errors else ""))

    # --- acciones ---

    def _button(self, label: str, cb, *classes: str, icon: str = "", tooltip: str = "") -> Gtk.Button:
        b = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=list(classes), tooltip_text=tooltip or None)
        if icon:
            b.set_icon_name(icon)
        else:
            b.set_label(label)
        b.connect("clicked", lambda *_: cb())
        return b

    def _copy(self, text: str, shown: str = "") -> None:
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toast(f"Copiado: {shown or text}")

    def _open(self, fn, *args) -> None:
        try:
            fn(*args)
        except (RuntimeError, ValueError, OSError) as e:
            self.toast(str(e))

    def _copy_password(self, d: Database) -> None:
        def worker():
            try:
                secret = session.password(d)
                GLib.idle_add(self._copy, secret, "la contraseña")
            except Exception as e:  # noqa: BLE001 - sin permiso al secreto
                GLib.idle_add(self.toast, f"No se puede leer el secreto: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _login(self, profile: str) -> None:
        self._open(session.open_login, profile)
        self.toast("Inicia sesión en el navegador y pulsa Actualizar")

    def _skip(self, profile: str) -> None:
        self.cfg.skip_profiles = sorted({*self.cfg.skip_profiles, profile})
        self.cfg.save()
        self.errors = [e for e in self.errors if e.profile != profile]
        self._build()

    def _dialog(self, heading: str, body: str, rows: list[Gtk.Widget], label: str, on_ok,
                extra: tuple[str, object] | None = None) -> None:
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        for r in rows:
            box.append(r)
        d = Adw.AlertDialog(heading=heading, body=body, extra_child=box)
        d.add_response("cancel", "Cancelar")
        if extra:
            d.add_response("extra", extra[0])
        d.add_response("ok", label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        d.connect("response", lambda _d, r: on_ok() if r == "ok" else (extra[1]() if r == "extra" else None))
        d.present(self)

    def _tunnel(self, d: Database) -> None:
        # El puerto de la base si está libre en este equipo: así sirven las conexiones ya guardadas en el cliente.
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", d.port))
            default = d.port
        except OSError:
            default = session.free_port()
        local = Adw.EntryRow(title="Puerto local", text=str(default))
        reader = Adw.SwitchRow(title="Endpoint de lectura", visible=bool(d.reader_host))
        rows = [local, reader]

        def port() -> int | None:
            try:
                p = int(local.get_text())
                if 0 < p < 65536:
                    return p
            except ValueError:
                pass
            self.toast("Puerto no válido")
            return None

        def ok():
            if (p := port()) is None:
                return
            self._open(session.open_tunnel, d, self._bastion(d), p, reader.get_active())
            self._copy(f"127.0.0.1:{p}")

        def copy_command():
            # Para lanzarlo a mano (otra terminal, tmux, un script): el mismo `aws ssm start-session`.
            if (p := port()) is None:
                return
            try:
                cmd = shlex.join(session.tunnel_command(d, self._bastion(d), p, reader.get_active()))
            except ValueError as e:
                self.toast(str(e))
                return
            self._copy(cmd, "el comando del túnel")

        self._dialog(f"Túnel a {d.id}", "Se queda abierto mientras esté la ventana de la terminal. "
                     "Conecta tu cliente a 127.0.0.1 con el puerto local.", rows, "Abrir túnel", ok,
                     extra=("Copiar comando", copy_command))

    def _choose_bastion(self, d: Database) -> None:
        if not d.bastions:
            self.toast(f"No hay instancias con SSM en línea en {d.profile} {d.region}")
            return
        labels = [f"{b.label}{'' if b.vpc == d.vpc else ' · otra VPC'}" for b in d.bastions]
        combo = Adw.ComboRow(title="Instancia", model=Gtk.StringList.new(labels))
        current = self._bastion(d)
        combo.set_selected(next((n for n, b in enumerate(d.bastions) if b.id == current), 0))

        def ok():
            self.cfg.bastions[d.key] = d.bastions[combo.get_selected()].id
            self.cfg.save()
            self._build()

        self._dialog(f"Bastión para {d.id}", "La instancia por la que pasa el túnel. Tiene que llegar al endpoint "
                     "(misma VPC o con ruta) y su grupo de seguridad estar permitido en el de la base.",
                     [combo], "Guardar", ok)

    def _preferences(self) -> None:
        d = Adw.PreferencesDialog(title="Preferencias")
        page = Adw.PreferencesPage()
        d.add(page)

        general = Adw.PreferencesGroup(title="Recorrido")
        regions = Adw.EntryRow(title="Regiones (separadas por comas; vacío = todas las habilitadas)",
                               text=", ".join(self.cfg.regions))
        general.add(regions)
        page.add(general)

        profiles = Adw.PreferencesGroup(title="Perfiles de ~/.aws/config",
                                        description="Los repetidos de una misma cuenta se recorren una vez")
        switches = {}
        for p in inventory.profiles():
            switches[p] = Adw.SwitchRow(title=GLib.markup_escape_text(p), active=p not in self.cfg.skip_profiles)
            profiles.add(switches[p])
        page.add(profiles)

        def closed(*_):
            self.cfg.regions = [r.strip() for r in regions.get_text().split(",") if r.strip()]
            self.cfg.skip_profiles = sorted(p for p, s in switches.items() if not s.get_active())
            self.cfg.save()
            self.scan()

        d.connect("closed", closed)
        d.present(self)

    def _cheatsheet(self) -> None:
        """Chuleta de psql con buscador; pulsar una fila la copia para pegarla en la terminal del cliente."""
        search = Gtk.SearchEntry(placeholder_text="Buscar (tamaño, bloqueos, permisos, \\d…)", hexpand=True)
        page = Adw.PreferencesPage()
        groups: list[tuple[Adw.PreferencesGroup, list[tuple[Adw.ActionRow, str]]]] = []
        for title, entries in cheatsheet.PSQL:
            g = Adw.PreferencesGroup(title=title)
            rows = []
            for cmd, desc in entries:
                row = Adw.ActionRow(title=f"<tt>{GLib.markup_escape_text(cmd)}</tt>", use_markup=True,
                                    subtitle=GLib.markup_escape_text(desc), activatable=True, title_lines=3)
                row.add_suffix(Gtk.Image(icon_name="edit-copy-symbolic", css_classes=["dim-label"]))
                row.connect("activated", lambda *_, c=cmd: self._copy(c))
                g.add(row)
                rows.append((row, f"{cmd} {desc}".lower()))
            page.add(g)
            groups.append((g, rows))

        def changed(*_):
            words = search.get_text().lower().split()
            for g, rows in groups:
                for row, text in rows:
                    row.set_visible(all(w in text for w in words))
                g.set_visible(any(r.get_visible() for r, _ in rows))

        search.connect("search-changed", changed)
        header = Adw.HeaderBar(title_widget=search)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        dialog = Adw.Dialog(title="Comandos de psql", child=view, content_width=760, content_height=720)
        search.set_key_capture_widget(dialog)
        dialog.present(self)

    def _about(self) -> None:
        Adw.AboutDialog(application_name="RDS por SSM", application_icon="drive-harddisk",
                        version=app_version(), developer_name="Raul Adamuz",
                        website="https://github.com/infranettone/infranettone-rds-connect",
                        license_type=Gtk.License.CUSTOM).present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()
