from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    llm_mode: str = "api"
    llm_model: str = "gpt-4.1-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    tavily_api_key: str = ""
    data_dir: str = "data"
    tool_timeout: float = Field(5, gt=0, le=60)
    llm_timeout: float = Field(30, gt=0, le=120)
    max_attempts: int = Field(3, ge=1, le=5)
    max_replans: int = Field(2, ge=0, le=4)
    backoff_base: float = Field(0.25, ge=0, le=10)
    backoff_cap: float = Field(4, ge=0, le=30)
    max_tool_calls: int = Field(40, ge=1, le=50)
    api_token: str = ""
