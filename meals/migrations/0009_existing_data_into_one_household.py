"""Before households, everyone shared one menu. Keep it that way: put every existing allowed user and
every plan, recipe, setting, Kroger link and remembered product into one household."""
from django.db import migrations

HOUSEHOLD_NAME = 'Home'  # rename it in /admin/


def forwards(apps, schema_editor):
    Household = apps.get_model('meals', 'Household')
    Membership = apps.get_model('meals', 'Membership')
    AllowedUser = apps.get_model('meals', 'AllowedUser')
    owned = [apps.get_model('meals', name) for name in
             ('HouseholdSettings', 'KrogerAuth', 'MealPlan', 'Recipe', 'GroceryProduct')]

    if not AllowedUser.objects.exists() and not any(model.objects.exists() for model in owned):
        return  # a fresh database: nothing to carry over
    household = Household.objects.create(name=HOUSEHOLD_NAME)
    for user in AllowedUser.objects.all():
        Membership.objects.create(household=household, member=user, receives_plan_email=user.receives_plan_email)
    AllowedUser.objects.update(active_household=household)

    for model in owned:
        rows = model.objects.filter(household__isnull=True)
        if model._meta.get_field('household').one_to_one:
            # There was only ever one settings row and one Kroger link (pk=1); keep that one.
            first = rows.order_by('pk').first()
            rows = model.objects.filter(pk=first.pk) if first else rows.none()
        rows.update(household=household)
    # Any extra singleton rows were never read (load() only ever used pk=1); drop them so 0010 can
    # make household required.
    for model in owned:
        model.objects.filter(household__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('meals', '0008_households'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
