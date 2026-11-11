from django.core.management.base import BaseCommand

from meals import photos
from meals.models import Recipe


class Command(BaseCommand):
    help = ('Find Pixabay photos for recipes that have none. New recipes get this automatically in the '
            'background; --retry also re-searches recipes whose earlier search found nothing.')

    def add_arguments(self, parser):
        parser.add_argument('--retry', action='store_true', help='Re-search recipes with no photo.')
        parser.add_argument('--limit', type=int, default=photos.BATCH_LIMIT)

    def handle(self, retry, limit, **options):
        if retry:
            Recipe.objects.filter(photo_updated_at__isnull=True).update(photo_searched_at=None)
        found = photos.fetch_missing_photos(limit=limit)
        missing = Recipe.objects.filter(photo_updated_at__isnull=True, archived=False).count()
        self.stdout.write(f'Found {found} photo(s); {missing} recipe(s) still without one.')
