"""Kroger public API (King Soopers is a Kroger banner): OAuth, product search, add-to-cart.

The API can fill the household's cart but cannot check out — that still happens in the
King Soopers app/site.
"""
import logging
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from meals.models import KrogerAuth

logger = logging.getLogger(__name__)

USER_SCOPES = 'cart.basic:write product.compact profile.compact'
TIMEOUT = 20


class KrogerNotConnected(Exception):
    pass


def redirect_uri() -> str:
    return f'{settings.APP_BASE_URL}/kroger/callback/'


def authorize_url(state: str) -> str:
    query = urlencode({
        'scope': USER_SCOPES,
        'response_type': 'code',
        'client_id': settings.KROGER_CLIENT_ID,
        'redirect_uri': redirect_uri(),
        'state': state,
    })
    return f'{settings.KROGER_API_BASE}/connect/oauth2/authorize?{query}'


def _token_request(data: dict) -> dict:
    response = requests.post(
        f'{settings.KROGER_API_BASE}/connect/oauth2/token',
        data=data,
        auth=(settings.KROGER_CLIENT_ID, settings.KROGER_CLIENT_SECRET),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _save_tokens(payload: dict) -> KrogerAuth:
    auth, _ = KrogerAuth.objects.update_or_create(pk=1, defaults={
        'access_token': payload['access_token'],
        'refresh_token': payload.get('refresh_token') or KrogerAuth.objects.get(pk=1).refresh_token,
        'expires_at': timezone.now() + timedelta(seconds=payload.get('expires_in', 1800) - 60),
    })
    return auth


def exchange_code(code: str) -> KrogerAuth:
    return _save_tokens(_token_request({
        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri(),
    }))


def _user_token() -> str:
    auth = KrogerAuth.objects.filter(pk=1).first()
    if not auth:
        raise KrogerNotConnected()
    if auth.expires_at <= timezone.now():
        try:
            auth = _save_tokens(_token_request({'grant_type': 'refresh_token', 'refresh_token': auth.refresh_token}))
        except requests.HTTPError as e:
            logger.warning('Kroger token refresh failed: %s', e)
            raise KrogerNotConnected() from e
    return auth.access_token


def _app_token() -> str:
    """Client-credentials token; enough for product and location search."""
    return _token_request({'grant_type': 'client_credentials', 'scope': 'product.compact'})['access_token']


def is_connected() -> bool:
    return KrogerAuth.objects.filter(pk=1).exists()


def search_products(term: str, location_id: str, limit: int = 5, token: str | None = None) -> list[dict]:
    response = requests.get(
        f'{settings.KROGER_API_BASE}/products',
        params={'filter.term': term, 'filter.locationId': location_id, 'filter.limit': limit},
        headers={'Authorization': f'Bearer {token or _app_token()}', 'Accept': 'application/json'},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get('data', [])


def find_locations(zip_code: str) -> list[dict]:
    response = requests.get(
        f'{settings.KROGER_API_BASE}/locations',
        params={'filter.zipCode.near': zip_code, 'filter.limit': 20},
        headers={'Authorization': f'Bearer {_app_token()}', 'Accept': 'application/json'},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get('data', [])


def add_to_cart(items: list[dict]):
    """`items` are {"upc", "quantity"}; added for pickup."""
    response = requests.put(
        f'{settings.KROGER_API_BASE}/cart/add',
        json={'items': [{'upc': i['upc'], 'quantity': i.get('quantity', 1), 'modality': 'PICKUP'} for i in items]},
        headers={'Authorization': f'Bearer {_user_token()}', 'Accept': 'application/json'},
        timeout=TIMEOUT,
    )
    if response.status_code == 401:
        raise KrogerNotConnected()
    response.raise_for_status()
