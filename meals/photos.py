"""Recipe photos: a Pixabay stock photo found in the background for each new recipe, which anyone can
replace with their own.

Finding photos never holds up a request. `fetch_missing_photos_soon` runs `fetch_missing_photos` on a
background thread once the current transaction commits; it works through every recipe that has no
photo and hasn't been searched yet, so recipes a lost thread missed are picked up by the next run.
Photo bytes are stored in `RecipePhoto` (the database, which survives redeploys); the `Recipe.photo_*`
fields carry what pages need to show and credit one.
"""
import io
import logging
import re
import threading

import requests
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from meals.models import Recipe, RecipePhoto

logger = logging.getLogger(__name__)

PIXABAY_SEARCH_URL = 'https://pixabay.com/api/'
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SIDE = 1600  # uploads are scaled down to this; phone photos are far bigger than any page shows
# Recipes searched per background run. Pixabay allows 100 requests a minute and asks for real use, not bulk.
BATCH_LIMIT = 40


class PhotoError(ValueError):
    """An upload we can't use; the message is shown to the person who uploaded it."""


def search_queries(recipe: Recipe) -> list[str]:
    """What to search for, best first: the planner's photo words, then the name without its
    'with …' / 'and …' tail ('Cajun Chicken with Dirty Rice' → 'Cajun Chicken')."""
    short = re.split(r'\s+(?:with|and|&|over|on)\s+|[,(]', recipe.name, maxsplit=1, flags=re.I)[0].strip()
    queries = [recipe.photo_query.strip(), short, recipe.name.strip()]
    return list(dict.fromkeys(q for q in queries if q))


def search_pixabay(query: str) -> dict | None:
    """The best wide food photo for `query`, as Pixabay's hit object, or None.

    Pixabay doesn't allow linking to its image URLs for good, so the caller downloads the image.
    """
    response = requests.get(
        PIXABAY_SEARCH_URL,
        params={'key': settings.PIXABAY_API_KEY, 'q': query[:100], 'image_type': 'photo', 'category': 'food',
                'orientation': 'horizontal', 'safesearch': 'true', 'per_page': 3},
        timeout=15,
    )
    if not response.ok:
        # Not raise_for_status(): its message includes the URL, and the key is in the query string.
        raise requests.HTTPError(f'Pixabay search failed: HTTP {response.status_code}', response=response)
    hits = response.json().get('hits') or []
    return hits[0] if hits else None


def _claim(recipe: Recipe) -> bool:
    """Mark `recipe` as searched, atomically, so two background runs don't both fetch it."""
    now = timezone.now()
    claimed = Recipe.objects.filter(pk=recipe.pk, photo_searched_at__isnull=True, photo_updated_at__isnull=True) \
        .update(photo_searched_at=now)
    recipe.photo_searched_at = now if claimed else recipe.photo_searched_at
    return bool(claimed)


def fetch_photo(recipe: Recipe) -> bool:
    """Find and store a Pixabay photo for `recipe`. Returns whether it now has one.

    A recipe is searched once: no match leaves it without a photo (anyone can add one). A network or
    API error un-claims it so a later run tries again.
    """
    if not _claim(recipe):
        return False
    try:
        for query in search_queries(recipe):
            photo = search_pixabay(query)
            if photo:
                break
        else:
            logger.info('No Pixabay photo for recipe %s (%s)', recipe.pk, recipe.name)
            return False
        image = requests.get(photo.get('largeImageURL') or photo['webformatURL'], timeout=30)
        image.raise_for_status()
    except (requests.RequestException, KeyError, ValueError):
        logger.exception('Pixabay photo fetch failed for recipe %s', recipe.pk)
        Recipe.objects.filter(pk=recipe.pk).update(photo_searched_at=None)
        return False
    content_type = image.headers.get('Content-Type', 'image/jpeg').split(';')[0]
    # Someone may have uploaded their own while we were searching; theirs wins.
    with transaction.atomic():
        if Recipe.objects.select_for_update().filter(pk=recipe.pk, photo_updated_at__isnull=False).exists():
            return True
        _store(recipe, image.content, content_type, source='pixabay', user=None,
               credit=photo.get('user') or 'Unknown', credit_url=photo.get('pageURL', ''))
    return True


def fetch_missing_photos(limit: int = BATCH_LIMIT) -> int:
    """Look up photos for recipes that have none and haven't been searched. Returns how many it found."""
    if not settings.PIXABAY_API_KEY:
        return 0
    recipes = Recipe.objects.filter(photo_updated_at__isnull=True, photo_searched_at__isnull=True,
                                    archived=False).order_by('-created_at')[:limit]
    return sum(fetch_photo(recipe) for recipe in recipes)


def _run_in_background(fn):
    def run():
        try:
            fn()
        except Exception:
            logger.exception('Background photo fetch failed')
        finally:
            connections.close_all()  # this thread's own DB connection
    threading.Thread(target=run, name='recipe-photos', daemon=True).start()


def fetch_missing_photos_soon():
    """Find photos for new recipes after this request's changes are saved, without making it wait."""
    if settings.PIXABAY_API_KEY:
        transaction.on_commit(lambda: _run_in_background(fetch_missing_photos))


def save_upload(recipe: Recipe, upload, user):
    """Use someone's own photo for `recipe`: re-encoded as a JPEG no bigger than MAX_SIDE, upright,
    with its metadata (including any location) dropped."""
    if upload.size > MAX_UPLOAD_BYTES:
        raise PhotoError('That photo is too big (20 MB at most).')
    try:
        image = Image.open(upload)
        image = ImageOps.exif_transpose(image).convert('RGB')
    except (UnidentifiedImageError, OSError):
        raise PhotoError("That file isn't a photo we can read. Try a JPEG or PNG.")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    image.save(out, 'JPEG', quality=85, optimize=True)
    name = user.first_name or user.username
    _store(recipe, out.getvalue(), 'image/jpeg', source='upload', user=user, credit=name, credit_url='')


def remove_photo(recipe: Recipe):
    """Drop the recipe's photo. It isn't searched for again (it stays marked as searched)."""
    RecipePhoto.objects.filter(recipe=recipe).delete()
    recipe.photo_source = recipe.photo_credit = recipe.photo_credit_url = ''
    recipe.photo_updated_at = None
    recipe.photo_searched_at = recipe.photo_searched_at or timezone.now()
    recipe.save(update_fields=['photo_source', 'photo_credit', 'photo_credit_url', 'photo_updated_at',
                               'photo_searched_at'])


def _store(recipe: Recipe, data: bytes, content_type: str, source: str, user, credit: str, credit_url: str):
    RecipePhoto.objects.update_or_create(
        recipe=recipe, defaults={'image': data, 'content_type': content_type, 'uploaded_by': user})
    recipe.photo_source = source
    recipe.photo_credit = credit[:200]
    recipe.photo_credit_url = credit_url[:500]
    recipe.photo_updated_at = timezone.now()
    recipe.photo_searched_at = recipe.photo_searched_at or recipe.photo_updated_at
    recipe.save(update_fields=['photo_source', 'photo_credit', 'photo_credit_url', 'photo_updated_at',
                               'photo_searched_at'])
