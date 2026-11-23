from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./foodbridge.db"
    use_postgis: bool = False  # True when the DB has the PostGIS extension
    redis_url: str = ""
    broker_url: str = ""
    event_backend: str = "inline"  # inline | celery
    embedded_worker: bool = True  # local dev: poll outbox/expiry inside the API process

    jwt_secret: str = "replace_me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14

    # matching
    donation_default_radius_km: float = 10.0
    donation_max_radius_km: float = 50.0
    radius_widen_every_minutes: int = 10
    radius_widen_factor: float = 0.5
    admin_alert_after_minutes: int = 30
    weight_proximity: float = 0.45
    weight_capacity_fit: float = 0.25
    weight_pickup_readiness: float = 0.20
    weight_historical: float = 0.10
    avg_courier_speed_kmh: float = 25.0

    # delivery
    volunteer_offer_timeout_minutes: int = 10
    volunteer_location_max_age_minutes: int = 15
    volunteer_search_radius_km: float = 15.0
    receipt_confirmation_timeout_hours: int = 4
    receipt_quantity_tolerance: float = 0.05

    # reliability
    notification_max_retries: int = 5
    rate_limit_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.app_env != "local" and s.jwt_secret == "replace_me":
        raise RuntimeError("JWT_SECRET must be set outside the local environment")
    return s


settings = get_settings()
