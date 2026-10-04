import datetime as dt
import socket
from urllib.parse import urlsplit, urlunsplit

import qrcode
import qrcode.image.svg
from nicegui import ui
from sqlalchemy import func, select

from app import sync
from app.models import sync_log
from app.web.layout import SUCCESS, TEXT_MUTED, WARNING, get_page_session, page_shell
from app.web.sync_api import SyncError, create_pairing_code, sync_with_peer

# Pinned rather than "latest" so a breaking upstream release can't silently
# break scanning.
_SCANNER_SCRIPT = "https://cdn.jsdelivr.net/npm/html5-qrcode@2.3.8/html5-qrcode.min.js"


def _lan_ip() -> str | None:
    """This machine's address on the local network — the route the OS would
    use to reach the outside world (no packet is actually sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return None


def _reachable_url(origin: str) -> str:
    """The address another device should use to reach this one: whatever
    this browser used, unless that's a loopback address only meaningful
    on this machine."""
    parts = urlsplit(origin)
    if parts.hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} and (ip := _lan_ip()):
        netloc = f"{ip}:{parts.port}" if parts.port else ip
        return urlunsplit(parts._replace(netloc=netloc))
    return origin


def _qr_svg(text: str) -> str:
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return image.to_string(encoding="unicode")


@ui.page("/sync")
async def sync_page():
    session = get_page_session()
    ui.add_head_html(f'<script src="{_SCANNER_SCRIPT}"></script>')
    ui.add_head_html("<style>.sync-qr svg { width: 260px; height: 260px; display: block; }</style>")

    with page_shell("/sync", "Sync Devices"):
        ui.label(
            "Merge this device's data with another instance of the app. Show the code on one device and "
            "scan it from the other — changes flow both ways, and where both sides edited the same field "
            "the most recent edit wins."
        ).style(f"color: {TEXT_MUTED};")

        with ui.row().classes("w-full gap-4 items-stretch"):
            with ui.column().classes("section-card gap-2").style("flex: 1; min-width: 300px;"):
                ui.label("Show pairing code").classes("text-base font-bold")
                ui.label("Scan this from the other device.").style(f"color: {TEXT_MUTED};")
                qr_box = (
                    ui.html("")
                    .classes("sync-qr self-center")
                    .style("background: white; padding: 8px; border-radius: 8px;")
                )
                expiry_label = ui.label().style(f"color: {TEXT_MUTED}; font-size: 12px;")
                url_input = ui.input("This device's address, as seen from the other device").classes("w-full")
                ui.button("New code", icon="refresh", on_click=lambda: show_code()).props("flat")

            with ui.column().classes("section-card gap-2").style("flex: 1; min-width: 300px;"):
                ui.label("Scan pairing code").classes("text-base font-bold")
                ui.label("Scan the code shown on the other device.").style(f"color: {TEXT_MUTED};")
                ui.button("Scan QR code", icon="qr_code_scanner", on_click=lambda: open_scanner())
                ui.label(
                    "Browsers only allow the live camera over https or localhost — otherwise use "
                    "“Scan an Image File” in the scanner (on a phone, that opens the camera too)."
                ).style(f"color: {TEXT_MUTED}; font-size: 12px;")
                ui.separator()
                code_input = ui.input("…or paste the code").classes("w-full")
                sync_button = ui.button("Sync", on_click=lambda: run_sync(code_input.value or ""))
                result_box = ui.column().classes("w-full gap-1")

        footer = ui.label().style(f"color: {TEXT_MUTED}; font-size: 12px;")

    def refresh_footer() -> None:
        conn = session.connection()
        device = sync.device_id(conn)
        count = conn.execute(select(func.count()).select_from(sync_log)).scalar()
        session.commit()
        footer.text = f"This device: {device[:8]} · {count} changes in write log"

    def show_code() -> None:
        code, expiry = create_pairing_code(url_input.value or "")
        qr_box.content = _qr_svg(code)
        expiry_label.text = f"Valid until {dt.datetime.fromtimestamp(expiry, dt.UTC).astimezone():%H:%M}"

    async def run_sync(code: str) -> None:
        if not code.strip():
            ui.notify("Paste or scan a pairing code first.", type="warning")
            return
        sync_button.props("loading")
        result_box.clear()
        try:
            received, sent = await sync_with_peer(code.strip())
        except SyncError as exc:
            with result_box:
                ui.label(str(exc)).style(f"color: {WARNING};")
            return
        finally:
            sync_button.props(remove="loading")
        with result_box:
            ui.label(
                f"✓ Synced — received {received.received} changes ({received.applied} applied), "
                f"sent {sent.received} ({sent.applied} applied)."
            ).style(f"color: {SUCCESS};")
            for note in received.notes + sent.notes:
                ui.label(note).style(f"color: {TEXT_MUTED}; font-size: 12px;")
        code_input.value = ""
        session.expire_all()
        refresh_footer()
        ui.notify("Sync complete — reload any other open pages to see the merged data.", type="positive")

    scanner_dialogs: list[ui.dialog] = []

    def open_scanner() -> None:
        with ui.dialog() as dialog, ui.card().classes("gap-2"):
            ui.label("Scan pairing code").classes("text-lg font-bold")
            ui.element("div").props("id=sync-qr-reader").style(
                "width: min(420px, 80vw); background: white; color: black; border-radius: 8px;"
            )
            ui.button("Cancel", on_click=dialog.close).props("flat")

        def on_hide() -> None:
            # Releases the camera — closing the dialog alone leaves it running.
            ui.run_javascript("window.syncScanner?.clear().catch(() => {})")
            scanner_dialogs.remove(dialog)
            dialog.delete()

        dialog.on("hide", on_hide)
        scanner_dialogs.append(dialog)
        dialog.open()
        # Wait for the dialog's contents to reach the DOM before mounting the
        # scanner into them.
        ui.run_javascript(
            """
            const start = () => {
                if (!document.getElementById('sync-qr-reader') || !window.Html5QrcodeScanner) {
                    return setTimeout(start, 50);
                }
                window.syncScanner = new Html5QrcodeScanner(
                    'sync-qr-reader', {fps: 10, qrbox: 240, rememberLastUsedCamera: true}, false);
                window.syncScanner.render(text => {
                    window.syncScanner.clear().catch(() => {});
                    emitEvent('sync_code_scanned', text);
                }, () => {});
            };
            start();
            """
        )

    async def on_scanned(e) -> None:
        for dialog in scanner_dialogs:
            dialog.close()
        await run_sync(e.args if isinstance(e.args, str) else e.args[0])

    ui.on("sync_code_scanned", on_scanned)

    await ui.context.client.connected()
    url_input.value = _reachable_url(await ui.run_javascript("window.location.origin"))
    url_input.on("blur", show_code)
    show_code()
    refresh_footer()
