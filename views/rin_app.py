import asyncio
import csv
import io
import math
import os
import time
import urllib.parse
from datetime import datetime, timezone

import flet as ft
import flet_charts as ftc
import flet_geolocator as ftg

from models import (
    DEFAULT_PARAMS,
    LocationPoint,
    SQLiteStore,
    Settings,
    TripTracker,
    format_duration,
    timestamp_ms,
)

# Classic, dignified academic engineering dark theme (non-neon)
COLOR_BG = "#0f141c"               # Deep slate graphite
COLOR_CARD = "#18202c"             # Solid dark slate card surface
COLOR_HERO = "#1e293b"             # Slate navy hero container
COLOR_PRIMARY = "#3b82d6"          # Classic university/slate blue (replaces neon cyan)
COLOR_PRIMARY_CONTAINER = "rgba(59, 130, 214, 0.15)"
COLOR_CYAN = COLOR_PRIMARY         # Kept as alias for compatibility
COLOR_DANGER = "#e05252"           # Calmer soft red
COLOR_SUCCESS = "#10b981"          # Emerald green
COLOR_TEXT_PRIMARY = "#f1f5f9"      # Clean high-contrast off-white
COLOR_TEXT_MUTED = "rgba(241, 245, 249, 0.6)" # Soft muted slate

SAQ_COLORS = {
    "A": "#69F0AE",
    "B": "#81C784",
    "C": "#FFF176",
    "D": "#FFB74D",
    "E": "#FF8A65",
    "F": "#e05252",
    "-": "#718096",
}


