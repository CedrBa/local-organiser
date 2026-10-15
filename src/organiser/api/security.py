import secrets
from typing import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from organiser.core.config import Settings


API_TOKEN_MISSING_DETAIL = (
    "Die API ist nicht konfiguriert: ORGANISER_API_TOKEN fehlt. Geschuetzte Routen sind gesperrt."
)
API_TOKEN_INVALID_DETAIL = "Ungueltiges oder fehlendes API-Token."

bearer_scheme = HTTPBearer(
    auto_error=False, description="Statischer Bearer-Token aus ORGANISER_API_TOKEN."
)


def build_api_token_dependency(settings: Settings) -> Callable[..., None]:
    """Liefert die zentrale Bearer-Token-Dependency fuer die App.

    Der Vergleich laeuft ueber ``secrets.compare_digest`` und der erwartete Token
    wird nie in Fehlermeldungen oder Logs ausgegeben. Ist kein Token konfiguriert,
    bleiben die geschuetzten Routen gesperrt (Fail closed).
    """

    def require_api_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> None:
        expected = settings.api_token
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=API_TOKEN_MISSING_DETAIL
            )

        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=API_TOKEN_INVALID_DETAIL,
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not secrets.compare_digest(credentials.credentials, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=API_TOKEN_INVALID_DETAIL,
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_api_token
