"""Слой доступа к Supabase."""

from supabase import create_client, Client

from pricing import Settings

DEFAULT_SETTINGS = Settings()


class DB:
    def __init__(self, url: str, key: str):
        self.client: Client = create_client(url, key)

    # ---------- Настройки ----------

    def get_settings(self, chat_id: int) -> Settings:
        res = (
            self.client.table("shops")
            .select("*")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
        if res.data:
            return Settings.from_row(res.data[0])
        # Первый запуск — создаём запись с дефолтами
        row = {"chat_id": chat_id, **DEFAULT_SETTINGS.__dict__}
        self.client.table("shops").insert(row).execute()
        return Settings()

    def update_setting(self, chat_id: int, field: str, value):
        self.client.table("shops").update({field: value}).eq("chat_id", chat_id).execute()

    # ---------- Справочник цветов ----------

    def list_flowers(self, chat_id: int) -> list:
        res = (
            self.client.table("flowers")
            .select("*")
            .eq("chat_id", chat_id)
            .order("name")
            .execute()
        )
        return res.data or []

    def add_flower(self, chat_id: int, name: str, price: float, packaging: float = 0.0):
        self.client.table("flowers").insert(
            {"chat_id": chat_id, "name": name, "purchase_price": price, "packaging": packaging}
        ).execute()

    def update_flower_price(self, flower_id: int, price: float):
        self.client.table("flowers").update({"purchase_price": price}).eq("id", flower_id).execute()

    def delete_flower(self, flower_id: int):
        self.client.table("flowers").delete().eq("id", flower_id).execute()
