from datetime import date

from django.core.management.base import BaseCommand, CommandError

from meals import services
from meals.models import Household


class Command(BaseCommand):
    help = "Generate (or fill in) a week's meal plan for each household, and optionally email it."

    def add_arguments(self, parser):
        parser.add_argument('--week-start', type=date.fromisoformat, help='Any date in the target week (default: today).')
        parser.add_argument('--household', type=int, help='Only this household (id); default: every household with members.')
        parser.add_argument('--regenerate', action='store_true', help='Re-pick every unlocked meal.')
        parser.add_argument('--email', action='store_true', help='Send the summary email afterwards.')

    def handle(self, *args, week_start=None, household=None, regenerate=False, email=False, **options):
        households = Household.objects.filter(pk=household) if household else services.households_to_plan()
        if household and not households.exists():
            raise CommandError(f'No household {household}.')
        for home in households:
            self.stdout.write(self.style.MIGRATE_HEADING(home.name))
            plan = services.generate_week(home, week_start, regenerate=regenerate)
            for meal in plan.meals.select_related('recipe'):
                self.stdout.write(f'{meal.date:%a %b %d} {meal.meal_type}: {meal.recipe.name}')
            if email:
                self.stdout.write(f"Emailed: {', '.join(services.send_plan_email(plan)) or 'nobody'}")
