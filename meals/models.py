from datetime import date, timedelta

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class AllowedUser(models.Model):
    """Who may sign in (Google login is checked against this list) and who gets the weekly email."""
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=100, blank=True)
    receives_plan_email = models.BooleanField(default=True)

    def __str__(self):
        return self.name or self.email


# Options on the Preferences page. Keys are stored; labels are shown and sent to the planner.
COOK_TIMES = [
    (20, 'Quick', '10–20 min'),
    (30, 'Easy', 'Up to 30 min'),
    (45, 'Standard', 'Up to 45 min'),
    (60, 'Unhurried', 'Up to an hour'),
]
PROTEINS = [
    ('chicken', 'Chicken'), ('beef', 'Beef'), ('pork', 'Pork'), ('turkey', 'Turkey'),
    ('fish', 'Fish'), ('shellfish', 'Shrimp & shellfish'), ('vegetarian', 'Vegetarian'),
]
PROTEIN_LEVELS = [('more', 'More'), ('some', 'Some'), ('never', 'Never')]
CUISINES = [
    'American', 'Mexican', 'Italian', 'Mediterranean', 'Greek', 'Middle Eastern', 'Indian', 'Thai',
    'Chinese', 'Japanese', 'Korean', 'Vietnamese', 'Cajun & Southern', 'BBQ',
]
MEAL_STYLES = [
    'One-pan & sheet-pan', 'Slow cooker & Instant Pot', 'Grilling', 'Kid-friendly', 'Lighter & healthy',
    'Comfort food', 'Spicy', 'Leftovers for lunch',
]


class HouseholdSettings(models.Model):
    """Singleton (pk=1) of planning knobs, edited in /admin/."""
    # Comma-separated weekday numbers, Monday=0 ... Sunday=6.
    meal_weekdays = models.CharField(max_length=20, default='0,1,2,3,4')
    servings = models.PositiveSmallIntegerField(default=4)
    max_weeknight_minutes = models.PositiveSmallIntegerField(default=45)
    favorites_per_week = models.PositiveSmallIntegerField(
        default=2, help_text='How many dinners each week should be repeats of well-liked recipes.')
    dietary_notes = models.TextField(
        blank=True, help_text='Allergies, dislikes, kid preferences, equipment (e.g. "has an Instant Pot").')
    pantry_staples = models.TextField(
        blank=True, default='salt, black pepper, olive oil, vegetable oil, butter, all-purpose flour, sugar, garlic powder',
        help_text='Comma-separated items never added to the grocery cart.')
    dinner_time = models.TimeField(default='17:30')
    dinner_minutes = models.PositiveSmallIntegerField(default=60)
    calendar_id = models.CharField(
        max_length=255, blank=True, help_text='Google Calendar ID, shared with the service account.')
    kroger_location_id = models.CharField(
        max_length=20, blank=True, help_text='King Soopers store id; pick it on the Settings page.')
    kroger_location_name = models.CharField(max_length=200, blank=True, help_text='Shown on the Settings page.')
    # Set on the Preferences page.
    protein_preferences = models.JSONField(default=dict, blank=True, help_text='{"chicken": "more", "pork": "never"}')
    favorite_cuisines = models.JSONField(default=list, blank=True)
    meal_styles = models.JSONField(default=list, blank=True)
    preferences_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'household settings'

    def __str__(self):
        return 'Household settings'

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        if created:
            obj.refresh_from_db()  # field defaults like dinner_time='17:30' come back as real types
        return obj

    @property
    def weekdays(self) -> list[int]:
        return sorted({int(d) for d in self.meal_weekdays.split(',') if d.strip()})

    @property
    def pantry_staple_set(self) -> set[str]:
        return {s.strip().lower() for s in self.pantry_staples.split(',') if s.strip()}


class MemberPreferences(models.Model):
    """One household member's own tastes, which the planner applies to the whole household."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, related_name='food_preferences', on_delete=models.CASCADE)
    loves = models.TextField(blank=True, help_text='Comma-separated foods this person loves.')
    dislikes = models.TextField(blank=True, help_text="Comma-separated foods this person won't eat.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'member preferences'

    def __str__(self):
        return f'{self.user} preferences'

    @staticmethod
    def split(text: str) -> list[str]:
        return [item.strip() for item in text.replace('\n', ',').split(',') if item.strip()]

    @property
    def love_list(self) -> list[str]:
        return self.split(self.loves)

    @property
    def dislike_list(self) -> list[str]:
        return self.split(self.dislikes)


class Recipe(models.Model):
    name = models.CharField(max_length=200)
    summary = models.TextField(blank=True)
    cuisine = models.CharField(max_length=60, blank=True)
    total_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    servings = models.PositiveSmallIntegerField(null=True, blank=True)
    # [{"name", "quantity", "grocery_item", "pantry_staple"}]
    ingredients = models.JSONField(default=list)
    instructions = models.TextField(blank=True)
    archived = models.BooleanField(default=False, help_text='Never suggest this recipe again.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def score(self) -> int:
        return self.ratings.aggregate(total=Sum('value'))['total'] or 0

    def rating_for(self, user):
        rating = self.ratings.filter(user=user).first()
        return rating.value if rating else 0


class Rating(models.Model):
    LIKE = 1
    DISLIKE = -1
    VALUE_CHOICES = [(LIKE, 'Liked'), (DISLIKE, 'Disliked')]

    recipe = models.ForeignKey(Recipe, related_name='ratings', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    value = models.SmallIntegerField(choices=VALUE_CHOICES)
    comment = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['recipe', 'user'], name='one_rating_per_user')]


def week_start_for(day: date) -> date:
    """Plans run Sunday through Saturday; return the Sunday on or before `day`."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


class MealPlan(models.Model):
    week_start = models.DateField(unique=True, help_text='The Sunday this plan starts on.')
    created_at = models.DateTimeField(auto_now_add=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-week_start']

    def __str__(self):
        return f'Week of {self.week_start:%b %-d, %Y}'

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)

    @classmethod
    def current(cls):
        return cls.objects.filter(week_start=week_start_for(timezone.localdate())).first()


class PlannedMeal(models.Model):
    plan = models.ForeignKey(MealPlan, related_name='meals', on_delete=models.CASCADE)
    date = models.DateField()
    recipe = models.ForeignKey(Recipe, related_name='planned_meals', on_delete=models.PROTECT)
    why = models.CharField(max_length=300, blank=True, help_text="Claude's one-line reason for the pick.")
    locked = models.BooleanField(default=False, help_text='Keep this meal when the week is regenerated.')
    calendar_event_id = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['date']
        constraints = [models.UniqueConstraint(fields=['plan', 'date'], name='one_dinner_per_day')]

    def __str__(self):
        return f'{self.date:%a %b %-d}: {self.recipe}'


class MealSwap(models.Model):
    """A recipe someone swapped out of a plan, and why — fed back to the planner as a soft dislike."""
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class KrogerAuth(models.Model):
    """Singleton (pk=1) holding the household's Kroger OAuth tokens (cart write access)."""
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)


class GroceryProduct(models.Model):
    """Remembered Kroger product for a grocery item, so the same item maps to the same UPC every week."""
    grocery_item = models.CharField(max_length=200, unique=True)
    upc = models.CharField(max_length=20)
    description = models.CharField(max_length=300, blank=True)

    def __str__(self):
        return f'{self.grocery_item} → {self.description or self.upc}'


class CartPush(models.Model):
    plan = models.ForeignKey(MealPlan, related_name='cart_pushes', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    # [{"grocery_item", "upc", "description", "status"}]
    results = models.JSONField(default=list)
