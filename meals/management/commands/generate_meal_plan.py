from datetime import date

from django.core.management.base import BaseCommand

from meals import services


class Command(BaseCommand):
    help = "Generate (or fill in) the dinner plan for a week, and optionally email it."

    def add_arguments(self, parser):
        parser.add_argument('--week-start', type=date.fromisoformat, help='Any date in the target week (default: today).')
        parser.add_argument('--regenerate', action='store_true', help='Re-pick every unlocked meal.')
        parser.add_argument('--email', action='store_true', help='Send the summary email afterwards.')

    def handle(self, *args, week_start=None, regenerate=False, email=False, **options):
        plan = services.generate_week(week_start, regenerate=regenerate)
        for meal in plan.meals.select_related('recipe'):
            self.stdout.write(f'{meal.date:%a %b %d}: {meal.recipe.name}')
        if email:
            self.stdout.write(f"Emailed: {', '.join(services.send_plan_email(plan)) or 'nobody'}")
