from django.core.management.base import BaseCommand

from meals import kroger


class Command(BaseCommand):
    help = "List Kroger-family stores near a ZIP code, to find the King Soopers locationId for household settings."

    def add_arguments(self, parser):
        parser.add_argument('zip_code')

    def handle(self, *args, zip_code, **options):
        for loc in kroger.find_locations(zip_code):
            address = loc.get('address', {})
            self.stdout.write(
                f"{loc['locationId']}  {loc.get('chain', ''):12}  {loc.get('name', '')} — "
                f"{address.get('addressLine1', '')}, {address.get('city', '')}"
            )