class RinApp:
    """Flet UI for RIN-Live tracking and SAQ evaluation."""

    def __init__(self, page: ft.Page):
        self._page = page
        self._page.bgcolor = COLOR_BG
        self._page.padding = 0
        self._page.spacing = 0
        self.store = SQLiteStore()
        self.settings = self.store.load_settings()
        self.tracker = TripTracker(self.settings)
        self.recording_state = "IDLE"  # "IDLE", "RECORDING", "PAUSED"
        self.active_tab = "dashboard"
        self.csv_visible = False
        self.last_position = None
        self.logs = ["RIN-Live bereit.", "SQLite-Speicher initialisiert."]

        # GPS background service
        self.geolocator = ftg.Geolocator(
            configuration=ftg.GeolocatorAndroidConfiguration(
                accuracy=ftg.GeolocatorPositionAccuracy.BEST_FOR_NAVIGATION,
                distance_filter=1,
                interval_duration=int(self.settings.gps_interval * 1000),
                foreground_notification_config=ftg.ForegroundNotificationConfiguration(
                    notification_title="RIN-Live",
                    notification_text="RIN-Live zeichnet deine Route auf.",
                ),
            ),
            on_position_change=self._on_position_change,
            on_error=self._on_location_error,
        )
        self.file_picker = ft.FilePicker()
        self.share_service = ft.Share()
        self.clipboard = ft.Clipboard()
        self._page.services.extend([self.geolocator, self.file_picker, self.share_service, self.clipboard])

        # Persistent view columns to preserve scroll positions across live updates
        self.dashboard_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=12,
            controls=[],
        )
        self.data_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=10,
            controls=[],
        )
        self.debug_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=10,
            controls=[],
        )
        self.settings_column = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=12,
            controls=[],
        )
        self.content_container = ft.Container(
            padding=ft.Padding.symmetric(horizontal=14, vertical=8),
            expand=True,
            content=self.dashboard_column,
        )

        # Root layout with SafeArea for Android status bar & bottom navigation bar / gesture insets
        self.root = ft.Column(
            expand=True,
            spacing=0,
            controls=[],
        )
        self.safe_area = ft.SafeArea(
            content=self.root,
            expand=True,
            avoid_intrusions_top=True,
            avoid_intrusions_bottom=True,
        )
        self._page.add(self.safe_area)
        self.render()

        # Start timer clock
        if hasattr(self._page, "run_task"):
            self._page.run_task(self._clock)
        else:
            try:
                asyncio.create_task(self._clock())
            except RuntimeError:
                pass

    @property
    def recording(self) -> bool:
        return self.recording_state == "RECORDING"

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.insert(0, f"[{stamp}] {message}")
        self.logs = self.logs[:80]

    async def _clock(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.recording_state == "RECORDING" and self.active_tab == "dashboard":
                self.render()

    def _on_location_error(self, event) -> None:
        err_msg = getattr(event, "data", str(event))
        self._log(f"GPS-Fehler: {err_msg}")
        if self.active_tab == "debug":
            self.render()

    def _on_position_change(self, event: ftg.GeolocatorPositionChangeEvent) -> None:
        if event.position is not None:
            self._process_position(event.position)

    def _process_position(self, position: ftg.GeolocatorPosition) -> None:
        self.last_position = position
        if self.recording_state != "RECORDING":
            if self.active_tab == "debug":
                self.render()
            return

        point, note = self.tracker.ingest(
            latitude=position.latitude,
            longitude=position.longitude,
            accuracy=position.accuracy or 0.0,
            timestamp=timestamp_ms(position.timestamp),
        )
        if note:
            self._log(note)
        if point:
            self.store.add_point(point)
            self._log(
                f"GPS Tick: {point.total_distance_km:.2f} km, V={point.instant_speed_kmh:.1f} km/h, SAQ {point.saq}"
            )
        if self.active_tab in {"dashboard", "data", "debug"}:
            self.render()

    async def open_app_settings(self, _event=None) -> None:
        try:
            await self.geolocator.open_app_settings()
            self._log("Android App-Einstellungen geöffnet.")
        except Exception as err:
            self._log(f"Konnte App-Einstellungen nicht öffnen: {err}")

    async def open_location_settings(self, _event=None) -> None:
        try:
            await self.geolocator.open_location_settings()
            self._log("Android Standorteinstellungen geöffnet.")
        except Exception as err:
            self._log(f"Konnte Standorteinstellungen nicht öffnen: {err}")

    def request_start_recording(self, _event=None) -> None:
        def on_cancel(_e):
            self._page.pop_dialog()
            self._log("Aufzeichnung abgebrochen (keine Standort-Zustimmung).")

        async def on_consent(_e):
            self._page.pop_dialog()
            await self._execute_start_recording()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.LOCATION_ON_ROUNDED, color=COLOR_PRIMARY, size=24),
                ft.Text("Hintergrund-GPS & Akku", size=18, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            "RIN-Live benötigt kontinuierlichen GPS-Zugriff im Hintergrund, um Verkehrsqualitätsstufen auch bei gesperrtem Smartphone lückenlos zu messen.",
                            size=12,
                            color=COLOR_TEXT_PRIMARY,
                        ),
                        ft.Container(height=4),
                        ft.Container(
                            content=ft.Column(
                                controls=[
                                    ft.Row(
                                        controls=[
                                            ft.Icon(ft.Icons.BATTERY_ALERT_ROUNDED, color="#f59e0b", size=18),
                                            ft.Text("1. Akku-Optimierung deaktivieren:", size=12, weight=ft.FontWeight.BOLD, color="#f59e0b"),
                                        ],
                                        spacing=6,
                                    ),
                                    ft.Text(
                                        "Android beendet Apps im Hintergrund bei Akkusparmodus. Setze unter 'Akku' die Nutzung auf 'Nicht eingeschränkt' (Unrestricted).",
                                        size=11,
                                        color="rgba(255, 255, 255, 0.9)",
                                    ),
                                    ft.Container(height=2),
                                    ft.Button(
                                        content=ft.Row([
                                            ft.Icon(ft.Icons.SETTINGS_ROUNDED, size=15),
                                            ft.Text("Android-Einstellungen öffnen", size=11, weight=ft.FontWeight.BOLD),
                                        ], spacing=6, tight=True),
                                        bgcolor="rgba(245, 158, 11, 0.2)",
                                        color="#f59e0b",
                                        on_click=lambda _: self._page.run_task(self.open_app_settings),
                                    ),
                                ],
                                spacing=4,
                            ),
                            bgcolor="rgba(245, 158, 11, 0.12)",
                            border=ft.Border.all(1, "rgba(245, 158, 11, 0.3)"),
                            border_radius=8,
                            padding=10,
                        ),
                        ft.Container(height=4),
                        ft.Container(
                            content=ft.Column(
                                controls=[
                                    ft.Row(
                                        controls=[
                                            ft.Icon(ft.Icons.SECURITY_ROUNDED, color=COLOR_PRIMARY, size=18),
                                            ft.Text("2. Standort: 'Immer zulassen':", size=12, weight=ft.FontWeight.BOLD, color=COLOR_PRIMARY),
                                        ],
                                        spacing=6,
                                    ),
                                    ft.Text(
                                        "Wähle bei der nachfolgenden Android-Berechtigungsabfrage unbedingt 'Immer zulassen' (Allow all the time).",
                                        size=11,
                                        color="rgba(255, 255, 255, 0.9)",
                                    ),
                                ],
                                spacing=4,
                            ),
                            bgcolor="rgba(59, 130, 214, 0.12)",
                            border=ft.Border.all(1, "rgba(59, 130, 214, 0.3)"),
                            border_radius=8,
                            padding=10,
                        ),
                        ft.Container(height=4),
                        ft.Text(
                            "Deine Daten verbleiben lokal auf deinem Gerät und werden nur auf Wunsch gespendet.",
                            size=10,
                            color=COLOR_TEXT_MUTED,
                        ),
                    ],
                    spacing=4,
                    tight=True,
                ),
                width=360,
            ),
            actions=[
                ft.TextButton("Abbrechen", on_click=on_cancel),
                ft.FilledButton(
                    content=ft.Text("Berechtigung erteilen & Starten"),
                    bgcolor=COLOR_PRIMARY,
                    color="#ffffff",
                    on_click=lambda e: self._page.run_task(on_consent, e),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

    start_recording = request_start_recording

    async def _execute_start_recording(self) -> None:
        try:
            permission = await self.geolocator.request_permission()
            if permission not in {
                ftg.GeolocatorPermissionStatus.WHILE_IN_USE,
                ftg.GeolocatorPermissionStatus.ALWAYS,
            }:
                self._log("Standortberechtigung wurde nicht erteilt.")
                self.render()
                return
            if permission == ftg.GeolocatorPermissionStatus.WHILE_IN_USE:
                self._log("Hinweis: Berechtigung nur 'Während der Nutzung'. Für Hintergrundaufzeichnung bitte in den App-Einstellungen auf 'Immer zulassen' stellen.")
        except Exception as perm_err:
            self._log(f"Berechtigungs-Check: {perm_err}")

        start_time = int(time.time() * 1000)
        session_id = self.tracker.start(start_time)
        self.store.start_session(session_id, self.settings.mode)
        self.recording_state = "RECORDING"
        self._log(f"Aufzeichnung gestartet: {session_id} (2s Intervall, 5m Genauigkeit).")
        try:
            position = await self.geolocator.get_current_position()
            if position is not None:
                self._process_position(position)
        except Exception as error:
            self._log(f"Erste GPS-Abfrage: {error}")
        self.render()

    def pause_recording(self, _event=None) -> None:
        if self.recording_state == "RECORDING":
            self.recording_state = "PAUSED"
            self.tracker.pause(int(time.time() * 1000))
            self._log(f"Aufzeichnung pausiert: {self.tracker.session_id}")
            self.render()

    def resume_recording(self, _event=None) -> None:
        if self.recording_state == "PAUSED":
            self.recording_state = "RECORDING"
            self.tracker.resume(int(time.time() * 1000))
            self._log(f"Aufzeichnung fortgesetzt: {self.tracker.session_id}")
            self.render()

    def request_stop_recording(self, _event=None) -> None:
        def on_cancel(_e):
            self._page.pop_dialog()

        def on_confirm(_e):
            self._page.pop_dialog()
            self.stop_recording()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.HELP_OUTLINE_ROUNDED, color="#FFA726", size=24),
                ft.Text("Aufzeichnung beenden?", size=18, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            content=ft.Text("Möchtest du die aktuelle Fahrtaufzeichnung wirklich beenden und speichern?"),
            actions=[
                ft.TextButton("Abbrechen", on_click=on_cancel),
                ft.FilledButton(
                    content=ft.Text("Beenden & Speichern"),
                    bgcolor=COLOR_DANGER,
                    color="#ffffff",
                    on_click=on_confirm,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

    def stop_recording(self) -> None:
        session_id = self.tracker.session_id
        self.recording_state = "IDLE"
        self._log(f"Aufzeichnung beendet und gespeichert: {session_id}")
        self.render()

    def toggle_recording(self, _event=None) -> None:
        if self.recording_state in {"RECORDING", "PAUSED"}:
            self.request_stop_recording()
        else:
            self.request_start_recording()

    def open_session(self, session_id: str) -> None:
        if self.recording_state != "IDLE":
            self._log("Kann während einer aktiven Aufzeichnung keine andere Fahrt öffnen.")
            return
        points = self.store.points_for(session_id)
        if not points:
            self._log(f"Keine Datenpunkte für {session_id} gefunden.")
            return
        self.tracker.load_session(session_id, points)
        self._log(f"Fahrt {session_id} geladen ({len(points)} Datenpunkte).")
        self.active_tab = "dashboard"
        self.render()

    def delete_session(self, session_id: str) -> None:
        def on_cancel(_e):
            self._page.pop_dialog()

        def on_confirm(_e):
            self._page.pop_dialog()
            self.store.clear_session(session_id)
            if self.tracker.session_id == session_id:
                self.tracker.reset()
            self._log(f"Fahrt {session_id} gelöscht.")
            self.render()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.DELETE_FOREVER_ROUNDED, color=COLOR_DANGER, size=24),
                ft.Text("Fahrt löschen?", size=18, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            content=ft.Text(f"Möchtest du die Aufzeichnung '{session_id}' unwiderruflich löschen?"),
            actions=[
                ft.TextButton("Abbrechen", on_click=on_cancel),
                ft.FilledButton(
                    content=ft.Text("Löschen"),
                    bgcolor=COLOR_DANGER,
                    color="#ffffff",
                    on_click=on_confirm,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

    async def fetch_location(self, _event) -> None:
        try:
            position = await self.geolocator.get_current_position()
            if position is None:
                self._log("GPS lieferte keine Position.")
            else:
                self._log("Manuelle GPS-Abfrage erfolgreich.")
                self._process_position(position)
        except Exception as error:
            self._log(f"GPS-Abfrage fehlgeschlagen: {error}")
        self.render()

    def switch_tab(self, tab: str):
        def handler(_event) -> None:
            self.active_tab = tab
            self.render()

        return handler

    def reset_session(self, _event) -> None:
        if self.tracker.session_id:
            self.store.clear_session(self.tracker.session_id)
        self.tracker.reset()
        self.recording_state = "IDLE"
        self.csv_visible = False
        self._log("Aufzeichnung und Messdaten zurückgesetzt.")
        self.render()

    def toggle_csv(self, _event) -> None:
        self.csv_visible = not self.csv_visible
        self.render()

    def prompt_export(self, session_id: str | None = None, is_share: bool = False) -> None:
        target_id = session_id or self.tracker.session_id
        if not target_id:
            self.prompt_export_all(is_share=is_share)
            return

        def choose_format(fmt: str):
            self._page.pop_dialog()
            self._page.run_task(self.execute_export, target_id, fmt, is_share)

        action_title = "Fahrt teilen" if is_share else "Fahrt exportieren"
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.SHARE_ROUNDED if is_share else ft.Icons.DOWNLOAD_ROUNDED, color=COLOR_PRIMARY, size=22),
                ft.Text(action_title, size=17, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(f"Fahrt: {target_id}", size=11, color=COLOR_TEXT_MUTED),
                        ft.Container(height=4),
                        ft.Text("Wähle das gewünschte Format:", size=12, weight=ft.FontWeight.W_600),
                        ft.Container(height=6),
                        ft.Container(
                            content=ft.Row([
                                ft.Icon(ft.Icons.MAP_ROUNDED, color=COLOR_PRIMARY, size=24),
                                ft.Column([
                                    ft.Text("GPX-Datei (.gpx)", size=13, weight=ft.FontWeight.BOLD, color=COLOR_TEXT_PRIMARY),
                                    ft.Text("Für Komoot, Bergfex, Strava, Garmin & GPS-Geräte", size=10, color=COLOR_TEXT_MUTED),
                                ], spacing=1, expand=True),
                            ], spacing=10),
                            bgcolor="rgba(255, 255, 255, 0.05)",
                            border=ft.Border.all(1, "rgba(255, 255, 255, 0.08)"),
                            border_radius=8,
                            padding=10,
                            ink=True,
                            on_click=lambda _: choose_format("gpx"),
                        ),
                        ft.Container(height=4),
                        ft.Container(
                            content=ft.Row([
                                ft.Icon(ft.Icons.TABLE_CHART_ROUNDED, color="#10b981", size=24),
                                ft.Column([
                                    ft.Text("CSV-Tabelle (.csv)", size=13, weight=ft.FontWeight.BOLD, color=COLOR_TEXT_PRIMARY),
                                    ft.Text("Für Excel, Tabellenprogramme & RIN-08 Analyse", size=10, color=COLOR_TEXT_MUTED),
                                ], spacing=1, expand=True),
                            ], spacing=10),
                            bgcolor="rgba(255, 255, 255, 0.05)",
                            border=ft.Border.all(1, "rgba(255, 255, 255, 0.08)"),
                            border_radius=8,
                            padding=10,
                            ink=True,
                            on_click=lambda _: choose_format("csv"),
                        ),
                    ],
                    tight=True,
                    spacing=2,
                ),
                width=340,
            ),
            actions=[
                ft.TextButton("Abbrechen", on_click=lambda _: self._page.pop_dialog()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

    def prompt_export_all(self, is_share: bool = False) -> None:
        def choose_format(fmt: str):
            self._page.pop_dialog()
            self._page.run_task(self.execute_export, None, fmt, is_share)

        action_title = "Alle Fahrten teilen" if is_share else "Alle Fahrten exportieren"
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.ALL_INBOX_ROUNDED, color=COLOR_PRIMARY, size=22),
                ft.Text(action_title, size=17, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text("Gesamter Verlauf aus der lokalen SQLite-Datenbank", size=11, color=COLOR_TEXT_MUTED),
                        ft.Container(height=4),
                        ft.Text("Wähle das Format für den Gesamtexport:", size=12, weight=ft.FontWeight.W_600),
                        ft.Container(height=6),
                        ft.Container(
                            content=ft.Row([
                                ft.Icon(ft.Icons.MAP_ROUNDED, color=COLOR_PRIMARY, size=24),
                                ft.Column([
                                    ft.Text("GPX-Datei (.gpx)", size=13, weight=ft.FontWeight.BOLD, color=COLOR_TEXT_PRIMARY),
                                    ft.Text("Alle Fahrten als separate Tracks für GIS & GPS", size=10, color=COLOR_TEXT_MUTED),
                                ], spacing=1, expand=True),
                            ], spacing=10),
                            bgcolor="rgba(255, 255, 255, 0.05)",
                            border=ft.Border.all(1, "rgba(255, 255, 255, 0.08)"),
                            border_radius=8,
                            padding=10,
                            ink=True,
                            on_click=lambda _: choose_format("gpx"),
                        ),
                        ft.Container(height=4),
                        ft.Container(
                            content=ft.Row([
                                ft.Icon(ft.Icons.TABLE_CHART_ROUNDED, color="#10b981", size=24),
                                ft.Column([
                                    ft.Text("CSV-Tabelle (.csv)", size=13, weight=ft.FontWeight.BOLD, color=COLOR_TEXT_PRIMARY),
                                    ft.Text("Komplette Tabelle aller Punkte für Tabellenkalkulation", size=10, color=COLOR_TEXT_MUTED),
                                ], spacing=1, expand=True),
                            ], spacing=10),
                            bgcolor="rgba(255, 255, 255, 0.05)",
                            border=ft.Border.all(1, "rgba(255, 255, 255, 0.08)"),
                            border_radius=8,
                            padding=10,
                            ink=True,
                            on_click=lambda _: choose_format("csv"),
                        ),
                    ],
                    tight=True,
                    spacing=2,
                ),
                width=340,
            ),
            actions=[
                ft.TextButton("Abbrechen", on_click=lambda _: self._page.pop_dialog()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

    async def execute_export(self, session_id: str | None, fmt: str, is_share: bool = False) -> None:
        if session_id:
            points = self.store.points_for(session_id)
            if not points:
                self._log(f"Keine Datenpunkte für Fahrt {session_id} vorhanden.")
                return
            target_name = f"rin_{session_id}"
            if fmt == "gpx":
                file_text = self._gpx_for(points, session_id)
                ext = "gpx"
                mime = "application/gpx+xml"
            else:
                file_text = self._csv_for(points)
                ext = "csv"
                mime = "text/csv"
        else:
            all_pts = self.store.all_points()
            if not all_pts:
                self._log("Keine Datenpunkte in Datenbank vorhanden.")
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target_name = f"rin_all_tracks_{stamp}"
            if fmt == "gpx":
                file_text = self._gpx_for_all(all_pts)
                ext = "gpx"
                mime = "application/gpx+xml"
            else:
                file_text = self._csv_for_all(all_pts)
                ext = "csv"
                mime = "text/csv"

        filename = f"{target_name}.{ext}"
        data_bytes = file_text.encode("utf-8")

        if is_share:
            try:
                share_file = ft.ShareFile.from_bytes(data_bytes, mime_type=mime, name=filename)
                await self.share_service.share_files(
                    [share_file],
                    title=f"RIN-Live {ext.upper()} teilen",
                    text=f"RIN-Live Datenexport ({filename})",
                )
                self._log(f"Teilen-Menü geöffnet für {filename}")
            except Exception as err:
                self._log(f"Teilen fehlgeschlagen: {err}")
            return

        # 1. Local copy in exports/ directory
        try:
            os.makedirs("exports", exist_ok=True)
            local_path = os.path.join("exports", filename)
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(file_text)
            self._log(f"Lokale Kopie: {filename}")
        except Exception as err:
            self._log(f"Lokales Speichern: {err}")

        # 2. Native File Picker (Opens Android Storage Access Framework / Desktop Save dialog)
        picker_saved = False
        try:
            saved_path = await self.file_picker.save_file(
                dialog_title=f"{ext.upper()}-Datei speichern",
                file_name=filename,
                src_bytes=data_bytes,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=[ext],
            )
            if saved_path:
                self._log(f"Datei erfolgreich exportiert: {saved_path}")
                picker_saved = True
            else:
                self._log("Dateiauswahl abgebrochen.")
                return
        except Exception as err:
            self._log(f"Dateiauswahl: {err}")

        # 3. Fallback: Browser download (Data URI) if on web
        if not picker_saved and getattr(self._page, "web", False):
            try:
                encoded = urllib.parse.quote(file_text)
                data_uri = f"data:{mime};charset=utf-8,{encoded}"
                self._page.launch_url(data_uri)
                self._log(f"Browser-Download angestoßen ({filename})")
            except Exception as err:
                self._log(f"Download-Fehler: {err}")

    async def download_csv(self, _event=None, session_id: str | None = None) -> None:
        self.prompt_export(session_id=session_id, is_share=False)

    async def download_csv_all(self, _event=None) -> None:
        self.prompt_export_all(is_share=False)

    async def share_csv(self, _event=None, session_id: str | None = None) -> None:
        self.prompt_export(session_id=session_id, is_share=True)

    async def copy_csv(self, _event=None) -> None:
        points = self.store.points_for(self.tracker.session_id)
        if not points:
            self._log("Keine Datenpunkte zum Kopieren vorhanden.")
            return
        csv_text = self._csv_for(points)
        try:
            await self.clipboard.set(csv_text)
            self._log("CSV in Zwischenablage kopiert.")
        except Exception as err:
            self._log(f"Kopieren fehlgeschlagen: {err}")

    def save_settings(self, _event) -> None:
        self.store.save_settings(self.settings)
        self._log("Konfiguration gespeichert.")
        self.render()

    def reset_parameters(self, _event) -> None:
        self.settings.params = {
            mode: {key: list(values) for key, values in curve.items()}
            for mode, curve in DEFAULT_PARAMS.items()
        }
        self.store.save_settings(self.settings)
        self._log("SAQ-Parameter auf RIN 2008 Standardwerte zurückgesetzt.")
        self.render()

    def set_number(self, field_name: str):
        def handler(event) -> None:
            try:
                val = float(event.control.value.replace(",", "."))
                setattr(self.settings, field_name, val)
            except (TypeError, ValueError):
                return

        return handler

    def set_string(self, field_name: str):
        def handler(event) -> None:
            val = str(event.control.value).strip() if event.control.value else ""
            setattr(self.settings, field_name, val)

        return handler

    def reset_api_endpoint(self, _event=None) -> None:
        self.settings.api_endpoint = "https://rin.isv.uni-stuttgart.de/api/v1/"
        self.store.save_settings(self.settings)
        self._log("API-Endpunkt auf Standard-URL zurückgesetzt.")
        self.render()

    def set_mode(self, event) -> None:
        self.settings.mode = event.control.value
        self.render()

    def set_parameter(self, key: str, index: int):
        def handler(event) -> None:
            try:
                val = float(event.control.value.replace(",", "."))
                self.settings.params[self.settings.mode][key][index] = val
            except (TypeError, ValueError):
                return

        return handler

    def confirm_server_upload(self, session_id: str | None = None) -> None:
        def on_cancel(_e):
            self._page.pop_dialog()

        async def on_confirm(_e):
            self._page.pop_dialog()
            if session_id:
                target_ids = [session_id]
            else:
                sessions = self.store.list_sessions()
                target_ids = [s["id"] for s in sessions]
            if not target_ids:
                self._log("Keine Fahrten für Server-Übertragung vorhanden.")
                return
            await self._execute_server_upload(target_ids)

        if session_id:
            msg_target = f"die ausgewählte Fahrt '{session_id}'"
        else:
            sessions = self.store.list_sessions()
            msg_target = f"alle {len(sessions)} gespeicherten Fahrten"

        endpoint_url = getattr(self.settings, "api_endpoint", "https://rin.isv.uni-stuttgart.de/api/v1/")

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.CLOUD_UPLOAD_ROUNDED, color="#10B981", size=24),
                ft.Text("Daten an Server senden", size=18, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(f"Möchtest du {msg_target} an den Server übertragen?"),
                        ft.Container(height=4),
                        ft.Container(
                            content=ft.Column([
                                ft.Row([
                                    ft.Icon(ft.Icons.SHIELD_ROUNDED, color="#69F0AE", size=16),
                                    ft.Text("Anonym & verschlüsselt", size=12, weight=ft.FontWeight.BOLD, color="#69F0AE"),
                                ], spacing=6),
                                ft.Text(
                                    "Ihre Daten werden anonym und verschlüsselt via HTTPS übertragen.\n"
                                    "Hinweis: Es ist in Ordnung, dieselbe Fahrt mehrfach zu übermitteln. Duplikate werden serverseitig gefiltert.",
                                    size=11,
                                    color="rgba(255, 255, 255, 0.9)",
                                ),
                            ], spacing=4),
                            bgcolor="rgba(16, 185, 129, 0.12)",
                            border=ft.Border.all(1, "rgba(16, 185, 129, 0.3)"),
                            border_radius=8,
                            padding=10,
                        ),
                        ft.Container(height=4),
                        ft.Text(f"Ziel-Endpunkt:\n{endpoint_url}", size=11, color=COLOR_TEXT_MUTED),
                    ],
                    spacing=6,
                    tight=True,
                ),
                width=360,
            ),
            actions=[
                ft.TextButton("Abbrechen", on_click=on_cancel),
                ft.FilledButton(
                    content=ft.Text("Jetzt senden"),
                    bgcolor="#10B981",
                    color="#0d0d1a",
                    on_click=lambda e: self._page.run_task(on_confirm, e),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

    async def _execute_server_upload(self, session_ids: list[str]) -> None:
        total = len(session_ids)
        prog_bar = ft.ProgressBar(value=0.0, color="#10B981", bgcolor="rgba(255,255,255,0.1)")
        status_text = ft.Text(f"Vorbereitung: 0 von {total} gesendet...", size=12, color=COLOR_TEXT_PRIMARY)
        detail_list = ft.Column(scroll=ft.ScrollMode.AUTO, height=130, spacing=4)
        close_btn = ft.TextButton("Schließen", visible=False, on_click=lambda _: self._page.pop_dialog())

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Icon(ft.Icons.CLOUD_SYNC_ROUNDED, color="#10B981", size=22),
                ft.Text("Übertragung an Server", size=16, weight=ft.FontWeight.BOLD),
            ], spacing=8),
            content=ft.Container(
                content=ft.Column([
                    status_text,
                    prog_bar,
                    ft.Container(height=4),
                    detail_list,
                ], spacing=8, tight=True),
                width=360,
            ),
            actions=[close_btn],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page.show_dialog(dialog)

        success_count = 0
        fail_count = 0
        endpoint = getattr(self.settings, "api_endpoint", "https://rin.isv.uni-stuttgart.de/api/v1/")

        for idx, sid in enumerate(session_ids, start=1):
            status_text.value = f"Sende Fahrt {idx} von {total}: {sid}"
            prog_bar.value = (idx - 1) / total
            self._page.update()

            points = self.store.points_for(sid)
            if not points:
                detail_list.controls.insert(0, ft.Text(f"• {sid}: Keine Messdaten vorhanden", size=11, color=COLOR_TEXT_MUTED))
                continue

            csv_text = self._csv_for(points)
            ok, msg = await asyncio.to_thread(self._post_track_csv, endpoint, sid, csv_text)
            if ok:
                success_count += 1
                detail_list.controls.insert(0, ft.Row([
                    ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color="#69F0AE", size=14),
                    ft.Text(f"{sid}: Gesendet ({len(points)} Pkt)", size=11, color="#69F0AE"),
                ], spacing=4))
                self._log(f"Server-Upload {sid}: Erfolgreich ({len(points)} Punkte).")
            else:
                fail_count += 1
                detail_list.controls.insert(0, ft.Row([
                    ft.Icon(ft.Icons.ERROR_OUTLINE_ROUNDED, color=COLOR_DANGER, size=14),
                    ft.Text(f"{sid}: {msg}", size=11, color=COLOR_DANGER),
                ], spacing=4))
                self._log(f"Server-Upload {sid} fehlgeschlagen: {msg}")

            prog_bar.value = idx / total
            self._page.update()
            await asyncio.sleep(0.05)

        status_text.value = f"Übertragung beendet: {success_count} erfolgreich, {fail_count} fehlgeschlagen."
        prog_bar.value = 1.0
        close_btn.visible = True
        self._page.update()

    @staticmethod
    def _post_track_csv(endpoint: str, session_id: str, csv_content: str) -> tuple[bool, str]:
        import urllib.request
        import urllib.error
        import ssl

        target_url = endpoint.strip()
        if not target_url.endswith("/"):
            target_url += "/"

        boundary = f"----RINBoundary{os.urandom(12).hex()}"
        body = bytearray()

        # Session ID field
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="session_id"\r\n\r\n{session_id}\r\n'.encode("utf-8"))

        # CSV file payload
        filename = f"rin_{session_id}.csv"
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(b"Content-Type: text/csv; charset=utf-8\r\n\r\n")
        body.extend(csv_content.encode("utf-8"))
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            target_url,
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "RIN-Live/1.0",
                "X-Session-ID": session_id,
            },
            method="POST",
        )
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                code = resp.getcode()
                if 200 <= code < 300:
                    return True, f"HTTP {code}"
                return False, f"Server Status {code}"
        except urllib.error.HTTPError as he:
            return False, f"HTTP {he.code}: {he.reason}"
        except Exception as ex:
            return False, f"{type(ex).__name__}: {str(ex)[:60]}"

    # UI Components
    def _metric_card(self, title: str, value: str, unit: str = "", highlight: bool = False) -> ft.Container:
        spans = [
            ft.TextSpan(
                text=value,
                style=ft.TextStyle(
                    size=22,
                    weight=ft.FontWeight.BOLD,
                    color=COLOR_PRIMARY if highlight else COLOR_TEXT_PRIMARY,
                ),
            ),
        ]
        if unit:
            spans.append(
                ft.TextSpan(
                    text=f" {unit}",
                    style=ft.TextStyle(
                        size=11,
                        weight=ft.FontWeight.NORMAL,
                        color=COLOR_TEXT_MUTED,
                    ),
                )
            )

        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(title, size=10, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.7)"),
                    ft.Text(spans=spans),
                ],
                spacing=4,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            expand=True,
            border=ft.Border.all(1, "rgba(255,255,255,0.06)"),
        )

    def _tab_button(self, label: str, tab: str) -> ft.Control:
        is_active = self.active_tab == tab
        return ft.Button(
            content=ft.Text(
                label,
                size=13.5,
                weight=ft.FontWeight.BOLD if is_active else ft.FontWeight.W_600,
                color="#ffffff" if is_active else "rgba(255,255,255,0.75)",
            ),
            bgcolor=COLOR_PRIMARY if is_active else "rgba(255,255,255,0.06)",
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=6),
                padding=ft.Padding.symmetric(vertical=10, horizontal=6),
                elevation=0,
            ),
            on_click=self.switch_tab(tab),
            expand=True,
        )

    def dashboard_view(self) -> ft.Column:
        metrics = self.tracker.metrics
        saq = self.tracker.current_saq()
        has_data = (self.recording_state in {"RECORDING", "PAUSED"}) or (self.tracker.total_distance_km > 0)
        letter = saq.letter if has_data else "-"
        grade_desc = f"Stufe {saq.grade}" if has_data else "Stufe -"
        if self.recording_state == "PAUSED":
            grade_desc += " · PAUSIERT"
        elif self.recording_state == "RECORDING":
            grade_desc += " · LIVE"
        elif self.tracker.session_id:
            grade_desc += " · Gespeichert"
        saq_color = SAQ_COLORS.get(letter, "#718096")

        if self.recording_state in {"RECORDING", "PAUSED"}:
            elapsed = self.tracker.effective_elapsed_ms(int(time.time() * 1000))
        elif self.tracker.session_id:
            elapsed = metrics.moving_time_ms or 0
        else:
            elapsed = 0

        # Optional Track Status Banner
        track_banner = None
        if self.tracker.session_id:
            if self.recording_state == "RECORDING":
                st_color = COLOR_DANGER
                st_label = "LIVE AUFZEICHNUNG"
                st_icon = ft.Icons.FIBER_MANUAL_RECORD
            elif self.recording_state == "PAUSED":
                st_color = "#FFA726"
                st_label = "PAUSIERT"
                st_icon = ft.Icons.PAUSE_CIRCLE_FILLED
            else:
                st_color = COLOR_PRIMARY
                st_label = "GELADENE FAHRT"
                st_icon = ft.Icons.FOLDER_OPEN_ROUNDED

            track_banner = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Icon(st_icon, color=st_color, size=15),
                        ft.Text(f"{st_label}: {self.tracker.session_id}", size=11, weight=ft.FontWeight.BOLD, color=st_color),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=6,
                ),
                bgcolor="rgba(255, 255, 255, 0.04)",
                border_radius=6,
                padding=ft.Padding.symmetric(vertical=6, horizontal=12),
                border=ft.Border.all(1, f"{st_color}33"),
            )

        # 1. Hero Card: ANGEBOTSQUALITÄT (SAQ)
        saq_hero_card = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        "ANGEBOTSQUALITÄT (SAQ)",
                        size=11,
                        weight=ft.FontWeight.BOLD,
                        color="#ffffff",
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        letter,
                        size=52,
                        weight=ft.FontWeight.W_800,
                        color=saq_color,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        grade_desc,
                        size=13,
                        color="rgba(255, 255, 255, 0.8)",
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=2,
            ),
            bgcolor=COLOR_CARD,
            border=ft.Border.all(1, "rgba(255, 255, 255, 0.08)"),
            border_radius=6,
            padding=ft.Padding.symmetric(vertical=14, horizontal=16),
            alignment=ft.Alignment.CENTER,
        )

        # 2. Line Chart (right-aligned SAQ labels, no top legend)
        chart_container = ft.Container(
            content=self._chart(),
            height=260,
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=ft.Padding.only(top=14, bottom=8, left=10, right=14),
            border=ft.Border.all(1, "rgba(255, 255, 255, 0.06)"),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

        # 3. Two-Column Metric Cards
        row_dist = ft.Row(
            controls=[
                self._metric_card("FAHRTDISTANZ", f"{metrics.total_distance_km:.2f}", "km", highlight=True),
                self._metric_card("LUFTLINIE", f"{metrics.straight_distance_km:.2f}", "km", highlight=True),
            ],
            spacing=10,
        )

        row_speed = ft.Row(
            controls=[
                self._metric_card("V-AKTUELL (5 PKT)", f"{metrics.current_speed_kmh:.1f}", "km/h"),
                self._metric_card("V-LUFTLINIE", f"{metrics.straight_speed_kmh:.1f}", "km/h"),
            ],
            spacing=10,
        )

        row_time = ft.Row(
            controls=[
                self._metric_card("ZEIT (TOTAL)", format_duration(elapsed)),
                self._metric_card("ZEIT (BEWEGUNG)", format_duration(metrics.moving_time_ms)),
            ],
            spacing=10,
        )

        dash_controls = []
        if track_banner:
            dash_controls.append(track_banner)
        dash_controls.extend([
            saq_hero_card,
            chart_container,
            row_dist,
            row_speed,
            row_time,
            ft.Container(height=8),
        ])

        self.dashboard_column.controls = dash_controls
        return self.dashboard_column

    def _chart(self) -> ftc.LineChart:
        metrics = self.tracker.metrics
        curr_dist = metrics.straight_distance_km
        curr_speed = metrics.straight_speed_kmh

        # Bounds and offsets with settings
        step_x = max(1.0, float(getattr(self.settings, "chart_x_step", 10.0)))
        min_distance = round(float(getattr(self.settings, "chart_min_x", 0.0)), 2)
        default_max_x = float(getattr(self.settings, "chart_default_max_x", 50.0))
        x_offset = float(getattr(self.settings, "chart_x_offset", 10.0))
        max_dist_target = max(default_max_x, curr_dist + x_offset)
        max_distance = round(math.ceil(max_dist_target / step_x) * step_x, 2)

        step_y = max(1.0, float(getattr(self.settings, "chart_y_step", 10.0)))
        min_speed = round(max(0.0, float(getattr(self.settings, "chart_min_y", 0.0))), 2)
        default_max_y = float(getattr(self.settings, "chart_default_max_y", 60.0))
        y_offset = float(getattr(self.settings, "chart_y_offset", 10.0))
        min_allowed_y = max(20.0, default_max_y)
        max_speed_target = max(min_allowed_y, curr_speed + y_offset)
        max_speed = round(math.ceil(max_speed_target / step_y) * step_y, 2)

        curve_colors = [
            "#69F0AE",  # SAQ A
            "#81C784",  # SAQ B
            "#FFF176",  # SAQ C
            "#FFB74D",  # SAQ D
            "#FF8A65",  # SAQ E
        ]

        series = []
        params = self.settings.params[self.settings.mode]
        right_labels = []

        # Generate SAQ curves with integer right-end alignment for 1.0 step axis labels
        # Suppress tooltips and selection dots on curves: only tracked GPS data displays tooltips
        for index in range(5):
            end_denom = (params["a"][index] * (max_distance ** params["b"][index])) + params["c"][index]
            raw_end_speed = (1.0 / end_denom) if end_denom else 0.0
            end_speed_int = max(1, min(int(round(raw_end_speed)), int(max_speed)))

            points = []
            for step in range(50):
                distance = min_distance + (max_distance - min_distance) * (step / 50.0)
                safe_dist = max(0.05, distance)
                denom = (params["a"][index] * (safe_dist ** params["b"][index])) + params["c"][index]
                speed = (1.0 / denom) if denom else 0.0
                points.append(
                    ftc.LineChartDataPoint(
                        round(distance, 2),
                        round(min(speed, max_speed), 2),
                        show_tooltip=False,
                    )
                )

            # Final curve point lands exactly on the integer y-coordinate of the axis label
            points.append(
                ftc.LineChartDataPoint(
                    round(max_distance, 2),
                    float(end_speed_int),
                    show_tooltip=False,
                )
            )

            # Labeled at the right end of the curve line and slightly above it in the graph color
            saq_letter = ["A", "B", "C", "D", "E"][index]
            right_labels.append(
                ftc.ChartAxisLabel(
                    value=float(end_speed_int),
                    label=ft.Container(
                        content=ft.Text(
                            f"Stufe {saq_letter}",
                            size=10,
                            weight=ft.FontWeight.BOLD,
                            color=curve_colors[index],
                        ),
                        offset=ft.Offset(0, -0.4),
                        margin=ft.Margin(left=4, bottom=6, top=0, right=0),
                    ),
                )
            )

            series.append(
                ftc.LineChartData(
                    points=points,
                    color=curve_colors[index],
                    stroke_width=1.5,
                    curved=True,
                    point=False,
                    selected_point=False,
                    selected_below_line=False,
                )
            )

        # Must sort right_labels strictly ascending by value to satisfy fl_chart
        right_labels.sort(key=lambda l: l.value)

        # GPS trajectory: display data on click only for tracked data (time rounded to min, v Luft, grade)
        points_history = self.store.points_for(self.tracker.session_id)
        if points_history:
            trajectory = []
            for pt in points_history:
                dt = datetime.fromtimestamp(pt.timestamp_ms / 1000)
                time_str = dt.strftime("%H:%M")
                tip_text = f"{time_str}\nV-Luft: {pt.straight_speed_kmh:.1f} km/h\nStufe {pt.saq}"
                trajectory.append(
                    ftc.LineChartDataPoint(
                        round(pt.straight_distance_km, 2),
                        round(pt.straight_speed_kmh, 2),
                        show_tooltip=True,
                        tooltip=tip_text,
                    )
                )
            series.append(
                ftc.LineChartData(
                    points=trajectory,
                    color=COLOR_PRIMARY,
                    stroke_width=2.5,
                    curved=False,
                    point=False,
                    selected_point=False,
                    selected_below_line=False,
                )
            )

        # Explicit clean labels for bottom axis (x) avoiding overlapping integers
        bottom_labels = []
        x_val = min_distance
        while x_val <= max_distance + 0.001:
            lbl_str = f"{int(x_val)}" if x_val == int(x_val) else f"{x_val:.1f}"
            bottom_labels.append(
                ftc.ChartAxisLabel(
                    value=round(x_val, 2),
                    label=ft.Text(lbl_str, size=9, color="rgba(255,255,255,0.7)"),
                )
            )
            x_val += step_x

        # Explicit clean labels for left axis (y) avoiding overlapping integers
        left_labels = []
        y_val = min_speed
        while y_val <= max_speed + 0.001:
            lbl_str = f"{int(y_val)}" if y_val == int(y_val) else f"{y_val:.1f}"
            left_labels.append(
                ftc.ChartAxisLabel(
                    value=round(y_val, 2),
                    label=ft.Text(lbl_str, size=9, color="rgba(255,255,255,0.7)"),
                )
            )
            y_val += step_y

        return ftc.LineChart(
            data_series=series,
            min_x=min_distance,
            max_x=max_distance,
            min_y=min_speed,
            max_y=max_speed,
            interactive=True,
            tooltip=ftc.LineChartTooltip(
                bgcolor="rgba(15, 23, 42, 0.95)",
                border_side=ft.BorderSide(1, "rgba(255,255,255,0.15)"),
                border_radius=4,
                fit_inside_horizontally=True,
                fit_inside_vertically=True,
            ),
            left_axis=ftc.ChartAxis(
                labels=left_labels,
                title=ft.Text("V-Luftlinie (km/h)", size=10, color="rgba(255,255,255,0.7)"),
                title_size=24,
                show_labels=True,
                label_size=28,
            ),
            bottom_axis=ftc.ChartAxis(
                labels=bottom_labels,
                title=ft.Text("Luftliniendistanz (km)", size=10, color="rgba(255,255,255,0.7)"),
                title_size=20,
                show_labels=True,
                label_size=20,
            ),
            right_axis=ftc.ChartAxis(
                labels=right_labels,
                show_labels=True,
                label_size=55,
                label_spacing=1.0,
            ),
            horizontal_grid_lines=ftc.ChartGridLines(color="rgba(255,255,255,0.05)", width=1),
            vertical_grid_lines=ftc.ChartGridLines(color="rgba(255,255,255,0.05)", width=1),
        )

    def data_view(self) -> ft.Column:
        sessions = self.store.list_sessions()
        points = self.store.points_for(self.tracker.session_id)
        
        # Summary & Export Action Bar (wraps into second row on narrow smartphone screens)
        action_row = ft.Row(
            wrap=True,
            spacing=8,
            run_spacing=8,
            controls=[
                ft.Button(
                    content=ft.Row([ft.Icon(ft.Icons.CLOUD_UPLOAD_ROUNDED, size=16), ft.Text("Daten an Server senden", size=13)], spacing=6),
                    bgcolor="#10B981",
                    color="#ffffff",
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=6),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    ),
                    on_click=lambda _: self.confirm_server_upload(None),
                ),
                ft.Button(
                    content=ft.Row([ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, size=16), ft.Text("Exportieren", size=13)], spacing=6),
                    bgcolor=COLOR_PRIMARY,
                    color="#ffffff",
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=6),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    ),
                    on_click=lambda _: self.prompt_export(None, is_share=False),
                ),
                ft.Button(
                    content=ft.Row([ft.Icon(ft.Icons.ALL_INBOX_ROUNDED, size=16), ft.Text("Alle exportieren", size=13)], spacing=6),
                    bgcolor="rgba(59, 130, 214, 0.2)",
                    color=COLOR_PRIMARY,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=6),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    ),
                    on_click=lambda _: self.prompt_export_all(is_share=False),
                ),
                ft.Button(
                    content=ft.Row([ft.Icon(ft.Icons.SHARE_ROUNDED, size=16), ft.Text("Teilen", size=13)], spacing=6),
                    bgcolor="rgba(255,255,255,0.08)",
                    color=COLOR_TEXT_PRIMARY,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=6),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    ),
                    on_click=lambda _: self.prompt_export(None, is_share=True),
                ),
                ft.Button(
                    content=ft.Row([ft.Icon(ft.Icons.CONTENT_COPY, size=16), ft.Text("Kopieren", size=13)], spacing=6),
                    bgcolor="rgba(255,255,255,0.08)",
                    color=COLOR_TEXT_PRIMARY,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=6),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    ),
                    on_click=lambda _: self._page.run_task(self.copy_csv),
                ),
                ft.Button(
                    content=ft.Row([ft.Icon(ft.Icons.VISIBILITY, size=16), ft.Text("Vorschau", size=13)], spacing=6),
                    bgcolor="rgba(255,255,255,0.08)",
                    color=COLOR_TEXT_PRIMARY,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=6),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                    ),
                    on_click=self.toggle_csv,
                ),
            ],
        )

        # Track History Section
        history_cards = []
        if not sessions:
            history_cards.append(
                ft.Container(
                    content=ft.Text("Noch keine Fahrten gespeichert.", size=12, color=COLOR_TEXT_MUTED),
                    alignment=ft.Alignment.CENTER,
                    padding=10,
                )
            )
        else:
            for s in sessions:
                is_active = (self.tracker.session_id == s["id"])
                stamp_str = datetime.fromtimestamp(s["started_ms"] / 1000).strftime("%d.%m.%Y %H:%M")
                history_cards.append(
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Column(
                                    controls=[
                                        ft.Row(
                                            controls=[
                                                ft.Text(stamp_str, size=12, weight=ft.FontWeight.BOLD, color=COLOR_TEXT_PRIMARY),
                                                ft.Container(
                                                    content=ft.Text("AKTIV", size=9, weight=ft.FontWeight.BOLD, color="#ffffff"),
                                                    bgcolor=COLOR_PRIMARY,
                                                    border_radius=4,
                                                    padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                                                ) if is_active else ft.Container(),
                                            ],
                                            spacing=8,
                                        ),
                                        ft.Text(f"ID: {s['id']}", size=10, color=COLOR_TEXT_MUTED),
                                        ft.Row(
                                            controls=[
                                                ft.Text(f"{s['total_distance_km']:.2f} km", size=11, weight=ft.FontWeight.W_600, color=COLOR_PRIMARY),
                                                ft.Text("·", size=11, color=COLOR_TEXT_MUTED),
                                                ft.Text(f"Luft: {s['straight_distance_km']:.2f} km", size=11, color=COLOR_TEXT_PRIMARY),
                                                ft.Text("·", size=11, color=COLOR_TEXT_MUTED),
                                                ft.Text(f"{s['point_count']} Pkt", size=11, color=COLOR_TEXT_MUTED),
                                                ft.Text("·", size=11, color=COLOR_TEXT_MUTED),
                                                ft.Text(format_duration(s["duration_ms"]), size=11, color=COLOR_TEXT_MUTED),
                                            ],
                                            spacing=6,
                                        ),
                                    ],
                                    spacing=2,
                                    expand=True,
                                    tight=True,
                                ),
                                ft.Row(
                                    controls=[
                                        ft.IconButton(
                                            icon=ft.Icons.CLOUD_UPLOAD_ROUNDED,
                                            icon_size=20,
                                            icon_color="#10B981",
                                            tooltip="Diese Fahrt an Server senden",
                                            on_click=lambda _, sid=s["id"]: self.confirm_server_upload(sid),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                                            icon_size=20,
                                            icon_color=COLOR_PRIMARY,
                                            tooltip="Diese Fahrt im Dashboard öffnen",
                                            on_click=lambda _, sid=s["id"]: self.open_session(sid),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.DOWNLOAD_ROUNDED,
                                            icon_size=20,
                                            icon_color="rgba(255,255,255,0.7)",
                                            tooltip="Fahrt exportieren (GPX oder CSV)",
                                            on_click=lambda _, sid=s["id"]: self.prompt_export(sid, is_share=False),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.SHARE_ROUNDED,
                                            icon_size=20,
                                            icon_color="rgba(255,255,255,0.7)",
                                            tooltip="Fahrt teilen (GPX oder CSV)",
                                            on_click=lambda _, sid=s["id"]: self.prompt_export(sid, is_share=True),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                                            icon_size=20,
                                            icon_color=COLOR_DANGER,
                                            tooltip="Fahrt löschen",
                                            on_click=lambda _, sid=s["id"]: self.delete_session(sid),
                                        ),
                                    ],
                                    spacing=2,
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bgcolor="rgba(59, 130, 214, 0.08)" if is_active else "rgba(255, 255, 255, 0.03)",
                        border_radius=4,
                        padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                        border=ft.Border.all(1, COLOR_PRIMARY if is_active else "rgba(255, 255, 255, 0.05)"),
                    )
                )

        history_group = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.HISTORY_ROUNDED, size=16, color=COLOR_PRIMARY),
                            ft.Text(f"Fahrten-Historie ({len(sessions)})", size=13, weight=ft.FontWeight.BOLD, color="#ffffff"),
                        ],
                        spacing=6,
                    ),
                    ft.Column(controls=history_cards, spacing=6),
                ],
                spacing=8,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=12,
        )

        rows = []
        if not points:
            rows.append(
                ft.Container(
                    content=ft.Text("Keine Datenpunkte für die ausgewählte Fahrt vorhanden.", color="rgba(255,255,255,0.4)"),
                    alignment=ft.Alignment.CENTER,
                    padding=20,
                )
            )
        else:
            rows.append(
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Text("Zeit", size=11, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.6)", expand=2),
                            ft.Text("V-Akt", size=11, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.6)", expand=2),
                            ft.Text("V-Luftlinie", size=11, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.6)", expand=2),
                            ft.Text("Luftlinie", size=11, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.6)", expand=2),
                            ft.Text("SAQ", size=11, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.6)", expand=1),
                        ]
                    ),
                    bgcolor="rgba(0,0,0,0.3)",
                    padding=ft.Padding.symmetric(vertical=8, horizontal=10),
                    border_radius=4,
                )
            )
            for pt in reversed(points[-100:]):
                time_str = datetime.fromtimestamp(pt.timestamp_ms / 1000).strftime("%H:%M:%S")
                saq_col = SAQ_COLORS.get(pt.saq, "#ccc")
                rows.append(
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Text(time_str, size=11, color="rgba(255,255,255,0.8)", expand=2),
                                ft.Text(f"{pt.instant_speed_kmh:.1f}", size=11, color="rgba(255,255,255,0.8)", expand=2),
                                ft.Text(f"{pt.straight_speed_kmh:.1f}", size=11, color="rgba(255,255,255,0.8)", expand=2),
                                ft.Text(f"{pt.straight_distance_km:.2f}", size=11, color="rgba(255,255,255,0.8)", expand=2),
                                ft.Container(
                                    content=ft.Text(pt.saq, size=11, weight=ft.FontWeight.BOLD, color="#0d0d1a"),
                                    bgcolor=saq_col,
                                    border_radius=4,
                                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                                    alignment=ft.Alignment.CENTER,
                                    width=24,
                                ),
                            ]
                        ),
                        padding=ft.Padding.symmetric(vertical=6, horizontal=10),
                        border=ft.Border(bottom=ft.BorderSide(1, "rgba(255,255,255,0.04)")),
                    )
                )

        table_box = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.LIST_ALT_ROUNDED, size=16, color=COLOR_PRIMARY),
                            ft.Text(f"Datenpunkte ({self.tracker.session_id or 'Keine Fahrt'}: {len(points)} Pkt)", size=13, weight=ft.FontWeight.BOLD, color="#ffffff"),
                        ],
                        spacing=6,
                    ),
                    ft.Column(controls=rows, scroll=ft.ScrollMode.AUTO, spacing=2),
                ],
                spacing=8,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=10,
        )

        controls = [action_row, history_group]

        if self.csv_visible:
            controls.append(
                ft.TextField(
                    value=self._csv_for(points),
                    multiline=True,
                    min_lines=6,
                    max_lines=10,
                    read_only=True,
                    bgcolor="#0d0d1a",
                    color="#69F0AE",
                    text_size=11,
                )
            )

        controls.append(table_box)
        controls.append(
            ft.Button(
                content=ft.Text("Aktive Messdaten leeren"),
                bgcolor="rgba(255, 107, 107, 0.15)",
                color=COLOR_DANGER,
                on_click=self.reset_session,
            )
        )

        self.data_column.controls = controls
        return self.data_column

    def _csv_for(self, points: list[LocationPoint]) -> str:
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "SessionID", "Timestamp_ms", "ISO_Time", "Latitude", "Longitude",
            "Accuracy_m", "V_Aktuell_kmh", "V_Luftlinie_kmh", "Luftlinie_km",
            "Fahrtdistanz_km", "SAQ"
        ])
        for pt in points:
            iso_time = datetime.fromtimestamp(pt.timestamp_ms / 1000).isoformat()
            writer.writerow([
                pt.session_id or self.tracker.session_id or "default",
                pt.timestamp_ms,
                iso_time,
                f"{pt.latitude:.6f}",
                f"{pt.longitude:.6f}",
                f"{pt.accuracy_m:.1f}",
                f"{pt.instant_speed_kmh:.1f}",
                f"{pt.straight_speed_kmh:.1f}",
                f"{pt.straight_distance_km:.3f}",
                f"{pt.total_distance_km:.3f}",
                pt.saq,
            ])
        return output.getvalue()

    def _csv_for_all(self, all_pts: list[LocationPoint]) -> str:
        return self._csv_for(all_pts)

    def _gpx_for(self, points: list[LocationPoint], session_id: str | None = None) -> str:
        trk_name = session_id or (points[0].session_id if points else "RIN_Track")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="RIN-Live - ISV Universitaet Stuttgart"',
            '     xmlns="http://www.topografix.com/GPX/1/1"',
            '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
            '     xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">',
            '  <metadata>',
            f'    <name>RIN-Live Fahrt {trk_name}</name>',
            f'    <time>{now_iso}</time>',
            '  </metadata>',
            '  <trk>',
            f'    <name>{trk_name}</name>',
            '    <trkseg>',
        ]
        for pt in points:
            pt_iso = datetime.fromtimestamp(pt.timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            speed_ms = max(0.0, pt.instant_speed_kmh / 3.6)
            lines.append(f'      <trkpt lat="{pt.latitude:.6f}" lon="{pt.longitude:.6f}">')
            lines.append(f'        <time>{pt_iso}</time>')
            lines.append(f'        <speed>{speed_ms:.2f}</speed>')
            lines.append('        <extensions>')
            lines.append(f'          <v_aktuell_kmh>{pt.instant_speed_kmh:.1f}</v_aktuell_kmh>')
            lines.append(f'          <v_luftlinie_kmh>{pt.straight_speed_kmh:.1f}</v_luftlinie_kmh>')
            lines.append(f'          <luftlinie_km>{pt.straight_distance_km:.3f}</luftlinie_km>')
            lines.append(f'          <fahrtdistanz_km>{pt.total_distance_km:.3f}</fahrtdistanz_km>')
            lines.append(f'          <saq>{pt.saq}</saq>')
            lines.append('        </extensions>')
            lines.append('      </trkpt>')
        lines.append('    </trkseg>')
        lines.append('  </trk>')
        lines.append('</gpx>')
        return "\n".join(lines)

    def _gpx_for_all(self, all_pts: list[LocationPoint]) -> str:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="RIN-Live - ISV Universitaet Stuttgart"',
            '     xmlns="http://www.topografix.com/GPX/1/1"',
            '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
            '     xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">',
            '  <metadata>',
            '    <name>RIN-Live Gesamtexport</name>',
            f'    <time>{now_iso}</time>',
            '  </metadata>',
        ]
        groups: dict[str, list[LocationPoint]] = {}
        for pt in all_pts:
            sid = pt.session_id or "default"
            groups.setdefault(sid, []).append(pt)

        for sid, pts in groups.items():
            lines.append('  <trk>')
            lines.append(f'    <name>{sid}</name>')
            lines.append('    <trkseg>')
            for pt in pts:
                pt_iso = datetime.fromtimestamp(pt.timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                speed_ms = max(0.0, pt.instant_speed_kmh / 3.6)
                lines.append(f'      <trkpt lat="{pt.latitude:.6f}" lon="{pt.longitude:.6f}">')
                lines.append(f'        <time>{pt_iso}</time>')
                lines.append(f'        <speed>{speed_ms:.2f}</speed>')
                lines.append('        <extensions>')
                lines.append(f'          <v_aktuell_kmh>{pt.instant_speed_kmh:.1f}</v_aktuell_kmh>')
                lines.append(f'          <v_luftlinie_kmh>{pt.straight_speed_kmh:.1f}</v_luftlinie_kmh>')
                lines.append(f'          <luftlinie_km>{pt.straight_distance_km:.3f}</luftlinie_km>')
                lines.append(f'          <fahrtdistanz_km>{pt.total_distance_km:.3f}</fahrtdistanz_km>')
                lines.append(f'          <saq>{pt.saq}</saq>')
                lines.append('        </extensions>')
                lines.append('      </trkpt>')
            lines.append('    </trkseg>')
            lines.append('  </trk>')
        lines.append('</gpx>')
        return "\n".join(lines)

    def debug_view(self) -> ft.Column:
        pos = self.last_position
        if pos:
            loc_info = f"Lat: {pos.latitude:.6f} · Lon: {pos.longitude:.6f} · Genauigkeit: {getattr(pos, 'accuracy', 0.0) or 0.0:.1f} m"
        else:
            loc_info = "Warte auf GPS-Signal..."

        self.debug_column.controls = [
            ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Row([ft.Text("Status:", weight=ft.FontWeight.BOLD), ft.Text("RECORDING" if self.recording else "INAKTIV", color=COLOR_DANGER if self.recording else COLOR_PRIMARY)]),
                        ft.Row([ft.Text("GPS Signal:", weight=ft.FontWeight.BOLD), ft.Text(loc_info, size=12)]),
                        ft.Row([ft.Text("Intervall / Genauigkeit:", weight=ft.FontWeight.BOLD), ft.Text(f"{self.settings.gps_interval} s / < {self.settings.min_accuracy} m", size=12)]),
                    ],
                    spacing=6,
                ),
                bgcolor=COLOR_CARD,
                border_radius=6,
                padding=14,
            ),
            ft.Button(
                content=ft.Text("GPS manuell abfragen"),
                bgcolor=COLOR_PRIMARY,
                color="#ffffff",
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                on_click=lambda _: self._page.run_task(self.fetch_location),
            ),
            ft.Text("Live Console Log", size=13, weight=ft.FontWeight.BOLD, color="#ffffff"),
            ft.Container(
                content=ft.Text("\n".join(self.logs), size=11, color="#69F0AE", selectable=True),
                bgcolor="rgba(0,0,0,0.5)",
                border_radius=6,
                padding=12,
                border=ft.Border.all(1, "rgba(255,255,255,0.08)"),
                expand=True,
            ),
        ]
        return self.debug_column

    def settings_view(self) -> ft.Column:
        filter_group = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text("Filter & Evaluierung", size=14, weight=ft.FontWeight.BOLD, color="#ffffff"),
                    ft.Row(
                        controls=[
                            ft.Text("Bewertungsmodus", size=13, expand=True),
                            ft.Dropdown(
                                value=self.settings.mode,
                                width=120,
                                options=[
                                    ft.DropdownOption(key="IOE", text="IÖ"),
                                    ft.DropdownOption(key="PKW", text="PKW"),
                                    ft.DropdownOption(key="OEV", text="ÖV"),
                                ],
                                on_select=self.set_mode,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Column([
                                ft.Text("Max V-Aktuell (km/h)", size=13),
                                ft.Text("Spikes ignorieren", size=10, color=COLOR_TEXT_MUTED),
                            ], spacing=0, expand=True),
                            ft.TextField(value=str(int(self.settings.max_current_speed)), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("max_current_speed")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("Max V-Luft (km/h)", size=13, expand=True),
                            ft.TextField(value=str(int(self.settings.max_straight_speed)), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("max_straight_speed")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("minimale Bewegungsgeschwindigkeit (km/h)", size=13, expand=True),
                            ft.TextField(value=str(int(self.settings.moving_cutoff)), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("moving_cutoff")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("Min. GPS-Genauigkeit (m)", size=13, expand=True),
                            ft.TextField(value=str(int(self.settings.min_accuracy)), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("min_accuracy")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("GPS-Intervall (Sekunden)", size=13, expand=True),
                            ft.TextField(value=str(self.settings.gps_interval), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("gps_interval")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ],
                spacing=10,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=14,
        )

        param_rows = [
            ft.Row(
                controls=[
                    ft.Text("SAQ", size=12, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.7)", width=35),
                    ft.Text("a", size=12, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.7)", expand=True, text_align=ft.TextAlign.CENTER),
                    ft.Text("b", size=12, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.7)", expand=True, text_align=ft.TextAlign.CENTER),
                    ft.Text("c", size=12, weight=ft.FontWeight.BOLD, color="rgba(255,255,255,0.7)", expand=True, text_align=ft.TextAlign.CENTER),
                ]
            )
        ]

        current_params = self.settings.params[self.settings.mode]
        for idx, letter in enumerate(["A", "B", "C", "D", "E"]):
            param_rows.append(
                ft.Row(
                    controls=[
                        ft.Text(letter, size=13, weight=ft.FontWeight.BOLD, color=SAQ_COLORS[letter], width=35),
                        ft.TextField(value=str(current_params["a"][idx]), text_align=ft.TextAlign.CENTER, expand=True, on_change=self.set_parameter("a", idx)),
                        ft.TextField(value=str(current_params["b"][idx]), text_align=ft.TextAlign.CENTER, expand=True, on_change=self.set_parameter("b", idx)),
                        ft.TextField(value=str(current_params["c"][idx]), text_align=ft.TextAlign.CENTER, expand=True, on_change=self.set_parameter("c", idx)),
                    ],
                    spacing=6,
                )
            )

        saq_group = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text("SAQ Parameter (RIN 2008 Kurven)", size=14, weight=ft.FontWeight.BOLD, color="#ffffff"),
                    ft.Text("Grenzfunktions-Parameter (a, b, c) gemäß RIN (Ausgabe 2008)", size=11, color=COLOR_TEXT_MUTED),
                    *param_rows,
                    ft.Container(height=6),
                    ft.Row(
                        controls=[
                            ft.Button(
                                content=ft.Text("Reset Default"),
                                bgcolor="rgba(255, 107, 107, 0.15)",
                                color=COLOR_DANGER,
                                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                                on_click=self.reset_parameters,
                                expand=True,
                            ),
                            ft.Button(
                                content=ft.Text("Speichern"),
                                bgcolor=COLOR_PRIMARY,
                                color="#ffffff",
                                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                                on_click=self.save_settings,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                    ),
                ],
                spacing=8,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=14,
        )

        chart_group = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text("Diagramm Einstellungen (Bounds & Offset)", size=14, weight=ft.FontWeight.BOLD, color="#ffffff"),
                    ft.Row(
                        controls=[
                            ft.Text("Standard X-Max (Luftlinie km)", size=13, expand=True),
                            ft.TextField(value=str(int(getattr(self.settings, "chart_default_max_x", 50.0))), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("chart_default_max_x")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Column([
                                ft.Text("Standard Y-Max (V-Luftlinie km/h)", size=13),
                                ft.Text("Mindestens 20 km/h", size=10, color=COLOR_TEXT_MUTED),
                            ], spacing=0, expand=True),
                            ft.TextField(value=str(int(getattr(self.settings, "chart_default_max_y", 60.0))), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("chart_default_max_y")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("X-Offset (km)", size=13, expand=True),
                            ft.TextField(value=str(int(getattr(self.settings, "chart_x_offset", 10.0))), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("chart_x_offset")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("Y-Offset (km/h)", size=13, expand=True),
                            ft.TextField(value=str(int(getattr(self.settings, "chart_y_offset", 10.0))), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("chart_y_offset")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("X-Schrittweite (Raster km)", size=13, expand=True),
                            ft.TextField(value=str(int(getattr(self.settings, "chart_x_step", 10.0))), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("chart_x_step")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text("Y-Schrittweite (Raster km/h)", size=13, expand=True),
                            ft.TextField(value=str(int(getattr(self.settings, "chart_y_step", 10.0))), width=120, text_align=ft.TextAlign.RIGHT, on_change=self.set_number("chart_y_step")),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ],
                spacing=10,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=14,
        )

        permissions_group = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.SECURITY_ROUNDED, size=16, color=COLOR_PRIMARY),
                        ft.Text("Android System-Berechtigungen & Akku", size=14, weight=ft.FontWeight.BOLD, color="#ffffff"),
                    ], spacing=6),
                    ft.Text(
                        "Für zuverlässige Messungen im Hintergrund bei gesperrtem Bildschirm müssen der Standort auf 'Immer zulassen' und die Akku-Nutzung auf 'Nicht eingeschränkt' gesetzt sein.",
                        size=11,
                        color=COLOR_TEXT_MUTED,
                    ),
                    ft.Row(
                        controls=[
                            ft.Button(
                                content=ft.Row([
                                    ft.Icon(ft.Icons.BATTERY_ALERT_ROUNDED, size=15),
                                    ft.Text("App-Einstellungen (Akku)", size=11, weight=ft.FontWeight.BOLD),
                                ], spacing=6, tight=True),
                                bgcolor="rgba(245, 158, 11, 0.15)",
                                color="#f59e0b",
                                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                                on_click=lambda _: self._page.run_task(self.open_app_settings),
                            ),
                            ft.Button(
                                content=ft.Row([
                                    ft.Icon(ft.Icons.LOCATION_ON_ROUNDED, size=15),
                                    ft.Text("Standort-Einstellungen", size=11, weight=ft.FontWeight.BOLD),
                                ], spacing=6, tight=True),
                                bgcolor="rgba(59, 130, 214, 0.15)",
                                color=COLOR_PRIMARY,
                                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                                on_click=lambda _: self._page.run_task(self.open_location_settings),
                            ),
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                ],
                spacing=8,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=14,
        )

        api_group = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row([
                        ft.Icon(ft.Icons.CLOUD_UPLOAD_ROUNDED, size=16, color=COLOR_PRIMARY),
                        ft.Text("Datenspende", size=14, weight=ft.FontWeight.BOLD, color="#ffffff"),
                    ], spacing=6),
                    ft.Text(
                        "API-Endpoint für die Übertragung anonymisierter Fahrtdaten. Standardmäßig ist der Server des Institut für Straßen- und Verkehrswesen (ISV) der Universität Stuttgart voreingestellt.",
                        size=11,
                        color=COLOR_TEXT_MUTED,
                    ),
                    ft.TextField(
                        value=str(getattr(self.settings, "api_endpoint", "https://rin.isv.uni-stuttgart.de/api/v1/")),
                        label="Server API-Endpunkt URL",
                        hint_text="https://rin.isv.uni-stuttgart.de/api/v1/",
                        text_size=12,
                        on_change=self.set_string("api_endpoint"),
                    ),
                    ft.Row(
                        controls=[
                            ft.Button(
                                content=ft.Text("Standard wiederherstellen"),
                                bgcolor="rgba(255, 255, 255, 0.08)",
                                color="rgba(255, 255, 255, 0.8)",
                                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                                on_click=self.reset_api_endpoint,
                            ),
                            ft.Button(
                                content=ft.Text("Speichern"),
                                bgcolor=COLOR_PRIMARY,
                                color="#ffffff",
                                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=6)),
                                on_click=self.save_settings,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.END,
                        spacing=8,
                    ),
                ],
                spacing=8,
            ),
            bgcolor=COLOR_CARD,
            border_radius=6,
            padding=14,
        )

        self.settings_column.controls = [
            filter_group,
            chart_group,
            saq_group,
            permissions_group,
            api_group,
            ft.Container(height=10),
        ]
        return self.settings_column

    def render(self) -> None:
        views = {
            "dashboard": self.dashboard_view,
            "data": self.data_view,
            "debug": self.debug_view,
            "settings": self.settings_view,
        }
        content = views[self.active_tab]()
        self.content_container.content = content

        header_bar = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text("RIN-Live", size=22, weight=ft.FontWeight.BOLD, color="#ffffff"),
                    ft.Row(
                        controls=[
                            self._tab_button("Dashboard", "dashboard"),
                            self._tab_button("Daten", "data"),
                            self._tab_button("Debug", "debug"),
                            self._tab_button("⚙ Config", "settings"),
                        ],
                        spacing=6,
                    ),
                ],
                spacing=10,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            bgcolor=COLOR_BG,
            border=ft.Border(bottom=ft.BorderSide(1, "rgba(255,255,255,0.06)")),
        )

        if self.recording_state == "IDLE":
            action_content = ft.Column(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.PLAY_ARROW_ROUNDED,
                        icon_size=36,
                        icon_color="#ffffff",
                        bgcolor=COLOR_PRIMARY,
                        width=64,
                        height=64,
                        style=ft.ButtonStyle(shape=ft.CircleBorder()),
                        on_click=self.start_recording,
                        tooltip="Aufzeichnung starten",
                    ),
                    ft.Text("START", size=11, weight=ft.FontWeight.BOLD, color=COLOR_PRIMARY),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
            )
        elif self.recording_state == "RECORDING":
            action_content = ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.PAUSE_ROUNDED,
                                icon_size=30,
                                icon_color="#0d0d1a",
                                bgcolor="#FFA726",
                                width=56,
                                height=56,
                                style=ft.ButtonStyle(shape=ft.CircleBorder()),
                                on_click=self.pause_recording,
                                tooltip="Pausieren",
                            ),
                            ft.Text("PAUSE", size=10, weight=ft.FontWeight.BOLD, color="#FFA726"),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4,
                    ),
                    ft.Column(
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.STOP_ROUNDED,
                                icon_size=30,
                                icon_color="#ffffff",
                                bgcolor=COLOR_DANGER,
                                width=56,
                                height=56,
                                style=ft.ButtonStyle(shape=ft.CircleBorder()),
                                on_click=self.request_stop_recording,
                                tooltip="Aufzeichnung beenden",
                            ),
                            ft.Text("STOPP", size=10, weight=ft.FontWeight.BOLD, color=COLOR_DANGER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=36,
            )
        else:  # PAUSED
            action_content = ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                icon_size=30,
                                icon_color="#0d0d1a",
                                bgcolor="#69F0AE",
                                width=56,
                                height=56,
                                style=ft.ButtonStyle(shape=ft.CircleBorder()),
                                on_click=self.resume_recording,
                                tooltip="Fortsetzen",
                            ),
                            ft.Text("WEITER", size=10, weight=ft.FontWeight.BOLD, color="#69F0AE"),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4,
                    ),
                    ft.Column(
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.STOP_ROUNDED,
                                icon_size=30,
                                icon_color="#ffffff",
                                bgcolor=COLOR_DANGER,
                                width=56,
                                height=56,
                                style=ft.ButtonStyle(shape=ft.CircleBorder()),
                                on_click=self.request_stop_recording,
                                tooltip="Aufzeichnung beenden",
                            ),
                            ft.Text("STOPP", size=10, weight=ft.FontWeight.BOLD, color=COLOR_DANGER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=36,
            )

        bottom_bar = ft.Container(
            content=action_content,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            alignment=ft.Alignment.CENTER,
            bgcolor=COLOR_BG,
            border=ft.Border(top=ft.BorderSide(1, "rgba(255,255,255,0.06)")),
        )

        self.root.controls = [
            header_bar,
            self.content_container,
            bottom_bar,
        ]
        self._page.update()
