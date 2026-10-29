from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/newsfeeder.db"
    environment: str = "development"
    app_api_key: str = ""
    demo_mode: bool = False
    worker_enabled: bool = True
    litellm_base_url: str = "https://secondstack.t1a.io/gateway/api/v1"
    litellm_api_key: str = ""
    litellm_model: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_target_type: str = ""

    @property
    def llm_ready(self) -> bool:
        return bool(self.litellm_base_url and self.litellm_api_key and self.litellm_model)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id and not self.demo_mode)
