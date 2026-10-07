import flet as ft
from views.rin_app import RinApp


def main(page: ft.Page):
    page.title = "RIN-Live"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#0d0d1a"
    page.padding = 0
    RinApp(page)


if __name__ == "__main__":
    ft.run(main)